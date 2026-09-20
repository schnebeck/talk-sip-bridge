"""Places real calls from the bridge's SIP stack to the Asterisk test peer
and checks what comes back, including the audio.

Needs no phone gateway and no signaling server - only the test peer, which
runs on the machine this runs on (see ../test-peer/README.md):

    cd ../test-peer && docker compose up -d

    BRIDGE_SIP_USER=bridge-a BRIDGE_SIP_PASS=test-peer-secret \
    BRIDGE_GATEWAY_HOST=127.0.0.1 BRIDGE_LOCAL_IP=127.0.0.1 \
    BRIDGE_LOCAL_SIP_PORT=5062 BRIDGE_LOCAL_RTP_PORT=41500 \
    BRIDGE_WS_URL=ws://127.0.0.1/ BRIDGE_INTERNAL_SECRET=x \
    BRIDGE_BACKEND_URL=http://127.0.0.1 BRIDGE_STATE_DIR= \
    BRIDGE_OUTBOUND_CALL_TIMEOUT=15 python3 test_peer_outbound.py

Exits non-zero if any check fails. test_peer_inbound.py is the other
direction.
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
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def wait_for(kind, timeout):
    """Next callback of this kind, or None."""
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
    on_call_connected=lambda **kw: events.put(("connected", kw)),
    on_call_ended=lambda **kw: events.put(("ended", kw)),
    on_call_failed=lambda **kw: events.put(("failed", kw)),
    on_incoming_call=lambda **kw: events.put(("incoming", kw)),
)
holder = {}
registrar = SipRegistrar(lambda: holder["t"], line)
holder["t"] = SipTransport(manager, line)
manager.transport = holder["t"]

print(f"\n== registration ==")
record("registers against the peer", registrar.turn_on(), registrar.last_error or "")
if not registrar.registered:
    sys.exit(1)

print(f"\n== outbound to 100 (answers and echoes) ==")
started = manager.dial("100")
record("dial accepted", "call_id" in started, started.get("error", ""))
event = wait_for("connected", 15)
record("call connects", event is not None)
if event:
    rtp = event[1]["rtp"]
    record("codec negotiated is G.722 (preferred)", rtp.payload_type == 9,
           f"payload type {rtp.payload_type}")

    # Send a tone and see whether the echo application sends it back.
    while not rtp.recv_queue.empty():
        rtp.recv_queue.get_nowait()
    tone = (np.sin(2 * np.pi * 440 * np.arange(rtp.sample_rate) / rtp.sample_rate) * 8000).astype(np.int16)
    sender_stop = threading.Event()

    def send_tone():
        chunk = rtp.samples_per_packet
        index = 0
        while not sender_stop.is_set():
            rtp.send_pcm(tone[index:index + chunk])
            index = (index + chunk) % (len(tone) - chunk)
            time.sleep(0.02)

    sender = threading.Thread(target=send_tone, daemon=True)
    sender.start()

    received = []
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and len(received) < 50:
        try:
            received.append(rtp.recv_queue.get(timeout=0.5))
        except queue.Empty:
            pass
    sender_stop.set()

    record("audio comes back from the echo application", len(received) >= 25,
           f"{len(received)} packets")
    if received:
        audio = np.concatenate(received).astype(np.float64)
        peak = int(np.abs(audio).max())
        spectrum = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
        dominant = np.fft.rfftfreq(len(audio), 1 / rtp.sample_rate)[spectrum.argmax()]
        record("echoed audio is the tone that was sent", abs(dominant - 440) < 25,
               f"peak {peak}, dominant {dominant:.0f} Hz")

    manager.hangup()
    record("hangup ends the call", wait_for("ended", 5) is not None)

time.sleep(1)
print(f"\n== outbound to 500 (rejects immediately) ==")
manager.dial("500")
event = wait_for("failed", 15)
record("busy is reported as a failure", event is not None,
       event[1].get("reason", "") if event else "no callback")

time.sleep(1)
print(f"\n== outbound to 400 (never answers) ==")
manager.dial("400")
event = wait_for("failed", 40)
record("unanswered call gives up", event is not None,
       event[1].get("reason", "") if event else "no callback")
record("and reports it as a timeout", bool(event) and event[1].get("reason") == "timeout")

time.sleep(1)
registrar.turn_off()

passed = sum(1 for _, ok, _ in results if ok)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
