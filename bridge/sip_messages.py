"""SIP message-level helpers: header parsing, digest authentication, and the
character allowlist for anything interpolated into a raw message.

Transport- and call-independent - everything here works on strings.
"""
import hashlib
import re

# Characters valid in a SIP user part / phone number (RFC 3261 user-unreserved
# plus digits). Rejecting anything else before interpolating a number into a
# raw SIP message prevents header/request injection via a crafted dialout
# request.
VALID_NUMBER = re.compile(r"[0-9A-Za-z+*#.\-]{1,32}")


def md5hex(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def parse_sip_headers(text: str) -> dict:
    """"via" is handled specially: a request/response that passed through a
    relay has multiple Via lines, all of which must be preserved in order
    for responses to route back correctly."""
    headers = {}
    via_lines = []
    for line in text.split("\r\n")[1:]:
        if not line:
            break
        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip().lower()
            if key in ("via", "v"):
                via_lines.append(v.strip())
            else:
                headers[key] = v.strip()
    if via_lines:
        headers["via"] = via_lines
    return headers


def digest_response(username: str, realm: str, password: str, method: str, uri: str, nonce: str) -> str:
    ha1 = md5hex(f"{username}:{realm}:{password}")
    ha2 = md5hex(f"{method}:{uri}")
    return md5hex(f"{ha1}:{nonce}:{ha2}")


def extract_contact_uri(contact_header: str) -> str:
    """A Contact header value may be "<sip:...>" or a bare "sip:...",
    optionally followed by ;params. In-dialog requests (BYE, etc.) must be
    sent to this URI, not the original Request-URI/To - RFC 3261 - the
    party at the other end may only be reachable at an address it gave us
    dynamically (e.g. the gateway assigning an opaque per-dialog contact)."""
    contact_header = contact_header.strip()
    m = re.search(r'<([^>]+)>', contact_header)
    if m:
        return m.group(1)
    return contact_header.split(';')[0].strip()


def caller_number(from_header: str) -> str:
    """The number a call came from: the user part of the SIP URI.

    Never the display name in front of it. A FRITZ!Box handset announces
    itself as `"FritzFon schwarz" <sip:**611@fritz.box>` - the name is
    what its owner typed into the box, and using it where a number
    belongs means Nextcloud is told a caller rang from "FritzFon
    schwarz"."""
    uri_user = re.search(r"sips?:([^@;>\s]+)@", from_header)
    return uri_user.group(1) if uri_user else caller_display_name(from_header)


def caller_display_name(from_header: str) -> str:
    """What to call the caller on screen - the display name if the
    gateway sent one, else the number. For showing, not for matching or
    for handing to an API that asks for a number."""
    quoted = re.match(r'\s*"([^"]+)"', from_header)
    if quoted:
        return quoted.group(1)
    uri_user = re.search(r"sips?:([^@;>]+)", from_header)
    if uri_user:
        return uri_user.group(1)
    return from_header.split(";")[0].strip()


def dialled_number(first_line: str, headers: dict) -> str:
    """Which number an incoming call was placed to.

    One registered line receives calls for more than one number - a trunk
    delivers every number it carries down the same registration, and a
    gateway with one account still distinguishes the extension dialled
    from the account it is delivering to.

    Three places, in this order, because a gateway that fills in the more
    specific one means it:

    1. `P-Called-Party-ID` (RFC 3455), whose whole purpose is to name the
       number that was called. A FRITZ!Box puts the dialled extension
       here and nowhere else - measured: an INVITE to this bridge is
       addressed to `sip:sip-phone@...`, the account's own contact, in
       both the Request-URI and To, and carries
       `P-Called-Party-ID: <sip:**9@fritz.box>` for the number the
       handset actually dialled.
    2. the Request-URI, which is where a trunk puts the number it is
       delivering,
    3. To, which holds what the caller dialled where nothing rewrote it.

    The user part only, without the host: "sip:**622@fritz.box" is the
    number **622 to everyone except the box."""
    match = re.match(r"\s*[A-Z]+\s+(\S+)", first_line)
    for candidate in (headers.get("p-called-party-id", ""),
                      match.group(1) if match else "",
                      headers.get("to", "")):
        user = re.search(r"sips?:([^@;>\s]+)@", candidate)
        if user:
            return user.group(1)
    return ""


def content_length_of(header_block: str) -> int:
    """The body length a message announces, 0 if it announces none. Both
    spellings occur: "l" is the compact form."""
    for line in header_block.split("\r\n")[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        if name.strip().lower() in ("content-length", "l"):
            try:
                return max(0, int(value.strip()))
            except ValueError:
                return 0
    return 0


def split_messages(buffer: bytes) -> tuple[list, bytes]:
    """Splits a stream into complete SIP messages and the remainder.

    A datagram is one message; a stream is not. Over TCP the only thing
    that says where a message ends is its own Content-Length, so a reader
    that assumes otherwise either truncates a message or glues two
    together - both silently.

    Leading CRLFs are keepalives, not messages: a connection with no SIP
    traffic on it gets closed by the far end, so both ends ping it with
    bare line breaks (RFC 5626). Treating one as an empty message hands
    the call layer something with no start line.

    Returns (messages, rest). Anything incomplete stays in rest for the
    next read."""
    messages = []
    while True:
        while buffer[:2] == b"\r\n":
            buffer = buffer[2:]
        separator = buffer.find(b"\r\n\r\n")
        if separator < 0:
            return messages, buffer
        header_end = separator + 4
        try:
            header_block = buffer[:separator].decode(errors="replace")
        except Exception:
            return messages, buffer
        total = header_end + content_length_of(header_block)
        if len(buffer) < total:
            return messages, buffer  # body still on its way
        messages.append(buffer[:total])
        buffer = buffer[total:]


def parse_auth_challenge(auth_val: str) -> dict:
    if auth_val.lower().startswith("digest"):
        auth_val = auth_val.split(" ", 1)[1]
    parts = {}
    for chunk in auth_val.split(","):
        chunk = chunk.strip()
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip()] = v.strip().strip('"')
    return parts
