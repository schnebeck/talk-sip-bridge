# talk-sip-bridge - bridge/rtp.py
# A minimal thread-based RTP session, sending and receiving G.711 and G.722.
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

"""A minimal thread-based RTP session, sending and receiving G.711 and G.722.

Threads rather than asyncio, to match the model the rest of the SIP side
uses in bridge/daemon.py; the small async adapter for the aiortc side is
in talk_client.py.
"""
import errno
import os
import queue
import socket
import struct
import threading
import time
import traceback

from dtmf import EVENT_DIGITS, DigitGuard, DtmfEvents, parse_event
from dtmf_capture import DtmfCapture
from dtmf_inband import InbandDtmf
from g711 import alaw_to_linear, linear_to_alaw, linear_to_ulaw, ulaw_to_linear
from g722 import G722Decoder, G722Encoder
import numpy as np

from config import config
from payload_types import PT_G722, PT_PCMA, PT_PCMU  # re-exported: rtp.PT_* stays valid

RTP_VERSION = 2

# 20ms per packet for both codecs. G.722 carries twice as many audio samples
# per packet as PCMU (16kHz vs 8kHz) but the RTP timestamp still advances by
# the PCMU-equivalent amount - see g722.py's module docstring.
_CODEC_INFO = {
    PT_PCMU: {"sample_rate": 8000, "samples_per_packet": 160},
    PT_PCMA: {"sample_rate": 8000, "samples_per_packet": 160},
    PT_G722: {"sample_rate": 16000, "samples_per_packet": 320},
}
RTP_CLOCK_INCREMENT = 160

# For backwards compatibility with anything still importing the old name.
SAMPLES_PER_PACKET = _CODEC_INFO[PT_PCMU]["samples_per_packet"]

# How long a new session waits for its port, which the previous call on
# the same line may still be letting go of: closing a UDP socket does not
# free the port while a thread is still inside recvfrom on it, so the
# port stays taken for up to one receive timeout after the call ended.
# Measured: a rebind 50ms after the close fails, 600ms after it succeeds.
# Without this, hanging up and being called straight back answers with no
# audio path at all.
BIND_RETRY_SECONDS = 2.0
BIND_RETRY_INTERVAL = 0.05


def bind_socket(sock, address, timeout: float = BIND_RETRY_SECONDS, sleep=time.sleep,
                clock=time.monotonic):
    """Binds, waiting out a port the previous call has not finished
    releasing. Any other error, and a port still taken when the time is
    up, is raised - a line whose port belongs to something else should
    say so rather than retry forever."""
    deadline = clock() + timeout
    while True:
        try:
            sock.bind(address)
            return
        except OSError as e:
            if e.errno != errno.EADDRINUSE or clock() >= deadline:
                raise
            sleep(BIND_RETRY_INTERVAL)


# How long one recurring packet fault stays quiet after it was reported.
# Fifty packets a second means one fault is fifty journal lines a second.
PACKET_ERROR_QUIET = 5.0

class RtpSession:
    def __init__(self, local_ip: str, local_port: int, remote_ip: str, remote_port: int,
                 payload_type: int = PT_PCMU, dtmf_payload_type: int = None, on_dtmf=None,
                 call_id: str = None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        bind_socket(self.sock, (local_ip, local_port))
        self.sock.settimeout(0.5)
        self.remote_addr = (remote_ip, remote_port)
        self.seq = 0
        self.timestamp = 0
        self.ssrc = int.from_bytes(os.urandom(4), "big")
        self.recv_queue = queue.Queue()
        self.stop_event = threading.Event()
        self._reported_pt = None  # payload type already reported as unexpected
        self._failed_packets = 0
        self._last_packet_error = 0.0
        # Key presses arrive as their own payload type, negotiated per call
        # (RFC 4733). Without knowing its number they are indistinguishable
        # from a codec nobody agreed on, and get dropped as noise.
        self.dtmf_payload_type = dtmf_payload_type
        self.on_dtmf = on_dtmf or (lambda digit: None)
        self._dtmf = DtmfEvents()
        # Some gateways agree to events and then play the tones into the
        # audio anyway - this deployment's does, for every press its own
        # handset makes. Listening as well costs eight dot products per
        # packet and is the only way to read a key on such a gateway.
        self._inband = None
        self._guard = DigitGuard()
        # With dtmf_debug on, everything the far end sends is kept so the
        # tones it really produces can be measured afterwards - which is
        # the only way to tell "the detector missed it" from "it was
        # never sent". Capped, because a call has no length limit.
        self.set_payload_type(payload_type)
        # After the codec: a capture is written at the sample rate the
        # call settled on, which set_payload_type is what decides.
        self._capture = DtmfCapture(call_id, self.sample_rate)
        self.recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.recv_thread.start()

    def set_payload_type(self, payload_type: int):
        """Switches codec. Safe to call once right after construction,
        before real traffic starts flowing (e.g. once the SDP answer for an
        outbound call is known) - not designed for switching mid-call."""
        info = _CODEC_INFO[payload_type]
        self.payload_type = payload_type
        self.sample_rate = info["sample_rate"]
        self.samples_per_packet = info["samples_per_packet"]
        if payload_type == PT_G722:
            self._encoder = G722Encoder()
            self._decoder = G722Decoder()
        else:
            self._encoder = None
            self._decoder = None
        self._inband = InbandDtmf(self.sample_rate) if config.inband_dtmf else None

    def _encode(self, chunk: np.ndarray) -> bytes:
        if self.payload_type == PT_G722:
            return self._encoder.encode(chunk)
        if self.payload_type == PT_PCMA:
            return linear_to_alaw(chunk).tobytes()
        return linear_to_ulaw(chunk).tobytes()

    def _decode(self, payload: bytes, payload_type: int = None):
        """Decodes by the payload type the packet actually carries, not the
        one that was negotiated. A peer may send comfort noise, DTMF events
        or a codec that was not agreed; running those bytes through the
        negotiated decoder turns them into noise instead of audio, which is
        indistinguishable from a broken call at the far end. Returns None
        for anything this bridge cannot decode, so it can be dropped."""
        if payload_type is None:
            payload_type = self.payload_type
        if payload_type == PT_G722:
            if self._decoder is None:
                self._decoder = G722Decoder()
            return self._decoder.decode(payload)
        if payload_type == PT_PCMU:
            return ulaw_to_linear(np.frombuffer(payload, dtype=np.uint8))
        if payload_type == PT_PCMA:
            return alaw_to_linear(np.frombuffer(payload, dtype=np.uint8))
        return None

    def send_pcm(self, pcm: np.ndarray):
        """pcm: int16 array of any length, at this session's sample_rate -
        split into 20ms packets."""
        spp = self.samples_per_packet
        for i in range(0, len(pcm), spp):
            chunk = pcm[i:i + spp]
            if len(chunk) < spp:
                chunk = np.pad(chunk, (0, spp - len(chunk)))
            payload = self._encode(chunk)
            header = struct.pack(
                "!BBHII",
                (RTP_VERSION << 6),
                self.payload_type,
                self.seq & 0xFFFF,
                self.timestamp & 0xFFFFFFFF,
                self.ssrc,
            )
            self.sock.sendto(header + payload, self.remote_addr)
            self.seq += 1
            self.timestamp += RTP_CLOCK_INCREMENT

    def send_dtmf(self, digit: str, duration_ms: int = 200, interval_ms: int = 20):
        """Sends one key press as RTP events (RFC 4733).

        The far end expects a packet per interval for as long as the key is
        held, all under the timestamp the press started at, and the last
        one repeated with the end marker - that repetition is what survives
        a lost packet. The marker bit on the first packet says a new press
        begins; the timestamp advances only afterwards, because the press
        occupies that stretch of the timeline.
        """
        if self.dtmf_payload_type is None:
            raise RuntimeError("no telephone-event payload type was negotiated for this call")
        event = EVENT_DIGITS.find(digit.upper())
        if event < 0:
            raise ValueError(f"not a key that exists: {digit!r}")
        started_at = self.timestamp & 0xFFFFFFFF
        per_packet = int(self.sample_rate * interval_ms / 1000)
        packets = max(1, duration_ms // interval_ms)

        def send(index, samples, end):
            header = struct.pack("!BBHII", (RTP_VERSION << 6),
                                 (0x80 if index == 0 else 0) | self.dtmf_payload_type,
                                 self.seq & 0xFFFF, started_at, self.ssrc)
            self.sock.sendto(header + struct.pack("!BBH", event, (0x80 if end else 0) | 10,
                                                  samples), self.remote_addr)
            self.seq += 1

        for index in range(packets):
            send(index, per_packet * (index + 1), end=False)
            time.sleep(interval_ms / 1000)
        for _ in range(3):
            send(packets, per_packet * packets, end=True)
        self.timestamp = (started_at + per_packet * packets) & 0xFFFFFFFF

    def _recv_loop(self):
        while not self.stop_event.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                self._handle_packet(data, addr)
            except Exception as e:
                # One packet may not cost the call. This thread dying
                # leaves the caller hearing nothing for the rest of the
                # call, with the rest of the bridge none the wiser -
                # every other sign of a healthy call stays exactly as it
                # was. What arrives here is decoded from the wire, so it
                # is also the one place a malformed packet reaches.
                self._packet_failed(e)

    def _packet_failed(self, error: Exception):
        """Says so once, then counts. At fifty packets a second, one
        recurring fault would otherwise be the only thing in the
        journal."""
        self._failed_packets += 1
        now = time.monotonic()
        if now - self._last_packet_error < PACKET_ERROR_QUIET:
            return
        print(f"[rtp] Dropped a packet: {error!r}"
              + (f" ({self._failed_packets} so far this call)"
                 if self._failed_packets > 1 else ""))
        traceback.print_exc()
        self._last_packet_error = now

    def _handle_packet(self, data: bytes, addr):
        if addr[0] != self.remote_addr[0]:
            # Only accept media from the configured peer - otherwise any
            # host able to reach this port could inject audio into an
            # active call.
            return
        if len(data) < 12:
            return
        payload_type = data[1] & 0x7F
        if self.dtmf_payload_type is not None and payload_type == self.dtmf_payload_type:
            timestamp = struct.unpack("!I", data[4:8])[0]
            digit = self._dtmf.feed(timestamp, data[12:])
            if config.dtmf_debug:
                parsed = parse_event(data[12:])
                print(f"[rtp] event packet ts={timestamp} "
                      f"digit={parsed[0] if parsed else '?'} "
                      f"end={parsed[1] if parsed else '?'} "
                      f"-> {'reported' if digit else 'same press'}")
            if digit is not None:
                self.report_digit(digit)
            return
        if payload_type != self.payload_type and payload_type != self._reported_pt:
            print(f"[rtp] Receiving payload type {payload_type} while {self.payload_type} was negotiated")
            self._reported_pt = payload_type
        pcm = self._decode(data[12:], payload_type)
        if pcm is None:
            return
        if self._inband is not None:
            digit = self._inband.feed(pcm)
            if digit is not None:
                if config.dtmf_debug:
                    print(f"[rtp] inband heard {digit}")
                self.report_digit(digit)
        self._capture.add(pcm)
        self.recv_queue.put(pcm)

    def report_digit(self, digit: str):
        """One key press, however it arrived - as an RTP event, as a tone
        in the audio, or as a SIP INFO from outside this session. The
        same press arriving by two of those roads is one press; see
        dtmf.DigitGuard."""
        if not self._guard.accepts(digit, time.monotonic()):
            if config.dtmf_debug:
                print(f"[rtp] {digit} dropped as a repeat of the press just reported")
            return
        try:
            self.on_dtmf(digit)
        except Exception as e:
            print(f"[rtp] DTMF handler for {digit} failed: {e!r}")

    def close(self):
        """Ends the session and does not return until its port is free.

        Closing the socket alone does not free the port: the receive
        thread may be inside recvfrom, and the port stays taken until
        that call comes back. Waiting for the thread here is what lets
        the next call on this line bind the same port."""
        self.stop_event.set()
        self.sock.close()
        if self.recv_thread is not threading.current_thread():
            self.recv_thread.join(timeout=2.0)
        self._capture.write()

