#!/usr/bin/env python3
"""Direct dial-in, end to end, with no phone and no second line.

A dial-in number is one the bridge answers on behalf of Nextcloud: the
INVITE says which number was called, Nextcloud creates a conversation for
that one call with the caller as a real participant, and the bridge picks
up into it. Testing that over the real gateway would need a line that
delivers a different number than the one a person's own phone rings on,
and a deployment with a single line has no such thing.

So the gateway is played here (see fake_gateway.py) and only the phone
network is left out. Everything else is the deployed path: this line's
CallManager, the running Nextcloud, its signaling server, the real
`direct-dial-in` endpoint, and real RTP carried through the bridge's own
sessions.

What it asserts, in the order it happens:

  the number is routed as the bridge's own    not to the line's room
  Nextcloud creates the conversation          token, and the caller in it
  the bridge answers without being told to    a mapped number is its own
  the audio reaches Talk                      recorded off the publisher
  the hangup is acknowledged                  and the call is forgotten

Runs on the bridge host, with the deployment's environment, alongside the
running daemon - its own ports, its own signaling connection, its own
line. It never touches the production line and never calls the gateway.

    set -a; . /etc/talk-sip-bridge/env; set +a
    <the deployment venv>/bin/python3 tests/hardware/test_dialin_answer.py

DIALIN_TEST_NUMBER is the number Nextcloud has in `talk_phone_numbers`
(`occ talk:phone-number:add`); the extension the fake gateway announces is
invented here, exactly as a real gateway invents its own.
"""

import os
import pathlib
import sys

sys.path.insert(0, os.environ.get("BRIDGE_CODE")
                or str(pathlib.Path(__file__).resolve().parent.parent.parent / "bridge"))

import asyncio
import time

import numpy as np

from config import config, LineConfig
from sip_call import CallManager
from sip_transport import SipTransport
import talk_client

from fake_gateway import FakeGateway
from test_audio_quality import subscribe_and_record

# The number Nextcloud maps to an account, and what the gateway claims to
# have been dialled to reach it. The two differ on purpose: that is the
# whole reason the mapping exists.
NUMBER = os.environ.get("DIALIN_TEST_NUMBER", "4930621")
EXTENSION = os.environ.get("DIALIN_TEST_EXTENSION", "**900")
CALLER = os.environ.get("DIALIN_TEST_CALLER", "+4930622")

# Ports nothing else on this host holds: the daemon has 5091/40000, and
# its test scripts 5093/41000 and the relay pipes.
GW_SIP_PORT = int(os.environ.get("DIALIN_TEST_GW_SIP_PORT", "5097"))
GW_RTP_PORT = int(os.environ.get("DIALIN_TEST_GW_RTP_PORT", "41400"))
LINE_SIP_PORT = int(os.environ.get("DIALIN_TEST_SIP_PORT", "5096"))
LINE_RTP_PORT = int(os.environ.get("DIALIN_TEST_RTP_PORT", "41300"))

TONE_HZ = 440
RECORD_SECONDS = 6

results = []


def record_result(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def build_line() -> LineConfig:
    """The line under test: registered nowhere, reachable only from this
    host, and mapping one number to dial-in.

    Built from the environment like every other line rather than by
    filling in attributes, so it keeps up with LineConfig. Its default
    room is deliberately empty - if the call ever took the ordinary path,
    there would be no room to hide in and the test says so instead of
    quietly passing."""
    settings = {
        "SIP_USER": "dialin-test",
        "SIP_PASS": "unused-nothing-registers",
        "GATEWAY_HOST": config.local_ip,
        "PROXY_HOST": config.local_ip,
        "PROXY_PORT": str(GW_SIP_PORT),
        "SIP_TRANSPORT": "udp",
        "LOCAL_SIP_PORT": str(LINE_SIP_PORT),
        "LOCAL_RTP_PORT": str(LINE_RTP_PORT),
        "DEFAULT_ROOM": "",
        "DIALIN_NUMBERS": f"{EXTENSION}={NUMBER}",
    }
    for key, value in settings.items():
        os.environ["DIALINTEST_" + key] = value
    return LineConfig("dialin_test", config.local_ip, env_prefix="DIALINTEST_")


def start_bridge(line):
    """The daemon's own wiring, minus the registrar: nothing has to
    register to receive a call from a peer that already knows where to
    send it."""
    call_manager = CallManager(line)
    client = talk_client.start_in_background(call_manager)
    call_manager.on_incoming_call = client.on_incoming_call
    call_manager.on_call_connected = client.on_call_connected
    call_manager.on_call_ended = client.on_call_ended
    call_manager.on_call_failed = client.on_call_failed
    call_manager.transport = SipTransport(call_manager, line)
    return client, call_manager


def wait_for(condition, timeout: float, interval: float = 0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    return None


def call_entry(client):
    with client._call_sessions_lock:
        return next(iter(client._call_sessions.values()), None)


def play(rtp, samples, stop_after: float):
    """A tone down the phone leg, paced against a fixed schedule so the
    stream arrives at the rate it claims (see test_human_call.py)."""
    spp = rtp.samples_per_packet
    interval = spp / rtp.sample_rate
    due = time.monotonic()
    deadline = due + stop_after
    index = 0
    while time.monotonic() < deadline:
        rtp.send_pcm(samples[index:index + spp])
        index = (index + spp) % (len(samples) - spp)
        due += interval
        remaining = due - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)


def dominant_frequency(audio: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
    return float(np.fft.rfftfreq(len(audio), 1 / rate)[spectrum.argmax()])


async def main() -> int:
    print(f"\n== direct dial-in: {CALLER} calls {EXTENSION}, which Nextcloud knows "
          f"as {NUMBER} ==", flush=True)
    if not config.sip_shared_secret:
        print("BRIDGE_SIP_SHARED_SECRET is not set - there is no dial-in to test.")
        return 2

    line = build_line()
    gateway = FakeGateway(config.local_ip, GW_SIP_PORT, GW_RTP_PORT)
    client, call_manager = start_bridge(line)
    try:
        session = wait_for(lambda: client.own_sessionid, 20)
        record_result("the bridge reaches the signaling server", session, session or "no session")
        if not session:
            return 1

        status, elapsed, rtp = await asyncio.to_thread(
            gateway.invite, (line.local_ip, line.local_sip_port), EXTENSION, CALLER)
        record_result("the call is answered", status == 200,
                      f"{status} after {elapsed:.1f}s" if status else "no final response")
        if status != 200 or rtp is None:
            return 1

        entry = wait_for(lambda: call_entry(client), 5)
        room = entry.roomid if entry else ""
        record_result("Nextcloud made a conversation for the call", bool(room),
                      room or "none - the call took the line's own room")
        actor = (entry.actor or {}) if entry else {}
        record_result("the caller is a participant of it",
                      bool(actor.get("actorType") and actor.get("actorId")),
                      f"{actor.get('actorType')}/{str(actor.get('actorId'))[:12]}"
                      if actor else "no actor - a session belonging to nobody")

        # Talk's side of the same audio: what a client in that conversation
        # would hear, recorded off the publisher the bridge just opened.
        tone = (np.sin(2 * np.pi * TONE_HZ * np.arange(rtp.sample_rate) / rtp.sample_rate)
                * 8000).astype(np.int16)
        playing = asyncio.create_task(asyncio.to_thread(play, rtp, tone, RECORD_SECONDS + 12))
        heard, rate = await subscribe_and_record(session, RECORD_SECONDS)
        await playing
        if len(heard):
            frequency = dominant_frequency(heard.astype(np.float64), rate)
            record_result("the tone reaches Talk", abs(frequency - TONE_HZ) < 25,
                          f"dominant {frequency:.0f} Hz of {len(heard) / rate:.1f}s")
        else:
            record_result("the tone reaches Talk", False, "nothing was published")

        record_result("the hangup is acknowledged", gateway.bye() == 200)
        record_result("the call is forgotten", wait_for(
            lambda: call_entry(client) is None, 10) is not None)
    finally:
        try:
            call_manager.hangup()
        except Exception:
            pass
        gateway.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed", flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
