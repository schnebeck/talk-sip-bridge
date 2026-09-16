"""Minimal RTP session (send + receive) for G.711/PCMU, synchronous/thread-
based (matches the existing thread model in bridge/daemon.py - there's a
small async adapter for the aiortc side in sip_to_talk_bridge.py).
"""
import os
import queue
import socket
import struct
import threading

from g711 import linear_to_ulaw, ulaw_to_linear
import numpy as np

RTP_VERSION = 2
PT_PCMU = 0
SAMPLES_PER_PACKET = 160  # 20ms at 8000Hz


class RtpSession:
    def __init__(self, local_ip: str, local_port: int, remote_ip: str, remote_port: int):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((local_ip, local_port))
        self.sock.settimeout(0.5)
        self.remote_addr = (remote_ip, remote_port)
        self.seq = 0
        self.timestamp = 0
        self.ssrc = int.from_bytes(os.urandom(4), "big")
        self.recv_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.recv_thread.start()

    def send_pcm(self, pcm: np.ndarray):
        """pcm: int16 array of any length - split into 20ms packets (160 samples)."""
        for i in range(0, len(pcm), SAMPLES_PER_PACKET):
            chunk = pcm[i:i + SAMPLES_PER_PACKET]
            if len(chunk) < SAMPLES_PER_PACKET:
                chunk = np.pad(chunk, (0, SAMPLES_PER_PACKET - len(chunk)))
            payload = linear_to_ulaw(chunk).tobytes()
            header = struct.pack(
                "!BBHII",
                (RTP_VERSION << 6),
                PT_PCMU,
                self.seq & 0xFFFF,
                self.timestamp & 0xFFFFFFFF,
                self.ssrc,
            )
            self.sock.sendto(header + payload, self.remote_addr)
            self.seq += 1
            self.timestamp += SAMPLES_PER_PACKET

    def _recv_loop(self):
        while not self.stop_event.is_set():
            try:
                data, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) < 12:
                continue
            payload = data[12:]
            pcm = ulaw_to_linear(np.frombuffer(payload, dtype=np.uint8))
            self.recv_queue.put(pcm)

    def close(self):
        self.stop_event.set()
        self.sock.close()
