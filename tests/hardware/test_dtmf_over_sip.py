#!/usr/bin/env python3
"""Presses keys down a real call and checks the bridge read them.

Nobody has to be present and nothing has to have a keypad: the second SIP
account places the call, a signaling client answers it the way a person
would, and the caller then sends the key presses itself as RTP events.
What the bridge made of them is read back out of its journal.

This is the path a caller-driven room choice would run on - the events
have to survive the gateway and the media relay, not just a loopback.

Usage: SIP_PHONE2_PASS=... test_dtmf_over_sip.py [digits] [extension]
"""

import os
import pathlib
import sys

# The daemon's modules are installed separately from these scripts.
sys.path.insert(0, os.environ.get("BRIDGE_CODE")
                or str(pathlib.Path(__file__).resolve().parent.parent.parent / "bridge"))

import subprocess
import threading
import time

DIGITS = sys.argv[1] if len(sys.argv) > 1 else "4711*#"
EXTENSION = sys.argv[2] if len(sys.argv) > 2 else "**621"
ANSWER_TIMEOUT = 40

from config import config                                   # noqa: E402
from sip_call import CallManager                            # noqa: E402
from sip_registrar import SipRegistrar                      # noqa: E402
from sip_transport import SipTransport                      # noqa: E402
from test_call_lifecycle import SignalingClient, bridge_status   # noqa: E402
from test_human_call import build_caller                    # noqa: E402

connected = threading.Event()
rtp_holder = {}


def on_connected(*, call_id, direction, rtp):
    print(f"[test] answered - codec {rtp.payload_type}, key presses as "
          f"payload type {rtp.dtmf_payload_type}", flush=True)
    rtp_holder["rtp"] = rtp
    connected.set()


def digits_in_journal(since: str) -> str:
    """What the bridge logged, in order - the only place the result of a
    key press currently shows up."""
    out = subprocess.run(["journalctl", "-u", "talk-sip-bridge", "--since", since,
                          "--no-pager", "-o", "cat"], capture_output=True, text=True).stdout
    return "".join(line.split("DTMF ", 1)[1].split()[0]
                   for line in out.splitlines() if "DTMF " in line)


def main() -> int:
    line = build_caller()
    manager = CallManager(line, on_call_connected=on_connected)
    holder = {}
    registrar = SipRegistrar(lambda: holder["t"], line)
    holder["t"] = SipTransport(manager, line)
    manager.transport = holder["t"]
    human = None
    started = time.strftime("%H:%M:%S")

    try:
        if not registrar.turn_on():
            print("[test] could not register the calling account")
            return 1
        print(f"[test] calling {EXTENSION} and answering it from a script", flush=True)
        if manager.dial(EXTENSION).get("error"):
            return 1

        human = SignalingClient(config.lines[0].default_room_token, in_call=True)
        deadline = time.time() + ANSWER_TIMEOUT
        while time.time() < deadline and not connected.is_set():
            if not human.is_alive() and (bridge_status()["active_call"] or {}).get("status") == "ringing":
                human.start()
            time.sleep(1)
        if not connected.wait(timeout=10):
            print("[test] the call never connected")
            return 1

        rtp = rtp_holder["rtp"]
        if rtp.dtmf_payload_type is None:
            print("[test] FAIL  the far end negotiated no telephone-event - nothing to send")
            return 1

        time.sleep(1)
        print(f"[test] pressing {DIGITS}", flush=True)
        for digit in DIGITS:
            rtp.send_dtmf(digit)
            time.sleep(0.3)
        time.sleep(1)

        seen = digits_in_journal(started)
        print(f"[test] the bridge read: {seen or '(nothing)'}")
        if seen == DIGITS:
            print("[test] PASS  every key press arrived exactly once, in order")
            return 0
        print(f"[test] FAIL  expected {DIGITS}")
        return 1
    finally:
        if human is not None:
            human.stop.set()
        try:
            manager.hangup()
        except Exception:
            pass
        registrar.turn_off()
        holder["t"].close()


if __name__ == "__main__":
    sys.exit(main())
