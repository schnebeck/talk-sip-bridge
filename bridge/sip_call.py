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
from sip_sdp import (CODEC_NAMES, SUPPORTED, answer_sdp, choose_payload_type,
                     extract_sip_body, offer_sdp, parse_offered_payload_types,
                     parse_sdp_media_address, parse_telephone_event_type)


# How long a hangup waits to hear that it arrived. Long enough for a
# gateway on the same LAN, short enough that the thread is gone before
# anyone looks.
BYE_RESPONSE_TIMEOUT = 3.0


class _OutboundAttempt:
    """What one outbound INVITE keeps across its retries: which CSeq it is
    on, and the branch of the request currently outstanding - a CANCEL has
    to repeat that branch, or it cancels nothing."""

    def __init__(self, number: str, call_id: str, from_tag: str, sdp: str):
        self.number = number
        self.call_id = call_id
        self.from_tag = from_tag
        self.sdp = sdp
        self.cseq = 1
        self.branch = sip_requests.new_branch()


class CallManager:
    """Bound to one LineConfig; holds at most one active call on that line.
    Real audio (via RtpSession) is set up for both inbound and outbound
    calls; talk_client.py reads from/writes to call["rtp"] to bridge it into
    a Talk room."""

    def __init__(self, line, *, on_incoming_call=None, on_call_connected=None,
                 on_call_ended=None, on_call_failed=None, on_dtmf=None):
        self.line = line
        self.lock = threading.Lock()
        self.call = None
        self.transport = None
        self.on_incoming_call = on_incoming_call or (lambda **kw: None)
        self.on_call_connected = on_call_connected or (lambda **kw: None)
        self.on_call_ended = on_call_ended or (lambda **kw: None)
        self.on_call_failed = on_call_failed or (lambda **kw: None)
        # Key presses during a call. Nothing in this bridge acts on them
        # yet; they are carried out so a caller-driven room choice can be
        # built on top without touching the SIP layer again.
        self.on_dtmf = on_dtmf or (lambda **kw: None)

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

    def _new_rtp_session(self, payload_type: int = PT_PCMU,
                         dtmf_payload_type: int = None, call_id: str = None) -> RtpSession:
        line = self.line

        def dtmf(digit):
            print(f"[call:{line.id}] DTMF {digit} on {call_id}")
            self.on_dtmf(call_id=call_id, digit=digit)

        if line.media_relay_enabled:
            return RtpSession(line.local_ip, line.local_rtp_port, line.relay_overlay_host,
                              line.relay_overlay_port, payload_type=payload_type,
                              dtmf_payload_type=dtmf_payload_type, on_dtmf=dtmf)
        return RtpSession(line.local_ip, line.local_rtp_port, line.gateway_host,
                          line.local_rtp_port, payload_type=payload_type,
                          dtmf_payload_type=dtmf_payload_type, on_dtmf=dtmf)

    def handle_invite(self, text, headers, call_id, remote_addr):
        body = extract_sip_body(text)
        offered_pts = parse_offered_payload_types(body)
        dtmf_pt = parse_telephone_event_type(body)
        with self.lock:
            if self.call is not None:
                self._send_response("486 Busy Here", headers, remote_addr, to_tag=f"bridge{secrets.token_hex(3)}")
                return
            to_tag = f"bridge{secrets.token_hex(3)}"
            self.call = {"call_id": call_id, "headers": headers, "remote_addr": remote_addr,
                         "status": "ringing", "bye_timer": None, "to_tag": to_tag, "rtp": None,
                         "offered_pts": offered_pts, "dtmf_pt": dtmf_pt}
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
            if payload_type is None:
                # Answering with a codec the caller never offered gives a
                # call that connects and carries nothing, with no error to
                # see. Refusing says what happened, to them and to us.
                offered = self.call.get("offered_pts") or []
                print(f"[call:{line.id}] No codec in common for {call_id} - "
                      f"caller offered {offered}, this bridge speaks {list(SUPPORTED)}")
                self._send_response("488 Not Acceptable Here", headers, remote_addr,
                                    to_tag=to_tag)
                self.call = None
                return False
            dtmf_pt = self.call.get("dtmf_pt")
            rtp = self._new_rtp_session(payload_type, dtmf_pt, call_id)
            self.call["status"] = "connected"
            self.call["rtp"] = rtp
        sdp, _, _ = answer_sdp(line, line.local_rtp_port, payload_type, dtmf_pt)
        # What the two sides settled on, once per call: a gateway that was
        # offered no telephone-event turns key presses into audible tones
        # instead of passing them on, and that is indistinguishable from a
        # caller who pressed nothing.
        print(f"[call:{line.id}] Answering {call_id} with "
              f"{CODEC_NAMES.get(payload_type, payload_type)}, key presses "
              + (f"as events (payload type {dtmf_pt})" if dtmf_pt
                 else "NOT negotiated - the caller offered no telephone-event"))
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
        self._watch_bye(call["call_id"])
        self.on_call_ended(call_id=call["call_id"], reason="local_hangup")

    def _watch_bye(self, call_id: str):
        """Says so when a hangup is not acknowledged.

        A BYE goes to the peer's Contact, and for SIP over TCP that address
        is the source port of the peer's own connection to us. Once that
        connection is gone the address exists nowhere and the BYE cannot be
        delivered - the call is over here and still running at the far end,
        with a person holding a handset nobody is on. Keepalives keep the
        connection alive so this does not happen; this notices when it did
        anyway."""
        waiter = self.transport.open_waiter(call_id)

        def wait():
            try:
                response = waiter.get(timeout=BYE_RESPONSE_TIMEOUT)
            except queue.Empty:
                response = None
            finally:
                self.transport.close_waiter(call_id)
            if response is None:
                print(f"[call:{self.line.id}] No answer to the BYE for {call_id} - "
                      f"the far end may still think this call is up")

        threading.Thread(target=wait, daemon=True).start()

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

    # -- placing a call ---------------------------------------------------
    def _send_invite(self, attempt, auth_header: str = None):
        self.transport.send(sip_requests.build_invite(
            self.line, number=attempt.number, call_id=attempt.call_id,
            from_tag=attempt.from_tag, branch=attempt.branch, cseq=attempt.cseq,
            sdp=attempt.sdp, auth_header=auth_header))

    def _send_ack(self, attempt, to_header: str):
        self.transport.send(sip_requests.build_ack(
            self.line, number=attempt.number, call_id=attempt.call_id,
            from_tag=attempt.from_tag, branch=sip_requests.new_branch(),
            cseq=attempt.cseq, to_header=to_header))

    def _retry_invite_with_auth(self, attempt, code: str, headers: dict):
        """Answers a challenge with a fresh INVITE. It is a new transaction,
        so it gets the next CSeq and its own branch."""
        line = self.line
        challenge = parse_auth_challenge(
            headers.get("www-authenticate" if code == "401" else "proxy-authenticate", ""))
        realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")
        uri = f"sip:{attempt.number}@{line.gateway_host}"
        digest = digest_response(line.sip_user, realm, line.sip_pass, "INVITE", uri, nonce)
        attempt.cseq += 1
        attempt.branch = sip_requests.new_branch()
        self._send_invite(attempt, sip_requests.authorization_header(
            line.sip_user, realm, uri, nonce, digest))

    def _cancel_outbound(self, attempt):
        """Abandoning a pending INVITE requires cancelling it - RFC 3261 -
        and the CANCEL has to repeat the branch of the request it cancels.
        Without it the far end goes on ringing long past our own timeout,
        as an unanswered test call showed."""
        self.transport.send(sip_requests.build_cancel(
            self.line, number=attempt.number, call_id=attempt.call_id,
            from_tag=attempt.from_tag, branch=attempt.branch, cseq=attempt.cseq))

    def _relevant_responses(self, waiter, deadline, attempt):
        """Yields the responses that say something, and drops the ones that
        do not: provisional 100/180/183, and anything answering a CSeq this
        transaction has already moved past. UDP can deliver a duplicate 401
        late, and acting on it starts a second INVITE while the first is
        still open - which the gateway then rejects with "491 Request
        Pending"."""
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            try:
                resp = waiter.get(timeout=remaining)
            except queue.Empty:
                return
            status_line = resp.split("\r\n", 1)[0]
            parts = status_line.split(" ", 2)
            code = parts[1] if len(parts) > 1 else ""
            headers = parse_sip_headers(resp)
            answered_cseq = headers.get("cseq", "").split(" ")[0]
            if answered_cseq.isdigit() and int(answered_cseq) != attempt.cseq:
                continue
            if code in ("100", "180", "183"):
                continue
            yield code, status_line, headers, resp

    def _connect_outbound(self, attempt, resp: str, headers: dict, rtp):
        """Turns an answered INVITE into a running call: settle the codec
        and where media goes, acknowledge, and record the dialog."""
        line = self.line
        to_header = headers.get("to", "")
        match = re.search(r'tag=([^;>\s]+)', to_header)
        answer_body = extract_sip_body(resp)

        answered_pts = parse_offered_payload_types(answer_body)
        chosen = choose_payload_type(answered_pts) if answered_pts else None
        if chosen is not None:
            rtp.set_payload_type(chosen)
        elif answered_pts:
            # We offered only what we speak, so this means the far end
            # answered with something else entirely. Say so rather than
            # carrying on with whatever the session was built with.
            print(f"[call:{line.id}] Answer for {attempt.call_id} picked {answered_pts}, "
                  f"none of which this bridge speaks")
        # Key presses come under the number the far end named in its answer.
        rtp.dtmf_payload_type = parse_telephone_event_type(answer_body)
        if not line.media_relay_enabled:
            # Without a relay the peer's own advertised media address is the
            # only correct target - the one this session was built with is a
            # guess that holds only when the gateway happens to use the same
            # port on its own address.
            media = parse_sdp_media_address(answer_body)
            if media:
                rtp.remote_addr = media

        self._send_ack(attempt, to_header)

        remote_contact = headers.get("contact", "")
        timer = threading.Timer(config.max_call_duration, self.hangup)
        timer.daemon = True
        with self.lock:
            if self.call and self.call["call_id"] == attempt.call_id:
                self.call["status"] = "connected"
                self.call["to_tag"] = match.group(1) if match else None
                self.call["rtp"] = rtp
                self.call["bye_timer"] = timer
                if remote_contact:
                    self.call["remote_contact"] = extract_contact_uri(remote_contact)
        if line.media_relay_enabled:
            rtp.send_pcm(np.zeros(rtp.samples_per_packet, dtype=np.int16))  # prime the relay
        timer.start()
        self.on_call_connected(call_id=attempt.call_id, direction="outbound", rtp=rtp)

    def _outbound_worker(self, number, call_id, from_tag):
        """Places one outbound call and follows it to its end: connected,
        refused, or given up on."""
        line = self.line
        # Payload types settle once the answer is read; the call id is
        # known now and is what a key press has to be reported against.
        rtp = self._new_rtp_session(call_id=call_id)
        sdp, _, _ = offer_sdp(line, line.local_rtp_port)
        attempt = _OutboundAttempt(number, call_id, from_tag, sdp)

        waiter = self.transport.open_waiter(call_id)
        self._send_invite(attempt)
        print(f"[call:{line.id}] Outbound call started to {number}")

        deadline = time.time() + config.outbound_call_timeout
        connected = refused = False
        try:
            for code, status_line, headers, resp in self._relevant_responses(waiter, deadline, attempt):
                if code in ("401", "407"):
                    self._retry_invite_with_auth(attempt, code, headers)
                elif code == "200":
                    self._connect_outbound(attempt, resp, headers, rtp)
                    connected = True
                    return
                elif code and code[0] in "456":
                    # RFC 3261 requires acknowledging every final non-2xx.
                    # Without it the gateway keeps retransmitting and holds
                    # the transaction open, which shows up as spurious "486
                    # Busy Here" on later calls until it times out by itself.
                    self._send_ack(attempt, headers.get("to", ""))
                    refused = True
                    self.on_call_failed(call_id=call_id, reason=status_line)
                    break
            if not connected and not refused:
                self._cancel_outbound(attempt)
                self.on_call_failed(call_id=call_id, reason="timeout")
        finally:
            self.transport.close_waiter(call_id)
            if not connected:
                rtp.close()
                with self.lock:
                    if self.call and self.call["call_id"] == call_id:
                        self.call = None
