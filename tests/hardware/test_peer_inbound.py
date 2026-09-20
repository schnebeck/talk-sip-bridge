"""Registers with the Asterisk test peer, waits for it to call in, answers
and checks the audio path - the inbound half, without Talk.

This one needs a second command to make the call happen. Start it, wait
for the line READY, then from the test peer:

    docker exec sip-test-peer \
        asterisk -rx "channel originate PJSIP/bridge-a extension 100@from-bridge"

Environment as in test_peer_outbound.py's docstring. Exits non-zero if any
check fails.
"""

import os
import pathlib
import sys

# The daemon's modules are installed separately from these scripts.
sys.path.insert(0, os.environ.get("BRIDGE_CODE")
                or str(pathlib.Path(__file__).resolve().parent.parent.parent / "bridge"))

import queue
import sys
import threading
import time

import numpy as np

from config import config
from sip_call import CallManager
from sip_registrar import SipRegistrar
from sip_transport import SipTransport

events = queue.Queue()
results = []


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def wait_for(kind, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=max(0.05, deadline - time.monotonic()))
        except queue.Empty:
            return None
        if event[0] == kind:
            return event
    return None


line = config.lines[0]
manager = CallManager(
    line,
    on_incoming_call=lambda **kw: events.put(("incoming", kw)),
    on_call_connected=lambda **kw: events.put(("connected", kw)),
    on_call_ended=lambda **kw: events.put(("ended", kw)),
    on_call_failed=lambda **kw: events.put(("failed", kw)),
)
holder = {}
registrar = SipRegistrar(lambda: holder["t"], line)
holder["t"] = SipTransport(manager, line)
manager.transport = holder["t"]

print("\n== inbound call ==", flush=True)
record("registers", registrar.turn_on(), registrar.last_error or "")
print("READY", flush=True)

event = wait_for("incoming", 45)
record("incoming INVITE arrives", event is not None,
       event[1].get("caller", "")[:60] if event else "nothing came in")

if event:
    record("answering succeeds", manager.answer())
    connected = wait_for("connected", 10)
    record("call reaches connected", connected is not None)
    if connected:
        rtp = connected[1]["rtp"]
        record("codec negotiated is G.722", rtp.payload_type == 9, f"payload type {rtp.payload_type}")
        # Without this, the two audio checks below can pass with the peer
        # switched off: audio sent to this line's own address comes back
        # to this line, at the right frequency, proving nothing.
        record("the peer's own media address is used",
               rtp.remote_addr != (line.local_ip, line.local_rtp_port), str(rtp.remote_addr))
        while not rtp.recv_queue.empty():
            rtp.recv_queue.get_nowait()
        tone = (np.sin(2 * np.pi * 440 * np.arange(rtp.sample_rate) / rtp.sample_rate) * 8000).astype(np.int16)
        stop = threading.Event()

        def send_tone():
            chunk = rtp.samples_per_packet
            index = 0
            while not stop.is_set():
                rtp.send_pcm(tone[index:index + chunk])
                index = (index + chunk) % (len(tone) - chunk)
                time.sleep(0.02)

        threading.Thread(target=send_tone, daemon=True).start()
        received = []
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and len(received) < 50:
            try:
                received.append(rtp.recv_queue.get(timeout=0.5))
            except queue.Empty:
                pass
        stop.set()
        record("audio flows in both directions", len(received) >= 25, f"{len(received)} packets")
        if received:
            audio = np.concatenate(received).astype(np.float64)
            spectrum = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
            dominant = np.fft.rfftfreq(len(audio), 1 / rtp.sample_rate)[spectrum.argmax()]
            record("what comes back is what was sent", abs(dominant - 440) < 25,
                   f"dominant {dominant:.0f} Hz")
        manager.hangup()
        record("hangup ends it", wait_for("ended", 5) is not None)

registrar.turn_off()
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n{passed}/{len(results)} checks passed", flush=True)
sys.exit(0 if passed == len(results) else 1)
