"""Minimal RTP session (send + receive) for G.711 (PCMU) and G.722, thread-
based (matches the existing thread model in bridge/daemon.py - there's a
small async adapter for the aiortc side in talk_client.py).
"""
import os
import queue
import socket
import struct
import threading

from g711 import linear_to_ulaw, ulaw_to_linear
from g722 import G722Decoder, G722Encoder
import numpy as np

RTP_VERSION = 2
PT_PCMU = 0
PT_G722 = 9

# 20ms per packet for both codecs. G.722 carries twice as many audio samples
# per packet as PCMU (16kHz vs 8kHz) but the RTP timestamp still advances by
# the PCMU-equivalent amount - see g722.py's module docstring.
_CODEC_INFO = {
    PT_PCMU: {"sample_rate": 8000, "samples_per_packet": 160},
    PT_G722: {"sample_rate": 16000, "samples_per_packet": 320},
}
RTP_CLOCK_INCREMENT = 160

# For backwards compatibility with anything still importing the old name.
SAMPLES_PER_PACKET = _CODEC_INFO[PT_PCMU]["samples_per_packet"]


class RtpSession:
    def __init__(self, local_ip: str, local_port: int, remote_ip: str, remote_port: int, payload_type: int = PT_PCMU):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((local_ip, local_port))
        self.sock.settimeout(0.5)
        self.remote_addr = (remote_ip, remote_port)
        self.seq = 0
        self.timestamp = 0
        self.ssrc = int.from_bytes(os.urandom(4), "big")
        self.recv_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.set_payload_type(payload_type)
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

    def _encode(self, chunk: np.ndarray) -> bytes:
        if self.payload_type == PT_G722:
            return self._encoder.encode(chunk)
        return linear_to_ulaw(chunk).tobytes()

    def _decode(self, payload: bytes) -> np.ndarray:
        if self.payload_type == PT_G722:
            return self._decoder.decode(payload)
        return ulaw_to_linear(np.frombuffer(payload, dtype=np.uint8))

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

    def _recv_loop(self):
        while not self.stop_event.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if addr[0] != self.remote_addr[0]:
                # Only accept media from the configured peer - otherwise any
                # host able to reach this port could inject audio into an
                # active call.
                continue
            if len(data) < 12:
                continue
            payload = data[12:]
            pcm = self._decode(payload)
            self.recv_queue.put(pcm)

    def close(self):
        self.stop_event.set()
        self.sock.close()
