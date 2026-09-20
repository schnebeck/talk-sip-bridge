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
