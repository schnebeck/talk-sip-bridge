#!/usr/bin/env python3
"""End-to-end check of an inbound call's lifecycle, without a person.

Drives a real call through the running bridge and asserts what has to be
true at each step. Answering is simulated by a second signaling client
joining the room: an internal client that does not claim the
"internal-incall" feature is put in the call by the server as it joins,
which is exactly the transition the bridge watches for.

Joining over the OCS API alone does not work for this - confirmed live:
such a session is a participant to Nextcloud but never appears in the
signaling server's participant updates, so nothing the bridge can see
changes.

Checked:
  1. ringing     - the bridge starts a call in the room for the phone call
  2. accept      - a session joining that call makes the bridge answer SIP
  3. hangup      - the caller hanging up ends the call in Talk as well,
                   leaving nobody in it and no call for a client to ring on

Needs the bridge service running and a second SIP account to call from.
Usage: test_call_lifecycle.py <sip-phone2-password> [extension]
"""
import asyncio
import base64
import http.cookiejar
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import websockets

sys.path.insert(0, "/opt/fritzbox-talk-bridge")

from config import config, LineConfig
import sip_core
from test_publish_and_verify import internal_hello


class SimulatedHuman(threading.Thread):
    """A Talk client as far as the signaling server is concerned: joining a
    room as a plain internal client makes the server mark the session as
    being in the call with audio, which is what a person answering looks
    like from the bridge's side."""

    daemon = True

    def __init__(self, roomid: str):
        super().__init__()
        self.roomid = roomid
        self.joined = threading.Event()
        self.stop = threading.Event()

    def run(self):
        asyncio.run(self._main())

    async def _main(self):
        async with websockets.connect(config.ws_url) as ws:
            await internal_hello(ws, config.internal_secret, config.backend_url)
            await ws.send(json.dumps({"id": "human-room", "type": "room",
                                      "room": {"roomid": self.roomid}}))
            self.joined.set()
            while not self.stop.is_set():
                try:
                    await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    return

EXTENSION = sys.argv[2] if len(sys.argv) > 2 else "**621"
CONTROL = f"http://{config.control_bind}:{config.control_port}"
ROOM = config.lines[0].default_room_token


class Ocs:
    """One Talk session of the notify account, like a client would hold."""

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
        with self.opener.open(req, timeout=8) as resp:
            return json.loads(resp.read())["ocs"]["data"]

    def join_room(self):
        return self.request("POST", f"/ocs/v2.php/apps/spreed/api/v4/room/{ROOM}/participants/active", {})

    def join_call(self):
        return self.request("POST", f"/ocs/v2.php/apps/spreed/api/v4/call/{ROOM}", {"flags": 3})

    def leave_call(self):
        return self.request("DELETE", f"/ocs/v2.php/apps/spreed/api/v4/call/{ROOM}?all=false")

    def leave_room(self):
        return self.request("DELETE", f"/ocs/v2.php/apps/spreed/api/v4/room/{ROOM}/participants/active")

    def room(self):
        return self.request("GET", f"/ocs/v2.php/apps/spreed/api/v4/room/{ROOM}")

    def participants(self):
        return self.request("GET", f"/ocs/v2.php/apps/spreed/api/v4/room/{ROOM}/participants")


def bridge_status():
    with urllib.request.urlopen(CONTROL + "/status", timeout=5) as r:
        return json.loads(r.read())


def wait_for(label: str, predicate, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                print(f"  PASS  {label} (after {timeout - (deadline - time.time()):.1f}s)")
                return True
        except Exception as e:
            print(f"  ....  {label}: {e!r}")
        time.sleep(1)
    print(f"  FAIL  {label} (waited {timeout:.0f}s)")
    return False


def main():
    watcher = Ocs(os.environ["BRIDGE_NOTIFY_USER"], os.environ["BRIDGE_NOTIFY_APP_PASSWORD"])
    human_client = None

    room = watcher.room()
    if room.get("hasCall"):
        print(f"Room {ROOM} already has a call - clean that up first, the test needs a quiet room.")
        return 1
    if bridge_status()["lines"][0]["active_call"]:
        print("The bridge already has an active call - not starting another one.")
        return 1

    line = LineConfig.__new__(LineConfig)
    line.id = "lifecycle_test"
    line.local_ip = config.lines[0].local_ip
    line.sip_user = "sip-phone2"
    line.sip_pass = sys.argv[1]
    line.gateway_host = config.lines[0].gateway_host
    line.proxy_host = config.lines[0].proxy_host
    line.proxy_port = config.lines[0].proxy_port
    line.local_sip_port = 5099
    line.local_rtp_port = 41000
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

    caller = sip_core.CallManager(line)
    holder = {}
    registrar = sip_core.SipRegistrar(lambda: holder["t"], line)
    holder["t"] = sip_core.SipTransport(caller, line)
    caller.transport = holder["t"]

    ok = True
    try:
        if not registrar.turn_on():
            print("Could not register the calling account.")
            return 1
        print(f"Calling {EXTENSION} ...")
        print(f"  dial: {caller.dial(EXTENSION)}")

        ok &= wait_for("1. the bridge starts a call in the room for the phone call",
                       lambda: watcher.room().get("hasCall") is True, 20)

        human_client = SimulatedHuman(ROOM)
        human_client.start()
        human_client.joined.wait(timeout=10)
        print("  (a second signaling client joined the room, standing in for a person answering)")

        ok &= wait_for("2. the bridge answers the phone call",
                       lambda: (bridge_status()["lines"][0]["active_call"] or {}).get("status") == "connected", 20)

        time.sleep(3)
        print("Caller hangs up ...")
        caller.hangup()

        ok &= wait_for("3a. the bridge releases the phone call",
                       lambda: bridge_status()["lines"][0]["active_call"] is None, 20)
        ok &= wait_for("3b. the call in Talk ends with it",
                       lambda: watcher.room().get("hasCall") is False, 25)
        ok &= wait_for("3c. nobody is left in the call",
                       lambda: all(p.get("inCall") == 0 for p in watcher.participants()), 25)
    finally:
        try:
            human_client.stop.set()
        except Exception:
            pass
        try:
            if bridge_status()["lines"][0]["active_call"]:
                caller.hangup()
        except Exception:
            pass
        registrar.turn_off()

    print("RESULT:", "SUCCESS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
