#!/usr/bin/env python3
"""Measures audio quality over the real phone path, end to end.

test_audio_quality.py feeds its signal straight into the publish path over
a local loopback, which measures what this bridge does to audio but not
what survives the gateway, the SIP proxy and the media relay. This places
an actual call from a second SIP account, answers it the way a person
would, plays the same speech-shaped signal down the phone line, and
records what a Talk client receives at the other end.

The calling line needs a media relay pipe of its own: a pipe carries one
call, and the bridge's own line is using the other one for this very call.

Usage: test_audio_over_sip.py <sip-phone2-password> [extension]
"""

import os
import pathlib
import sys

# The daemon's modules are installed separately from these scripts.
sys.path.insert(0, os.environ.get("BRIDGE_CODE")
                or str(pathlib.Path(__file__).resolve().parent.parent.parent / "bridge"))

import asyncio
import sys
import threading
import time

import numpy as np

sys.path.insert(0, "/opt/fritzbox-talk-bridge")

from config import config, LineConfig
from sip_call import CallManager
from sip_registrar import SipRegistrar
from sip_transport import SipTransport
from test_audio_quality import build_signal, report, subscribe_and_record
from test_call_lifecycle import SignalingClient, bridge_status
from talk_client import TalkClient
import websockets

EXTENSION = sys.argv[2] if len(sys.argv) > 2 else "**621"
ROOM = config.lines[0].default_room_token
BURSTS = 10

# The second pipe of the media relay - the bridge's own line holds the
# first one for the call this places.
RELAY_LAN_HOST = "192.168.1.10"
RELAY_LAN_PORT = 40010
RELAY_OVERLAY_HOST = "10.1.1.5"
RELAY_OVERLAY_PORT = 40011


def build_caller(password: str):
    line = LineConfig.__new__(LineConfig)
    line.id = "audio_test"
    line.local_ip = config.lines[0].local_ip
    line.sip_user = "sip-phone2"
    line.sip_pass = password
    line.gateway_host = config.lines[0].gateway_host
    line.proxy_host = config.lines[0].proxy_host
    line.proxy_port = config.lines[0].proxy_port
    line.sip_transport = config.lines[0].sip_transport
    line.contact_transport = config.lines[0].contact_transport
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
    line.relay_lan_host = RELAY_LAN_HOST
    line.relay_lan_port = RELAY_LAN_PORT
    line.relay_overlay_host = RELAY_OVERLAY_HOST
    line.relay_overlay_port = RELAY_OVERLAY_PORT

    manager = CallManager(line, on_call_connected=on_connected)
    holder = {}
    registrar = SipRegistrar(lambda: holder["t"], line)
    holder["t"] = SipTransport(manager, line)
    manager.transport = holder["t"]
    return manager, registrar, holder["t"]


connected = threading.Event()
rtp_holder = {}


def on_connected(*, call_id, direction, rtp):
    print(f"[test] call connected, codec payload type {rtp.payload_type} at {rtp.sample_rate} Hz", flush=True)
    rtp_holder["rtp"] = rtp
    connected.set()


def play(rtp, signal):
    spp = rtp.samples_per_packet
    for i in range(0, len(signal) - spp, spp):
        rtp.send_pcm(signal[i:i + spp])
        time.sleep(spp / rtp.sample_rate)


async def main():
    caller, registrar, transport = build_caller(sys.argv[1])
    human = None
    try:
        if not registrar.turn_on():
            print("Could not register the calling account.")
            return 1
        print(f"[test] calling {EXTENSION}: {caller.dial(EXTENSION)}", flush=True)

        human = SignalingClient(ROOM, in_call=True)
        deadline = time.time() + 25
        while time.time() < deadline and not human.is_alive():
            if (bridge_status()["active_call"] or {}).get("status") == "ringing":
                human.start()
                human.joined.wait(timeout=10)
                print("[test] answered", flush=True)
            time.sleep(1)

        if not connected.wait(timeout=25):
            print("[test] the call never connected")
            return 1

        rtp = rtp_holder["rtp"]
        signal = build_signal(rtp.sample_rate, BURSTS)
        threading.Thread(target=play, args=(rtp, signal), daemon=True).start()

        # Subscribe to what the bridge publishes for this call, as a Talk
        # client would, and record it.
        client = TalkClient(call_manager=None)
        async with websockets.connect(config.ws_url) as ws:
            client.ws = ws
            client.loop = asyncio.get_event_loop()
            await client._hello()
            publisher = None
            deadline = asyncio.get_event_loop().time() + 20
            while asyncio.get_event_loop().time() < deadline and publisher is None:
                # The bridge publishes under its own session; find it by
                # joining the room and looking at who is in the call.
                await asyncio.sleep(1)
                publisher = find_bridge_session()
            if publisher is None:
                print("[test] could not identify the bridge's publishing session")
                return 1
            print(f"[test] recording from {publisher[:12]}...", flush=True)
            pcm, rate = await subscribe_and_record(publisher, len(signal) / rtp.sample_rate - 3)
        ok = report(pcm, rate)
        return 0 if ok else 1
    finally:
        if human is not None:
            human.stop.set()
        try:
            caller.hangup()
        except Exception:
            pass
        registrar.turn_off()
        transport.close()


def find_bridge_session():
    """The bridge's own signaling session id, read from its log line for
    this call - it is the publisher a Talk client would subscribe to."""
    import subprocess
    out = subprocess.run(["journalctl", "-u", "fritzbox-talk-bridge", "--since", "-2min",
                          "--no-pager", "-o", "cat"], capture_output=True, text=True).stdout
    session = None
    for line in out.splitlines():
        if "Connected as internal client, session " in line:
            session = line.rsplit("session ", 1)[1].strip()
    return session


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
