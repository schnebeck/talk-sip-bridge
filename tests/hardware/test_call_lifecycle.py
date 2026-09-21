#!/usr/bin/env python3
# talk-sip-bridge - tests/hardware/test_call_lifecycle.py
# End-to-end check of an inbound call's lifecycle, without a person.
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

"""End-to-end check of an inbound call's lifecycle, without a person.

Drives real calls through the running bridge and asserts what has to be
true at each step:

  1. ringing  - the bridge starts a call in the room for the phone call
  2. accept   - a session joining that call makes the bridge answer SIP
  3. hangup   - the caller hanging up ends the call in Talk as well,
                leaving nobody in it and no call for a client to ring on

The second run repeats all of it with bystanders in the room: one session
that is in the call the whole time (a client the signaling server still
lists long after its user is gone - this is what used to make the bridge
answer instantly against a dead peer) and one that sits in the room
without joining the call (Talk open in a browser tab). Answering has to
keep working with both of them there, and must not happen before the
person actually joins.

People are simulated by signaling clients, not OCS sessions: confirmed
live that a session which joins over the OCS API alone is a participant
to Nextcloud but never appears in the signaling server's participant
updates, so the bridge cannot see it at all.

Needs the bridge service running and a second SIP account to call from.
Usage: test_call_lifecycle.py <sip-phone2-password> [extension]
"""

import os
import pathlib
import sys

# The daemon's modules are installed separately from these scripts.
sys.path.insert(0, os.environ.get("BRIDGE_CODE")
                or str(pathlib.Path(__file__).resolve().parent.parent.parent / "bridge"))

import asyncio
import base64
import hashlib
import hmac
import http.cookiejar
import json
import os
import secrets
import sys
import threading
import time
import urllib.request

import websockets


from config import config, LineConfig
from sip_call import CallManager
from sip_registrar import SipRegistrar
from sip_transport import SipTransport

EXTENSION = sys.argv[2] if len(sys.argv) > 2 else "**621"
CONTROL = f"http://{config.control_bind}:{config.control_port}"
ROOM = config.lines[0].default_room_token


class SignalingClient(threading.Thread):
    """A Talk client as far as the signaling server is concerned.

    An internal client that does not claim "internal-incall" is put in the
    call by the server as it joins a room, which is what a person answering
    looks like from the bridge's side. Claiming the feature and never
    setting the flags is the opposite: present in the room, not in the
    call - a browser tab sitting on the conversation."""

    daemon = True

    def __init__(self, roomid: str, in_call: bool):
        super().__init__()
        self.roomid = roomid
        self.in_call = in_call
        self.joined = threading.Event()
        self.stop = threading.Event()

    def run(self):
        asyncio.run(self._main())

    async def _main(self):
        try:
            async with websockets.connect(config.ws_url) as ws:
                random_str = secrets.token_hex(32)
                token = hmac.new(config.internal_secret.encode(), random_str.encode(),
                                 hashlib.sha256).hexdigest()
                await ws.send(json.dumps({
                    "id": "sim-hello", "type": "hello",
                    "hello": {"version": "1.0",
                              "features": [] if self.in_call else ["internal-incall"],
                              "auth": {"type": "internal",
                                       "params": {"random": random_str, "token": token,
                                                  "backend": config.backend_url}}}}))
                await ws.recv()
                await ws.recv()
                await ws.send(json.dumps({"id": "sim-room", "type": "room",
                                          "room": {"roomid": self.roomid}}))
                self.joined.set()
                while not self.stop.is_set():
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
        except Exception as e:
            print(f"  (simulated client ended: {e!r})")


class Ocs:
    """Reads room state the way Nextcloud itself sees it."""

    def __init__(self, user: str, password: str):
        self.base = config.backend_url.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"OCS-APIREQUEST": "true", "Accept": "application/json", "Authorization": self.auth}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        with self.opener.open(req, timeout=15) as resp:
            return json.loads(resp.read())["ocs"]["data"]

    def room(self):
        return self.request("GET", f"/ocs/v2.php/apps/spreed/api/v4/room/{ROOM}")

    def participants(self):
        return self.request("GET", f"/ocs/v2.php/apps/spreed/api/v4/room/{ROOM}/participants")


def bridge_status():
    with urllib.request.urlopen(CONTROL + "/status", timeout=5) as r:
        return json.loads(r.read())["lines"][0]


def wait_for(label: str, predicate, timeout: float) -> bool:
    started = time.time()
    while time.time() - started < timeout:
        try:
            if predicate():
                print(f"  PASS  {label} (after {time.time() - started:.1f}s)")
                return True
        except Exception as e:
            print(f"  ....  {label}: {e!r}")
        time.sleep(1)
    print(f"  FAIL  {label} (waited {timeout:.0f}s)")
    return False


def stays_false(label: str, predicate, seconds: float) -> bool:
    started = time.time()
    while time.time() - started < seconds:
        try:
            if predicate():
                print(f"  FAIL  {label} (happened after {time.time() - started:.1f}s)")
                return False
        except Exception:
            pass
        time.sleep(1)
    print(f"  PASS  {label} (held for {seconds:.0f}s)")
    return True


def build_caller(password: str, sip_port: int, rtp_port: int):
    line = LineConfig.__new__(LineConfig)
    line.id = "lifecycle_test"
    line.local_ip = config.lines[0].local_ip
    line.sip_user = "sip-phone2"
    line.sip_pass = password
    line.gateway_host = config.lines[0].gateway_host
    line.proxy_host = config.lines[0].proxy_host
    line.proxy_port = config.lines[0].proxy_port
    line.sip_transport = config.lines[0].sip_transport
    line.contact_transport = config.lines[0].contact_transport
    line.local_sip_port = sip_port
    line.local_rtp_port = rtp_port
    line.contact_host = ""
    line.contact_port = 0
    line.default_room_token = ""
    line.dialout_number_allowlist = ""
    line.dialout_strip_prefix = ""
    line.dialout_internal_dial_prefix = ""
    line.notify_user = ""
    line.notify_app_password = ""
    line.relay_lan_host = ""
    line.relay_lan_port = 0
    line.relay_overlay_host = ""
    line.relay_overlay_port = 0

    manager = CallManager(line)
    holder = {}
    registrar = SipRegistrar(lambda: holder["t"], line)
    holder["t"] = SipTransport(manager, line)
    manager.transport = holder["t"]
    return manager, registrar, holder["t"]


def run_call(watcher: Ocs, password: str, bystanders: bool, sip_port: int, rtp_port: int) -> bool:
    print(f"\n--- call {'with a stale in-call session and an idle one in the room' if bystanders else 'in a quiet room'} ---")
    standing_by = []
    if bystanders:
        for in_call in (True, False):
            client = SignalingClient(ROOM, in_call=in_call)
            client.start()
            client.joined.wait(timeout=10)
            standing_by.append(client)
        time.sleep(2)
        print("  (one session in the call, one only in the room - both there before the phone rings)")

    caller, registrar, transport = build_caller(password, sip_port, rtp_port)
    human = None
    ok = True
    try:
        if not registrar.turn_on():
            print("  Could not register the calling account.")
            return False
        print(f"  calling {EXTENSION}: {caller.dial(EXTENSION)}")

        ok &= wait_for("1. the bridge starts a call in the room for the phone call",
                       lambda: watcher.room().get("hasCall") is True, 20)
        ok &= stays_false("2a. it does not answer before anyone joins",
                          lambda: (bridge_status()["active_call"] or {}).get("status") == "connected", 6)

        human = SignalingClient(ROOM, in_call=True)
        human.start()
        human.joined.wait(timeout=10)
        print("  (a person answers)")

        ok &= wait_for("2b. the bridge answers the phone call",
                       lambda: (bridge_status()["active_call"] or {}).get("status") == "connected", 20)
        time.sleep(3)
        print("  caller hangs up")
        caller.hangup()

        ok &= wait_for("3a. the bridge releases the phone call",
                       lambda: bridge_status()["active_call"] is None, 20)
        ok &= wait_for("3b. the call in Talk ends with it",
                       lambda: watcher.room().get("hasCall") is False, 25)
        ok &= wait_for("3c. nobody is left in the call",
                       lambda: all(p.get("inCall") == 0 for p in watcher.participants()), 25)
    finally:
        for client in standing_by + ([human] if human else []):
            client.stop.set()
        try:
            if bridge_status()["active_call"]:
                caller.hangup()
        except Exception:
            pass
        registrar.turn_off()
        # SipTransport holds the line's SIP port for as long as it lives, and
        # the next run needs it back.
        transport.close()
        time.sleep(2)
    return ok


def main():
    watcher = Ocs(os.environ["BRIDGE_NOTIFY_USER"], os.environ["BRIDGE_NOTIFY_APP_PASSWORD"])
    if watcher.room().get("hasCall"):
        print(f"Room {ROOM} already has a call - the test needs a quiet room.")
        return 1
    if bridge_status()["active_call"]:
        print("The bridge already has an active call - not starting another one.")
        return 1

    ok = run_call(watcher, sys.argv[1], bystanders=False, sip_port=5099, rtp_port=41000)
    # Fresh ports: the previous run's transport can still hold its own for
    # a moment, and that has nothing to do with what is being tested.
    ok &= run_call(watcher, sys.argv[1], bystanders=True, sip_port=5098, rtp_port=41010)
    print("\nRESULT:", "SUCCESS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
