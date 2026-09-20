"""Call handling for one line, with real audio in both directions.

Each CallManager is bound to one LineConfig (see config.py) and holds at
most one active call on that line - a second incoming call on the same line
is rejected with "486 Busy Here". Running several lines concurrently means
constructing several independent (SipTransport, SipRegistrar, CallManager)
sets, one per line.

Contains no Talk-specific logic - callers pass callback hooks
(on_incoming_call, on_call_connected, on_call_ended, on_call_failed) so the
Talk-facing layer (talk_client.py) can react (addsession, publish audio,
etc.) without this module knowing anything about Talk.
"""
import queue
import re
import secrets
import threading
import time

import numpy as np

import sip_requests
from config import config
from payload_types import PT_PCMU
from rtp import RtpSession
from sip_messages import (VALID_NUMBER, digest_response, extract_contact_uri,
                          parse_auth_challenge, parse_sip_headers)
from sip_sdp import (answer_sdp, choose_payload_type, extract_sip_body,
                     offer_sdp, parse_offered_payload_types,
                     parse_sdp_media_address)


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
        self.transport.send(sip_requests.build_response(
            status_line, req_headers, extra_headers=extra_headers or (),
            body=body, to_tag=to_tag), remote_addr)

    def _new_rtp_session(self, payload_type: int = PT_PCMU) -> RtpSession:
        line = self.line
        if line.media_relay_enabled:
            return RtpSession(line.local_ip, line.local_rtp_port, line.relay_overlay_host, line.relay_overlay_port, payload_type=payload_type)
        return RtpSession(line.local_ip, line.local_rtp_port, line.gateway_host, line.local_rtp_port, payload_type=payload_type)

    def handle_invite(self, text, headers, call_id, remote_addr):
        offered_pts = parse_offered_payload_types(extract_sip_body(text))
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
            payload_type = choose_payload_type(self.call.get("offered_pts") or [])
            rtp = self._new_rtp_session(payload_type)
            self.call["status"] = "connected"
            self.call["rtp"] = rtp
        sdp, _, _ = answer_sdp(line, line.local_rtp_port, payload_type)
        self._send_response("200 OK", headers, remote_addr, extra_headers=[
            sip_requests.contact_header(line, with_transport=False),
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
                # A CANCEL can arrive just after the call was answered, when
                # the caller gives up in the same moment - the media session
                # created by answer() has to be released here too, or its
                # socket keeps this line's RTP port bound and the next call
                # cannot be answered at all ("Address already in use").
                if self.call.get("bye_timer"):
                    self.call["bye_timer"].cancel()
                if self.call.get("rtp"):
                    self.call["rtp"].close()
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
            self.transport.send(sip_requests.build_bye(
                line, request_uri=request_uri, call_id=call["call_id"],
                from_header=f"{sip_requests.address(line, line.sip_user)};tag={call['from_tag']}",
                to_header=f"{sip_requests.address(line, call['number'])};tag={call['to_tag']}",
                branch=sip_requests.new_branch()))
        else:
            headers = call["headers"]
            # Likewise, the caller's own Contact from their INVITE is the
            # correct in-dialog target - not the gateway's registrar address.
            caller_contact = headers.get("contact", "")
            request_uri = extract_contact_uri(caller_contact) if caller_contact else f"sip:{line.gateway_host}"
            self.transport.send(sip_requests.build_bye(
                line, request_uri=request_uri, call_id=call["call_id"],
                from_header=headers.get("to", ""), to_header=headers.get("from", ""),
                branch=sip_requests.new_branch()), call["remote_addr"])
        self.on_call_ended(call_id=call["call_id"], reason="local_hangup")

    def dial(self, number: str) -> dict:
        """number is already this line's own dial target (stripped/prefixed
        by the caller, e.g. talk_client.py's line-selection logic) - this
        only re-validates the allowlist, it does not re-derive the target."""
        line = self.line
        if not VALID_NUMBER.fullmatch(number):
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
        sdp, _, _ = offer_sdp(line, line.local_rtp_port)

        def build_invite(cseq, branch, auth_header=None):
            return sip_requests.build_invite(
                line, number=number, call_id=call_id, from_tag=from_tag,
                branch=branch, cseq=cseq, sdp=sdp, auth_header=auth_header)

        def send_ack(to_header, cseq):
            self.transport.send(sip_requests.build_ack(
                line, number=number, call_id=call_id, from_tag=from_tag,
                branch=sip_requests.new_branch(), cseq=cseq, to_header=to_header))

        cseq = 1
        last_branch = sip_requests.new_branch()
        q = self.transport.open_waiter(call_id)
        self.transport.send(build_invite(cseq, last_branch))
        print(f"[call:{line.id}] Outbound call started to {number}")
        deadline = time.time() + config.outbound_call_timeout
        connected = False
        final_response_received = False
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
                    challenge = parse_auth_challenge(resp_headers.get(auth_hdr_name, ""))
                    realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")
                    uri = f"sip:{number}@{line.gateway_host}"
                    resp_digest = digest_response(line.sip_user, realm, line.sip_pass, "INVITE", uri, nonce)
                    auth_header = (
                        f'Authorization: Digest username="{line.sip_user}", realm="{realm}", '
                        f'nonce="{nonce}", uri="{uri}", response="{resp_digest}", algorithm=MD5'
                    )
                    cseq += 1
                    last_branch = f"z9hG4bK{secrets.token_hex(4)}"
                    self.transport.send(build_invite(cseq, last_branch, auth_header=auth_header))
                    continue
                if code in ("180", "183"):
                    continue
                if code == "200":
                    to_header = resp_headers.get("to", "")
                    m = re.search(r'tag=([^;>\s]+)', to_header)
                    to_tag = m.group(1) if m else None
                    remote_contact = resp_headers.get("contact", "")
                    answer_body = extract_sip_body(resp)
                    answered_pts = parse_offered_payload_types(answer_body)
                    if answered_pts:
                        rtp.set_payload_type(choose_payload_type(answered_pts))
                    if not line.media_relay_enabled:
                        # Without a relay the peer's own advertised media
                        # address is the only correct target - the address
                        # this session was constructed with is a guess that
                        # holds only when the gateway happens to use the
                        # same port on its own address.
                        media = parse_sdp_media_address(answer_body)
                        if media:
                            rtp.remote_addr = media
                    send_ack(to_header, cseq)
                    with self.lock:
                        if self.call and self.call["call_id"] == call_id:
                            self.call["status"] = "connected"
                            self.call["to_tag"] = to_tag
                            self.call["rtp"] = rtp
                            if remote_contact:
                                self.call["remote_contact"] = extract_contact_uri(remote_contact)
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
                    final_response_received = True
                    self.on_call_failed(call_id=call_id, reason=status_line)
                    break
            if not connected and not final_response_received:
                # Gave up waiting (deadline reached, or no response at all
                # within the remaining time) without ever getting a final
                # response - RFC 3261 requires CANCELling a still-pending
                # INVITE transaction we're abandoning, or the gateway keeps
                # ringing/processing a call nobody is listening for anymore
                # (observed live: an unanswered test call kept the far end
                # busy well past our own local timeout, with nothing telling
                # it to stop).
                self.transport.send(sip_requests.build_cancel(
                    line, number=number, call_id=call_id, from_tag=from_tag,
                    branch=last_branch, cseq=cseq))
                self.on_call_failed(call_id=call_id, reason="timeout")
        finally:
            self.transport.close_waiter(call_id)
            if not connected:
                rtp.close()
                with self.lock:
                    if self.call and self.call["call_id"] == call_id:
                        self.call = None
