#!/usr/bin/env python3
"""Does a line come back by itself after its gateway goes away?

A gateway that reboots, or a relay that blinks, costs one refresh. What
used to happen then was that the keepalive ended and the line stayed
unreachable until somebody toggled it by hand - silently, because
everything else kept running.

Drives that with the Asterisk test peer, which can be stopped and started
at will, unlike a phone gateway:

    cd ../test-peer && docker compose up -d
    BRIDGE_SIP_USER=bridge-a BRIDGE_SIP_PASS=test-peer-secret \\
    BRIDGE_GATEWAY_HOST=127.0.0.1 BRIDGE_LOCAL_IP=127.0.0.1 \\
    BRIDGE_LOCAL_SIP_PORT=5062 BRIDGE_LOCAL_RTP_PORT=41500 \\
    BRIDGE_WS_URL=ws://127.0.0.1/ BRIDGE_INTERNAL_SECRET=x \\
    BRIDGE_BACKEND_URL=http://127.0.0.1 BRIDGE_STATE_DIR= \\
    BRIDGE_REGISTER_EXPIRES=20 BRIDGE_SIP_RESPONSE_TIMEOUT=3 \\
    python3 test_registration_recovery.py

BRIDGE_REGISTER_EXPIRES=20 is what makes this take a minute instead of
ten: refreshes then fall every 12 seconds.
"""

import os
import pathlib
import subprocess
import sys
import time

# The daemon's modules are installed separately from these scripts.
sys.path.insert(0, os.environ.get("BRIDGE_CODE")
                or str(pathlib.Path(__file__).resolve().parent.parent.parent / "bridge"))

from config import config
from sip_registrar import SipRegistrar
from sip_transport import SipTransport

PEER_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "test-peer"
results = []


def record(name, ok, detail=""):
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def peer(*args):
    subprocess.run(["docker-compose", *args], cwd=PEER_DIR, capture_output=True)


def wait_until(condition, timeout, poll=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(poll)
    return False


class NoCalls:
    def handle_invite(self, *a): pass
    def handle_bye(self, *a): pass
    def handle_cancel(self, *a): pass
    def handle_options(self, *a): pass


line = config.lines[0]
holder = {}
registrar = SipRegistrar(lambda: holder["t"], line)
holder["t"] = SipTransport(NoCalls(), line)

print("\n== registration recovery ==", flush=True)
record("registers to begin with", registrar.turn_on(), registrar.last_error or "")
if not registrar.registered:
    sys.exit(1)

print("  stopping the peer ...", flush=True)
peer("stop")
record("notices the gateway is gone",
      wait_until(lambda: not registrar.registered, timeout=60),
      "within one refresh")

record("keeps the line switched on while it is unreachable", registrar.wanted)
record("keeps trying rather than giving up",
       registrar.keepalive_thread is not None and registrar.keepalive_thread.is_alive())

print("  starting the peer again ...", flush=True)
peer("start")
record("comes back by itself, with nobody touching it",
       wait_until(lambda: registrar.registered, timeout=90),
       "no toggle, no restart")

registrar.turn_off()
record("switching off still stops it", not registrar.registered and not registrar.wanted)
time.sleep(1)
record("and the keepalive ends with it",
       registrar.keepalive_thread is None or not registrar.keepalive_thread.is_alive())

print(f"\n{sum(results)}/{len(results)} checks passed", flush=True)
sys.exit(0 if all(results) else 1)
