"""SIP registration and call handling against a phone gateway, with real
audio for both inbound and outbound calls. Each CallManager is bound to one
LineConfig (see config.py) and holds at most one active call on that line -
a second incoming call on the same line is rejected with "486 Busy Here".
Running several lines concurrently means constructing several independent
(SipTransport, SipRegistrar, CallManager) sets, one per line.

Contains no Talk-specific logic - callers pass callback hooks
(on_incoming_call, on_call_connected, on_call_ended, on_call_failed) so the
Talk-facing layer (talk_client.py) can react (addsession, publish audio,
etc.) without this module knowing anything about Talk.
"""
import hashlib
import queue
import re
import secrets
import socket
import threading
import time

import numpy as np

from config import config
from rtp import PT_G722, PT_PCMU, RtpSession

# Characters valid in a SIP user part / phone number (RFC 3261 user-unreserved
# plus digits). Rejecting anything else before interpolating a number into a
# raw SIP message prevents header/request injection via a crafted dialout
# request.
_VALID_NUMBER = re.compile(r"[0-9A-Za-z+*#.\-]{1,32}")


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


def _digest_response(username: str, realm: str, password: str, method: str, uri: str, nonce: str) -> str:
    ha1 = md5hex(f"{username}:{realm}:{password}")
    ha2 = md5hex(f"{method}:{uri}")
    return md5hex(f"{ha1}:{nonce}:{ha2}")


def _extract_contact_uri(contact_header: str) -> str:
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


def _parse_auth_challenge(auth_val: str) -> dict:
    if auth_val.lower().startswith("digest"):
        auth_val = auth_val.split(" ", 1)[1]
    parts = {}
    for chunk in auth_val.split(","):
        chunk = chunk.strip()
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip()] = v.strip().strip('"')
    return parts


class SipTransport:
    """A single, permanently bound UDP socket for one line: its own
    REGISTER/INVITE transactions AND incoming requests. Its Contact header
    (where calls arrive) points at exactly this port. Only accepts traffic
    from that line's own configured proxy/gateway."""

    def __init__(self, call_manager, line):
        self.line = line
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((line.local_ip, line.local_sip_port))
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.call_manager = call_manager
        self.thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.thread.start()

    def send(self, data: bytes, addr=None):
        self.sock.sendto(data, addr or (self.line.proxy_host, self.line.proxy_port))

    def wait_response(self, call_id: str, timeout: float = None):
        if timeout is None:
            timeout = config.sip_response_timeout
        q = queue.Queue()
        with self.pending_lock:
            self.pending[call_id] = q
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            with self.pending_lock:
                self.pending.pop(call_id, None)

    def open_waiter(self, call_id: str) -> queue.Queue:
        q = queue.Queue()
        with self.pending_lock:
            self.pending[call_id] = q
        return q

    def close_waiter(self, call_id: str):
        with self.pending_lock:
            self.pending.pop(call_id, None)

    def _listen_loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(65536)
            except OSError:
                return
            if addr[0] != self.line.proxy_host:
                # Only this line's configured proxy/gateway may send it SIP
                # traffic - anything else on this network could otherwise
                # forge an INVITE, BYE or CANCEL for an existing call.
                print(f"[sip:{self.line.id}] Ignoring packet from unexpected source {addr[0]} (expected {self.line.proxy_host})")
                continue
            text = data.decode(errors="replace")
            if not text.strip():
                continue
            first_line = text.split("\r\n", 1)[0]
            headers = parse_sip_headers(text)
            call_id = headers.get("call-id", "")

            if first_line.startswith("SIP/2.0"):
                with self.pending_lock:
                    q = self.pending.get(call_id)
                if q:
                    q.put(text)
                continue

            method = first_line.split(" ")[0]
            try:
                if method == "INVITE":
                    self.call_manager.handle_invite(text, headers, call_id, addr)
                elif method == "BYE":
                    self.call_manager.handle_bye(text, headers, call_id, addr)
                elif method == "CANCEL":
                    self.call_manager.handle_cancel(text, headers, call_id, addr)
                elif method == "OPTIONS":
                    self.call_manager.handle_options(text, headers, call_id, addr)
            except Exception as e:
                print(f"[sip:{self.line.id}] Error handling {method}: {e}")


class SipRegistrar:
    def __init__(self, transport_holder, line):
        self.line = line
        self.lock = threading.Lock()
        self.registered = False
        self.keepalive_thread = None
        self.stop_event = threading.Event()
        self.last_error = None
        self._transport_holder = transport_holder

    def _build_register(self, cseq, call_id, tag, branch, auth_header=None, expires=None, wildcard_contact=False):
        line = self.line
        expires = config.register_expires if expires is None else expires
        # Contact points at the configured proxy/relay's LAN-reachable
        # address, not at this host itself, when a relay is configured (see
        # docs/CONFIG.md) - otherwise the gateway can't deliver calls here.
        contact_host = line.contact_host or line.local_ip
        contact_port = line.contact_port or line.local_sip_port
        contact = "*" if wildcard_contact else f"<sip:{line.sip_user}@{contact_host}:{contact_port};transport=tcp>"
        lines = [
            f"REGISTER sip:{line.gateway_host} SIP/2.0",
            f"Via: SIP/2.0/UDP {line.local_ip}:{line.local_sip_port};rport;branch={branch}",
            "Max-Forwards: 70",
            f"From: <sip:{line.sip_user}@{line.gateway_host}>;tag={tag}",
            f"To: <sip:{line.sip_user}@{line.gateway_host}>",
            f"Call-ID: {call_id}",
            f"CSeq: {cseq} REGISTER",
            f"Contact: {contact}",
            f"Expires: {expires}",
            "User-Agent: FritzboxTalkBridge/0.1",
            "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS",
        ]
        if auth_header:
            lines.append(auth_header)
        lines.append("Content-Length: 0")
        lines.append("")
        lines.append("")
        return "\r\n".join(lines).encode()

    def _do_register(self, expires: int, wildcard: bool = False) -> bool:
        line = self.line
        transport = self._transport_holder()
        call_id = f"bridge-reg-{secrets.token_hex(6)}@{line.local_ip}"
        tag = f"tag{secrets.token_hex(4)}"
        branch = f"z9hG4bK{secrets.token_hex(4)}"

        msg = self._build_register(1, call_id, tag, branch, expires=expires, wildcard_contact=wildcard)
        transport.send(msg)
        resp_text = transport.wait_response(call_id, timeout=config.sip_response_timeout)
        if resp_text is None:
            self.last_error = "Timeout on REGISTER #1"
            return False

        status_line = resp_text.split("\r\n", 1)[0]
        if " 401 " not in status_line and " 407 " not in status_line:
            if " 200 " in status_line:
                return True
            self.last_error = f"Unexpected response: {status_line}"
            return False

        headers = parse_sip_headers(resp_text)
        challenge = _parse_auth_challenge(headers.get("www-authenticate", ""))
        realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")
        uri = f"sip:{line.gateway_host}"
        response_digest = _digest_response(line.sip_user, realm, line.sip_pass, "REGISTER", uri, nonce)
        auth_header = (
            f'Authorization: Digest username="{line.sip_user}", realm="{realm}", '
            f'nonce="{nonce}", uri="{uri}", response="{response_digest}", algorithm=MD5'
        )
        branch2 = f"z9hG4bK{secrets.token_hex(4)}x2"
        msg2 = self._build_register(2, call_id, tag, branch2, auth_header=auth_header, expires=expires, wildcard_contact=wildcard)
        transport.send(msg2)
        resp2_text = transport.wait_response(call_id, timeout=config.sip_response_timeout)
        if resp2_text is None:
            self.last_error = "Timeout on REGISTER #2 (with auth)"
            return False
        if " 200 " in resp2_text.split("\r\n", 1)[0]:
            self.last_error = None
            return True
        self.last_error = f"Registration failed: {resp2_text.splitlines()[0]}"
        return False

    def _keepalive_loop(self):
        while not self.stop_event.wait(config.register_expires * 0.6):
            with self.lock:
                if not self.registered:
                    return
                ok = self._do_register(config.register_expires)
                if not ok:
                    self.registered = False
                    return

    def wipe_all_bindings(self):
        with self.lock:
            self._do_register(0, wildcard=True)

    def turn_on(self) -> bool:
        with self.lock:
            if self.registered:
                return True
            ok = self._do_register(config.register_expires)
            self.registered = ok
            if ok:
                self.stop_event.clear()
                self.keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
                self.keepalive_thread.start()
            return ok

    def turn_off(self) -> bool:
        with self.lock:
            if not self.registered:
                return True
            self.stop_event.set()
            ok = self._do_register(0)
            self.registered = False
            return ok

    def status(self) -> dict:
        return {
            "line": self.line.id,
            "registered": self.registered,
            "username": self.line.sip_user,
            "proxy": f"{self.line.proxy_host}:{self.line.proxy_port}",
            "last_error": self.last_error,
        }


def _sdp_host_port(line, local_port: int) -> tuple[str, int]:
    """Advertises the relay's LAN-reachable address if a media relay is
    configured for this line (see LineConfig.media_relay_enabled) - the
    gateway can only deliver RTP to an address on its own LAN, same
    constraint as the SIP Contact header."""
    host = line.relay_lan_host if line.media_relay_enabled else line.local_ip
    port = line.relay_lan_port if line.media_relay_enabled else local_port
    return host, port


def _offer_sdp(line, local_port: int) -> tuple[str, str, int]:
    """Returns (sdp, advertised_host, advertised_port). Offers G.722
    ("HD-Telefonie") first, PCMU as a fallback for gateways that don't
    support it - the far end picks one in its answer."""
    host, port = _sdp_host_port(line, local_port)
    sdp = (
        f"v=0\r\no=- 0 0 IN IP4 {host}\r\ns=-\r\n"
        f"c=IN IP4 {host}\r\nt=0 0\r\nm=audio {port} RTP/AVP {PT_G722} {PT_PCMU}\r\n"
        f"a=rtpmap:{PT_G722} G722/8000\r\na=rtpmap:{PT_PCMU} PCMU/8000\r\n"
    )
    return sdp, host, port


def _answer_sdp(line, local_port: int, payload_type: int) -> tuple[str, str, int]:
    """Returns (sdp, advertised_host, advertised_port) for a single, already
    chosen codec - used when this line is the UAS answering an INVITE."""
    host, port = _sdp_host_port(line, local_port)
    name = "G722" if payload_type == PT_G722 else "PCMU"
    sdp = (
        f"v=0\r\no=- 0 0 IN IP4 {host}\r\ns=-\r\n"
        f"c=IN IP4 {host}\r\nt=0 0\r\nm=audio {port} RTP/AVP {payload_type}\r\n"
        f"a=rtpmap:{payload_type} {name}/8000\r\n"
    )
    return sdp, host, port


def _extract_sip_body(text: str) -> str:
    parts = text.split("\r\n\r\n", 1)
    return parts[1] if len(parts) > 1 else ""


def _parse_offered_payload_types(sdp_body: str) -> list[int]:
    for line in sdp_body.splitlines():
        line = line.strip()
        if line.startswith("m=audio"):
            fields = line.split()
            return [int(p) for p in fields[3:] if p.isdigit()]
    return []


def _choose_payload_type(offered: list[int]) -> int:
    """We're the UAS (answering an INVITE) - pick G.722 if the caller
    offered it, otherwise fall back to PCMU regardless of what else was
    offered (the only two codecs this bridge implements)."""
    return PT_G722 if PT_G722 in offered else PT_PCMU


class CallManager:
    """Bound to one LineConfig; holds at most one active call on that line.
    Real audio (via RtpSession) is set up for both inbound and outbound
    calls; talk_client.py reads from/writes to call["rtp"] to bridge it into
    a Talk room."""

    def __init__(self, line, *, on_incoming_call=None, on_call_connected=None, on_call_ended=None, on_call_failed=None):
        self.line = line
        self.lock = threading.Lock()
        self.call = None
        self.transport = None
        self.on_incoming_call = on_incoming_call or (lambda **kw: None)
        self.on_call_connected = on_call_connected or (lambda **kw: None)
        self.on_call_ended = on_call_ended or (lambda **kw: None)
        self.on_call_failed = on_call_failed or (lambda **kw: None)

    def status(self) -> dict:
        with self.lock:
            if not self.call:
                return {"active_call": None}
            return {"active_call": {
                "direction": self.call.get("direction", "inbound"),
                "status": self.call["status"],
                "number": self.call.get("number"),
            }}

    def _send_response(self, status_line, req_headers, remote_addr, extra_headers=None, body="", to_tag=None):
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
        if extra_headers:
            lines.extend(extra_headers)
        lines.append(f"Content-Length: {len(body)}")
        lines.append("")
        lines.append(body)
        self.transport.send("\r\n".join(lines).encode(), remote_addr)

    def _new_rtp_session(self, payload_type: int = PT_PCMU) -> RtpSession:
        line = self.line
        if line.media_relay_enabled:
            return RtpSession(line.local_ip, line.local_rtp_port, line.relay_overlay_host, line.relay_overlay_port, payload_type=payload_type)
        return RtpSession(line.local_ip, line.local_rtp_port, line.gateway_host, line.local_rtp_port, payload_type=payload_type)

    def handle_invite(self, text, headers, call_id, remote_addr):
        offered_pts = _parse_offered_payload_types(_extract_sip_body(text))
        with self.lock:
            if self.call is not None:
                self._send_response("486 Busy Here", headers, remote_addr, to_tag=f"bridge{secrets.token_hex(3)}")
                return
            to_tag = f"bridge{secrets.token_hex(3)}"
            self.call = {"call_id": call_id, "headers": headers, "remote_addr": remote_addr,
                         "status": "ringing", "bye_timer": None, "to_tag": to_tag, "rtp": None,
                         "offered_pts": offered_pts}
        caller = headers.get("from", "unknown")
        print(f"[call:{self.line.id}] Incoming call from {caller}")
        self._send_response("180 Ringing", headers, remote_addr, to_tag=to_tag)
        self.on_incoming_call(call_id=call_id, caller=caller)

    def answer(self) -> bool:
        """Accepts the current ringing call with real audio."""
        line = self.line
        with self.lock:
            if not self.call or self.call["status"] != "ringing":
                return False
            headers = self.call["headers"]
            remote_addr = self.call["remote_addr"]
            to_tag = self.call["to_tag"]
            call_id = self.call["call_id"]
            payload_type = _choose_payload_type(self.call.get("offered_pts") or [])
            rtp = self._new_rtp_session(payload_type)
            self.call["status"] = "connected"
            self.call["rtp"] = rtp
        sdp, _, _ = _answer_sdp(line, line.local_rtp_port, payload_type)
        contact_host = line.contact_host or line.local_ip
        contact_port = line.contact_port or line.local_sip_port
        self._send_response("200 OK", headers, remote_addr, extra_headers=[
            f"Contact: <sip:{line.sip_user}@{contact_host}:{contact_port}>",
            "Content-Type: application/sdp",
        ], body=sdp, to_tag=to_tag)
        if line.media_relay_enabled:
            rtp.send_pcm(np.zeros(rtp.samples_per_packet, dtype=np.int16))  # prime the relay
        timer = threading.Timer(config.max_call_duration, self.hangup)
        timer.daemon = True
        with self.lock:
            if self.call and self.call["call_id"] == call_id:
                self.call["bye_timer"] = timer
        timer.start()
        self.on_call_connected(call_id=call_id, direction="inbound", rtp=rtp)
        return True

    def decline(self) -> bool:
        with self.lock:
            if not self.call or self.call["status"] != "ringing":
                return False
            headers = self.call["headers"]
            remote_addr = self.call["remote_addr"]
            to_tag = self.call["to_tag"]
            self.call = None
        self._send_response("603 Decline", headers, remote_addr, to_tag=to_tag)
        return True

    def handle_bye(self, text, headers, call_id, remote_addr):
        with self.lock:
            if self.call and self.call["call_id"] == call_id:
                if self.call.get("bye_timer"):
                    self.call["bye_timer"].cancel()
                if self.call.get("rtp"):
                    self.call["rtp"].close()
                self.call = None
        self._send_response("200 OK", headers, remote_addr)
        self.on_call_ended(call_id=call_id, reason="remote_bye")

    def handle_cancel(self, text, headers, call_id, remote_addr):
        with self.lock:
            if self.call and self.call["call_id"] == call_id:
                self.call = None
        self._send_response("200 OK", headers, remote_addr)
        self.on_call_ended(call_id=call_id, reason="cancelled")

    def handle_options(self, text, headers, call_id, remote_addr):
        self._send_response("200 OK", headers, remote_addr)

    def hangup(self):
        """Ends the current call (either direction) from our side."""
        line = self.line
        with self.lock:
            if not self.call:
                return
            call = self.call
            self.call = None
        if call.get("bye_timer"):
            call["bye_timer"].cancel()
        if call.get("rtp"):
            call["rtp"].close()
        if call.get("direction") == "outbound":
            # In-dialog requests go to the Contact the gateway gave us in
            # its 200 OK (often an opaque per-dialog URI, not the number we
            # dialed) - falls back to the original target if a call somehow
            # never captured one (should not normally happen once connected).
            request_uri = call.get("remote_contact") or f"sip:{call['number']}@{line.gateway_host}"
            bye = (
                f"BYE {request_uri} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {line.local_ip}:{line.local_sip_port};rport;branch=z9hG4bK{secrets.token_hex(4)}\r\n"
                f"Max-Forwards: 70\r\n"
                f"From: <sip:{line.sip_user}@{line.gateway_host}>;tag={call['from_tag']}\r\n"
                f"To: <sip:{call['number']}@{line.gateway_host}>;tag={call['to_tag']}\r\n"
                f"Call-ID: {call['call_id']}\r\nCSeq: 2 BYE\r\nContent-Length: 0\r\n\r\n"
            )
            self.transport.send(bye.encode())
        else:
            headers = call["headers"]
            # Likewise, the caller's own Contact from their INVITE is the
            # correct in-dialog target - not the gateway's registrar address.
            caller_contact = headers.get("contact", "")
            request_uri = _extract_contact_uri(caller_contact) if caller_contact else f"sip:{line.gateway_host}"
            bye = (
                f"BYE {request_uri} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {line.local_ip}:{line.local_sip_port};rport;branch=z9hG4bK{secrets.token_hex(4)}\r\n"
                f"Max-Forwards: 70\r\n"
                f"From: {headers.get('to', '')}\r\n"
                f"To: {headers.get('from', '')}\r\n"
                f"Call-ID: {call['call_id']}\r\nCSeq: 2 BYE\r\nContent-Length: 0\r\n\r\n"
            )
            self.transport.send(bye.encode(), call["remote_addr"])
        self.on_call_ended(call_id=call["call_id"], reason="local_hangup")

    def dial(self, number: str) -> dict:
        """number is already this line's own dial target (stripped/prefixed
        by the caller, e.g. talk_client.py's line-selection logic) - this
        only re-validates the allowlist, it does not re-derive the target."""
        line = self.line
        if not _VALID_NUMBER.fullmatch(number):
            return {"error": f"Rejected number (disallowed characters): {number!r}"}
        if line.dialout_number_allowlist and not re.fullmatch(line.dialout_number_allowlist, number):
            return {"error": f"Number not in line {line.id}'s configured allowlist: {number!r}"}
        # A number that passed the allowlist is, by construction of that
        # allowlist, an internal extension - only those get the gateway's
        # own dial-prefix notation (e.g. "**" on a FritzBox) added, needed
        # to actually alert the physical device rather than just being
        # accepted at the SIP signaling level.
        dial_target = number
        if line.dialout_number_allowlist and line.dialout_internal_dial_prefix:
            dial_target = line.dialout_internal_dial_prefix + number
        with self.lock:
            if self.call is not None:
                return {"error": f"Line {line.id} already has an active call"}
            call_id = f"bridge-out-{secrets.token_hex(6)}@{line.local_ip}:{line.local_sip_port}"
            from_tag = f"tag{secrets.token_hex(4)}"
            self.call = {"call_id": call_id, "direction": "outbound", "status": "dialing",
                         "to_tag": None, "bye_timer": None, "number": number, "from_tag": from_tag, "rtp": None}
        threading.Thread(target=self._outbound_worker, args=(dial_target, call_id, from_tag), daemon=True).start()
        return {"started": True, "call_id": call_id}

    def _outbound_worker(self, number, call_id, from_tag):
        line = self.line
        rtp = self._new_rtp_session()  # payload type finalized once the 200 OK's answer is parsed
        sdp, _, _ = _offer_sdp(line, line.local_rtp_port)

        def build_invite(cseq, branch, auth_header=None):
            lines = [
                f"INVITE sip:{number}@{line.gateway_host} SIP/2.0",
                f"Via: SIP/2.0/UDP {line.local_ip}:{line.local_sip_port};rport;branch={branch}",
                "Max-Forwards: 70",
                f"From: <sip:{line.sip_user}@{line.gateway_host}>;tag={from_tag}",
                f"To: <sip:{number}@{line.gateway_host}>",
                f"Call-ID: {call_id}",
                f"CSeq: {cseq} INVITE",
                f"Contact: <sip:{line.sip_user}@{line.contact_host or line.local_ip}:{line.contact_port or line.local_sip_port};transport=tcp>",
                "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS",
                "Content-Type: application/sdp",
                f"Content-Length: {len(sdp)}",
                "",
                sdp,
            ]
            if auth_header:
                lines.insert(-3, auth_header)
            return "\r\n".join(lines).encode()

        def send_ack(to_header, cseq):
            ack = (
                f"ACK sip:{number}@{line.gateway_host} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {line.local_ip}:{line.local_sip_port};rport;branch=z9hG4bK{secrets.token_hex(4)}\r\n"
                f"Max-Forwards: 70\r\n"
                f"From: <sip:{line.sip_user}@{line.gateway_host}>;tag={from_tag}\r\n"
                f"To: {to_header}\r\nCall-ID: {call_id}\r\nCSeq: {cseq} ACK\r\nContent-Length: 0\r\n\r\n"
            )
            self.transport.send(ack.encode())

        cseq = 1
        q = self.transport.open_waiter(call_id)
        self.transport.send(build_invite(cseq, f"z9hG4bK{secrets.token_hex(4)}"))
        print(f"[call:{line.id}] Outbound call started to {number}")
        deadline = time.time() + config.outbound_call_timeout
        connected = False
        try:
            while time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    resp = q.get(timeout=remaining)
                except queue.Empty:
                    break
                status_line = resp.split("\r\n", 1)[0]
                parts = status_line.split(" ", 2)
                code = parts[1] if len(parts) > 1 else ""
                resp_headers = parse_sip_headers(resp)
                # UDP does not guarantee ordering or delivery-once - a
                # retransmitted or delayed response for an earlier CSeq
                # (e.g. a duplicate 401) must not be reprocessed, or it
                # triggers a spurious second INVITE while the real one is
                # still in progress, which the gateway then rejects with
                # "491 Request Pending".
                resp_cseq = resp_headers.get("cseq", "").split(" ")[0]
                if resp_cseq.isdigit() and int(resp_cseq) != cseq:
                    continue
                if code == "100":
                    continue
                if code in ("401", "407"):
                    auth_hdr_name = "www-authenticate" if code == "401" else "proxy-authenticate"
                    challenge = _parse_auth_challenge(resp_headers.get(auth_hdr_name, ""))
                    realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")
                    uri = f"sip:{number}@{line.gateway_host}"
                    resp_digest = _digest_response(line.sip_user, realm, line.sip_pass, "INVITE", uri, nonce)
                    auth_header = (
                        f'Authorization: Digest username="{line.sip_user}", realm="{realm}", '
                        f'nonce="{nonce}", uri="{uri}", response="{resp_digest}", algorithm=MD5'
                    )
                    cseq += 1
                    self.transport.send(build_invite(cseq, f"z9hG4bK{secrets.token_hex(4)}", auth_header=auth_header))
                    continue
                if code in ("180", "183"):
                    continue
                if code == "200":
                    to_header = resp_headers.get("to", "")
                    m = re.search(r'tag=([^;>\s]+)', to_header)
                    to_tag = m.group(1) if m else None
                    remote_contact = resp_headers.get("contact", "")
                    answered_pts = _parse_offered_payload_types(_extract_sip_body(resp))
                    if answered_pts:
                        rtp.set_payload_type(_choose_payload_type(answered_pts))
                    send_ack(to_header, cseq)
                    with self.lock:
                        if self.call and self.call["call_id"] == call_id:
                            self.call["status"] = "connected"
                            self.call["to_tag"] = to_tag
                            self.call["rtp"] = rtp
                            if remote_contact:
                                self.call["remote_contact"] = _extract_contact_uri(remote_contact)
                    if line.media_relay_enabled:
                        rtp.send_pcm(np.zeros(rtp.samples_per_packet, dtype=np.int16))
                    timer = threading.Timer(config.max_call_duration, self.hangup)
                    timer.daemon = True
                    with self.lock:
                        if self.call and self.call["call_id"] == call_id:
                            self.call["bye_timer"] = timer
                    timer.start()
                    connected = True
                    self.on_call_connected(call_id=call_id, direction="outbound", rtp=rtp)
                    return
                if code and code[0] in "456":
                    # RFC 3261 requires ACKing every final non-2xx response -
                    # without it the gateway keeps retransmitting it and
                    # holds the transaction open, causing spurious "486 Busy
                    # Here" on subsequent calls until it times out on its own.
                    to_header = resp_headers.get("to", "")
                    send_ack(to_header, cseq)
                    self.on_call_failed(call_id=call_id, reason=status_line)
                    break
            if not connected:
                self.on_call_failed(call_id=call_id, reason="timeout")
        finally:
            self.transport.close_waiter(call_id)
            if not connected:
                rtp.close()
                with self.lock:
                    if self.call and self.call["call_id"] == call_id:
                        self.call = None
