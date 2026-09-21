# talk-sip-bridge - tests/hardware/fake_gateway.py
# A SIP gateway made of one UDP socket, for tests that need a caller nobody
# has to dial.
#
#   Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
#   Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
#   Written by Anthropic Claude Opus 5 - AI generated content.
#
#   Free software under the GNU General Public License, version 3 or later.
#   There is no warranty, to the extent permitted by law. The full text is
#   in LICENSES/GPL-3.0-or-later.txt.
#
# SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
# SPDX-FileContributor: Anthropic Claude Opus 5 (AI generated content)
# SPDX-License-Identifier: GPL-3.0-or-later

"""A SIP gateway made of one UDP socket, for tests that need a caller
nobody has to dial.

Every other script here calls through the real gateway from a second
registered account, which means a test can only exercise what a phone
line is currently arranged to do. A number the bridge answers on behalf
of Nextcloud cannot be arranged that way at all on a line that also
carries a person's own number: the call would have to arrive addressed
to a different number than the one the box delivers.

So this plays the other side instead. It sends an INVITE addressed to
whatever number the test wants, answers what comes back, and carries RTP
through the bridge's own RtpSession. Nothing here registers, because
nothing needs to: registration only tells a gateway where to send calls,
and this one already knows.

It is a test peer, not a SIP stack - one call at a time, no
authentication, no retransmissions, no forking.
"""
import os
import pathlib
import queue
import re
import secrets
import socket
import sys
import threading
import time

sys.path.insert(0, os.environ.get("BRIDGE_CODE")
                or str(pathlib.Path(__file__).resolve().parent.parent.parent / "bridge"))

from payload_types import PT_G722, PT_PCMU
from rtp import RtpSession
from sip_messages import extract_contact_uri, parse_sip_headers
from sip_sdp import extract_sip_body, parse_sdp_media_address, parse_telephone_event_type


class FakeGateway:
    """One socket, one call. `invite()` places it, `bye()` ends it."""

    def __init__(self, local_ip: str, sip_port: int, rtp_port: int):
        self.local_ip = local_ip
        self.sip_port = sip_port
        self.rtp_port = rtp_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((local_ip, sip_port))
        self.responses = []
        self.requests = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.call = None
        threading.Thread(target=self._listen, daemon=True).start()

    # -- the socket ------------------------------------------------------

    def _listen(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65536)
            except OSError:
                return
            text = data.decode(errors="replace")
            if not text.strip():
                continue
            if text.startswith("SIP/2.0"):
                with self._lock:
                    self.responses.append(text)
                continue
            with self._lock:
                self.requests.append(text)
            self._answer_request(text, addr)

    def _answer_request(self, text: str, addr):
        """200 OK to anything the bridge sends in-dialog - a BYE when it
        hangs up first, an OPTIONS keepalive. Echoing the headers back is
        all a peer has to do to be believed."""
        headers = parse_sip_headers(text)
        via = headers.get("via") or []
        lines = [f"Via: {v}" for v in via] + [
            f"From: {headers.get('from', '')}",
            f"To: {headers.get('to', '')}",
            f"Call-ID: {headers.get('call-id', '')}",
            f"CSeq: {headers.get('cseq', '')}",
            "Content-Length: 0",
        ]
        self.sock.sendto(("SIP/2.0 200 OK\r\n" + "\r\n".join(lines) + "\r\n\r\n").encode(), addr)

    def _send(self, message: str, to):
        self.sock.sendto(message.encode(), to)

    def _wait_response(self, call_id: str, wanted: range, timeout: float):
        """The next response for this call whose status is in `wanted` -
        a 100 and a 180 arrive first and are not what a caller waits for."""
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            with self._lock:
                pending = self.responses[seen:]
                seen = len(self.responses)
            for text in pending:
                headers = parse_sip_headers(text)
                if headers.get("call-id") != call_id:
                    continue
                status = int(text.split(" ")[1])
                if status in wanted:
                    return status, text
            time.sleep(0.05)
        return None, None

    # -- the call --------------------------------------------------------

    def invite(self, bridge: tuple, dialled: str, caller: str, caller_name: str = None,
               timeout: float = 30.0, codec: int = PT_PCMU):
        """Calls `dialled` at the bridge and waits for it to be answered.

        Returns (status, seconds, rtp) - rtp is an RtpSession pointed at
        whatever the answer's SDP named, or None if the call was not
        answered with one. PCMU is the only codec offered: what a codec
        does to audio is measured elsewhere, and one codec keeps what
        this test asserts about the answer unambiguous."""
        call_id = f"fakegw-{secrets.token_hex(6)}@{self.local_ip}"
        tag = secrets.token_hex(4)
        name, rate = {PT_PCMU: ("PCMU", 8000), PT_G722: ("G722", 8000)}[codec]
        sdp = "\r\n".join([
            "v=0", f"o=- 0 0 IN IP4 {self.local_ip}", "s=-",
            f"c=IN IP4 {self.local_ip}", "t=0 0",
            f"m=audio {self.rtp_port} RTP/AVP {codec} 101",
            f"a=rtpmap:{codec} {name}/{rate}",
            "a=rtpmap:101 telephone-event/8000",
            "a=fmtp:101 0-15",
            "a=sendrecv", ""])
        request = "\r\n".join([
            f"INVITE sip:{dialled}@{bridge[0]}:{bridge[1]} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{secrets.token_hex(6)}",
            "Max-Forwards: 70",
            f'From: "{caller_name or caller}" <sip:{caller}@{self.local_ip}>;tag={tag}',
            f"To: <sip:{dialled}@{self.local_ip}>",
            f"Call-ID: {call_id}",
            "CSeq: 1 INVITE",
            f"Contact: <sip:{caller}@{self.local_ip}:{self.sip_port}>",
            "User-Agent: talk-sip-bridge test gateway",
            "Content-Type: application/sdp",
            f"Content-Length: {len(sdp)}",
            "", sdp])
        self.call = {"call_id": call_id, "tag": tag, "caller": caller, "dialled": dialled,
                     "bridge": bridge, "to_tag": "", "contact": "", "cseq": 1}
        started = time.monotonic()
        self._send(request, bridge)
        status, text = self._wait_response(call_id, range(200, 700), timeout)
        elapsed = time.monotonic() - started
        if status is None:
            return None, elapsed, None
        headers = parse_sip_headers(text)
        tag_of_theirs = re.search(r";tag=([^;\s]+)", headers.get("to", ""))
        self.call["to_tag"] = tag_of_theirs.group(1) if tag_of_theirs else ""
        self.call["contact"] = extract_contact_uri(headers.get("contact", ""))
        if status != 200:
            return status, elapsed, None
        self._ack()
        body = extract_sip_body(text)
        remote = parse_sdp_media_address(body)
        if not remote:
            return status, elapsed, None
        rtp = RtpSession(self.local_ip, self.rtp_port, remote[0], remote[1], payload_type=codec,
                         dtmf_payload_type=parse_telephone_event_type(body))
        self.call["rtp"] = rtp
        return status, elapsed, rtp

    def _in_dialog(self, method: str, uri: str, cseq: int) -> str:
        call = self.call
        return "\r\n".join([
            f"{method} {uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{secrets.token_hex(6)}",
            "Max-Forwards: 70",
            f'From: "{call["caller"]}" <sip:{call["caller"]}@{self.local_ip}>;tag={call["tag"]}',
            f'To: <sip:{call["dialled"]}@{self.local_ip}>;tag={call["to_tag"]}',
            f'Call-ID: {call["call_id"]}',
            f"CSeq: {cseq} {method}",
            f'Contact: <sip:{call["caller"]}@{self.local_ip}:{self.sip_port}>',
            "Content-Length: 0", "", ""])

    def keypad(self, digits: str, gap: float = 0.25):
        """Types on the caller's keypad, as events (RFC 4733). A real
        handset also leaves the tones in the audio; the bridge reads
        either, and a press reported twice is collapsed."""
        rtp = self.call["rtp"]
        for digit in digits:
            rtp.send_dtmf(digit)
            time.sleep(gap)

    def heard(self, seconds: float, arrivals: list = None):
        """What the far end sent in that time, as the caller's own
        session decoded it.

        `arrivals` collects the moment each packet was taken off the
        wire. A caller hears the gaps between packets, not the samples
        in them: a sender that stalls once a second sounds like a click
        once a second while every packet is perfect."""
        chunks = []
        deadline = time.monotonic() + seconds
        rtp = self.call["rtp"]
        while time.monotonic() < deadline:
            try:
                chunk = rtp.recv_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            chunks.append(chunk)
            if arrivals is not None:
                arrivals.append(time.monotonic())
        return chunks

    def received(self, method: str) -> bool:
        """Whether the bridge sent this request for the current call -
        a BYE says it ended the call itself."""
        with self._lock:
            requests = list(self.requests)
        return any(text.startswith(method + " ") and self.call["call_id"] in text
                   for text in requests)

    def _dialog_uri(self) -> str:
        """Where in-dialog requests go: the Contact the answer gave, which
        is the only address that identifies this dialog's remote end."""
        return self.call["contact"] or f'sip:{self.call["dialled"]}@{self.call["bridge"][0]}'

    def _ack(self):
        # An ACK repeats the INVITE's CSeq number rather than taking the
        # next one.
        self._send(self._in_dialog("ACK", self._dialog_uri(), self.call["cseq"]),
                   self.call["bridge"])

    def bye(self, timeout: float = 5.0):
        """Hangs up and reports whether the bridge acknowledged it."""
        if not self.call:
            return None
        if self.call.get("rtp"):
            self.call["rtp"].close()
        self.call["cseq"] += 1
        self._send(self._in_dialog("BYE", self._dialog_uri(), self.call["cseq"]),
                   self.call["bridge"])
        status, _ = self._wait_response(self.call["call_id"], range(200, 700), timeout)
        return status

    def close(self):
        self._stop.set()
        if self.call and self.call.get("rtp"):
            self.call["rtp"].close()
        self.sock.close()
