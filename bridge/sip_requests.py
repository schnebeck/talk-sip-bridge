"""Assembly of the SIP messages this bridge sends.

Every request has the same skeleton - request line, Via, Max-Forwards,
From, To, Call-ID, CSeq, then whatever that method adds, then
Content-Length and the body - and every response echoes the request's Via
lines in order. Building them in one place keeps that skeleton consistent
and puts the transport the bridge speaks in a single spot: `via_header`
and `contact_header` are the only two functions that name it.

Pure string assembly, no sockets and no call state: what comes out is what
goes on the wire.
"""
import secrets

USER_AGENT = "FritzboxTalkBridge/0.1"
ALLOWED_METHODS = "INVITE, ACK, BYE, CANCEL, OPTIONS"

# Which transport a line speaks appears twice in what it sends: in Via, as
# the transport of its own socket, and in Contact, as the one the gateway
# should reach it over. They can legitimately differ - with a relay in
# between, Contact names the relay's leg, not this host's (see
# LineConfig.contact_host / LineConfig.contact_transport).


def via_transport(line) -> str:
    return line.sip_transport.upper()


def contact_transport(line) -> str:
    """What the gateway is told to use. Defaults to the line's own
    transport; a relay that changes transport on the way sets it apart."""
    return (getattr(line, "contact_transport", "") or line.sip_transport).lower()


def new_branch(suffix: str = "") -> str:
    """RFC 3261 requires the magic cookie prefix on every branch."""
    return f"z9hG4bK{secrets.token_hex(4)}{suffix}"


def new_tag() -> str:
    return f"tag{secrets.token_hex(4)}"


def via_header(line, branch: str) -> str:
    return f"Via: SIP/2.0/{via_transport(line)} {line.local_ip}:{line.local_sip_port};rport;branch={branch}"


def contact_header(line, *, with_transport: bool = True) -> str:
    """Where the gateway should send calls for this registration. With a
    relay configured this is the relay's LAN-reachable address rather than
    this host's own (see docs/CONFIG.md), because the gateway can only
    deliver to an address on its own LAN."""
    host = line.contact_host or line.local_ip
    port = line.contact_port or line.local_sip_port
    transport = f";transport={contact_transport(line)}" if with_transport else ""
    return f"Contact: <sip:{line.sip_user}@{host}:{port}{transport}>"


def address(line, user: str) -> str:
    """A From/To value addressing `user` at this line's gateway."""
    return f"<sip:{user}@{line.gateway_host}>"


def build_request(method: str, request_uri: str, *, line, branch: str, from_header: str,
                  to_header: str, call_id: str, cseq, headers=(), body: str = "") -> bytes:
    """headers are this method's own, placed between CSeq and
    Content-Length; Content-Length is always derived from body."""
    lines = [
        f"{method} {request_uri} SIP/2.0",
        via_header(line, branch),
        "Max-Forwards: 70",
        f"From: {from_header}",
        f"To: {to_header}",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} {method}",
        *headers,
        f"Content-Length: {len(body)}",
        "",
        body,
    ]
    return "\r\n".join(lines).encode()


def build_response(status_line: str, req_headers: dict, *, extra_headers=(), body: str = "",
                   to_tag: str = None) -> bytes:
    """A response echoes every Via line of the request it answers, in
    order - a request that passed through a relay carries several, and
    dropping any of them breaks the path back."""
    via_values = req_headers.get("via", [])
    if isinstance(via_values, str):
        via_values = [via_values]
    to_header = req_headers.get("to", "")
    if to_tag and "tag=" not in to_header:
        to_header = f"{to_header};tag={to_tag}"
    lines = [f"SIP/2.0 {status_line}"]
    lines.extend(f"Via: {v}" for v in via_values)
    lines.extend([
        f"From: {req_headers.get('from', '')}",
        f"To: {to_header}",
        f"Call-ID: {req_headers.get('call-id', '')}",
        f"CSeq: {req_headers.get('cseq', '')}",
    ])
    lines.extend(extra_headers)
    lines.append(f"Content-Length: {len(body)}")
    lines.append("")
    lines.append(body)
    return "\r\n".join(lines).encode()


def authorization_header(username: str, realm: str, uri: str, nonce: str, response_digest: str) -> str:
    return (
        f'Authorization: Digest username="{username}", realm="{realm}", '
        f'nonce="{nonce}", uri="{uri}", response="{response_digest}", algorithm=MD5'
    )


def build_register(line, *, call_id: str, tag: str, branch: str, cseq, expires: int,
                   auth_header: str = None, wildcard_contact: bool = False) -> bytes:
    contact = "Contact: *" if wildcard_contact else contact_header(line)
    headers = [
        contact,
        f"Expires: {expires}",
        f"User-Agent: {USER_AGENT}",
        f"Allow: {ALLOWED_METHODS}",
    ]
    if auth_header:
        headers.append(auth_header)
    return build_request(
        "REGISTER", f"sip:{line.gateway_host}", line=line, branch=branch,
        from_header=f"{address(line, line.sip_user)};tag={tag}",
        to_header=address(line, line.sip_user),
        call_id=call_id, cseq=cseq, headers=headers,
    )


def build_invite(line, *, number: str, call_id: str, from_tag: str, branch: str, cseq,
                 sdp: str, auth_header: str = None) -> bytes:
    headers = [
        contact_header(line),
        f"Allow: {ALLOWED_METHODS}",
        "Content-Type: application/sdp",
    ]
    if auth_header:
        headers.append(auth_header)
    return build_request(
        "INVITE", f"sip:{number}@{line.gateway_host}", line=line, branch=branch,
        from_header=f"{address(line, line.sip_user)};tag={from_tag}",
        to_header=address(line, number),
        call_id=call_id, cseq=cseq, headers=headers, body=sdp,
    )


def build_ack(line, *, number: str, call_id: str, from_tag: str, branch: str, cseq,
              to_header: str) -> bytes:
    return build_request(
        "ACK", f"sip:{number}@{line.gateway_host}", line=line, branch=branch,
        from_header=f"{address(line, line.sip_user)};tag={from_tag}",
        to_header=to_header, call_id=call_id, cseq=cseq,
    )


def build_cancel(line, *, number: str, call_id: str, from_tag: str, branch: str, cseq) -> bytes:
    """CANCEL repeats the branch of the INVITE it cancels - that is what
    identifies the transaction being abandoned."""
    return build_request(
        "CANCEL", f"sip:{number}@{line.gateway_host}", line=line, branch=branch,
        from_header=f"{address(line, line.sip_user)};tag={from_tag}",
        to_header=address(line, number), call_id=call_id, cseq=cseq,
    )


def build_bye(line, *, request_uri: str, call_id: str, from_header: str, to_header: str,
              branch: str, cseq=2) -> bytes:
    """request_uri is the peer's own Contact from the dialog, not the
    address originally dialled - see sip_messages.extract_contact_uri."""
    return build_request(
        "BYE", request_uri, line=line, branch=branch, from_header=from_header,
        to_header=to_header, call_id=call_id, cseq=cseq,
    )
