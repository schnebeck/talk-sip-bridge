#!/usr/bin/env python3
# talk-sip-bridge - tests/hardware/test_dialin_ivr.py
# The conference dialogue against the running deployment, with no phone.
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

"""The conference dialogue against the running deployment, with no phone.

A caller rings a conference number, hears the announcement, keys in the
meeting id they were sent, and ends up in that conversation. This runs
that twice - once with the right number and once with wrong ones - and
measures each step, with fake_gateway.py playing the caller so no line
and no handset are involved.

What each case has to show:

  right number   answered at once, the announcement audible, the caller
                 a participant of exactly the conversation they named,
                 their audio arriving in it
  wrong numbers  three refusals and then a BYE from the bridge - a caller
                 must not be able to sit on the line guessing

Preconditions, both on the conversation being dialled: its token is all
digits (Talk generates those wherever SIP is configured) and SIP dial-in
is switched on for it. Without a PIN it admits guests; with a PIN, pass
the participant's PIN as the second argument and the caller joins as that
participant instead.

    set -a; . /etc/talk-sip-bridge/env; set +a
    <the deployment venv>/bin/python3 tests/hardware/test_dialin_ivr.py <meeting-id> [pin]

The caller's side of the audio is written to OUT_WAV, which is the only
way to judge what the announcement actually sounds like on a telephone.
"""

import os
import pathlib
import sys

sys.path.insert(0, os.environ.get("BRIDGE_CODE")
                or str(pathlib.Path(__file__).resolve().parent.parent.parent / "bridge"))

import asyncio
import time
import wave

import numpy as np

from config import config, LineConfig
from sip_call import CallManager
from sip_transport import SipTransport
import talk_client

from fake_gateway import FakeGateway
from test_audio_quality import subscribe_and_record

MEETING_ID = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("IVR_MEETING_ID", "")
PIN = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("IVR_PIN", "")
CONFERENCE_NUMBER = os.environ.get("IVR_CONFERENCE_NUMBER", "**900")
CALLER = os.environ.get("IVR_CALLER", "+4930622")
OUT_WAV = os.environ.get("OUT_WAV", f"/tmp/ivr-as-the-caller-hears-it-{int(time.time())}.wav")

GW_SIP_PORT = int(os.environ.get("IVR_GW_SIP_PORT", "5097"))
GW_RTP_PORT = int(os.environ.get("IVR_GW_RTP_PORT", "41400"))
LINE_SIP_PORT = int(os.environ.get("IVR_SIP_PORT", "5096"))
LINE_RTP_PORT = int(os.environ.get("IVR_RTP_PORT", "41300"))

LISTEN_BEFORE_TYPING = 3.0     # long enough to hear the prompt start
TONE_HZ = 440
RECORD_SECONDS = 5

results = []


def record_result(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def build_line() -> LineConfig:
    """A line whose only number is a conference number, registered
    nowhere and reachable only from this host."""
    settings = {
        "SIP_USER": "ivr-test",
        "SIP_PASS": "unused-nothing-registers",
        "GATEWAY_HOST": config.local_ip,
        "PROXY_HOST": config.local_ip,
        "PROXY_PORT": str(GW_SIP_PORT),
        "SIP_TRANSPORT": "udp",
        "LOCAL_SIP_PORT": str(LINE_SIP_PORT),
        "LOCAL_RTP_PORT": str(LINE_RTP_PORT),
        "DEFAULT_ROOM": "",
        "DIALIN_NUMBERS": "",
        "CONFERENCE_NUMBERS": CONFERENCE_NUMBER,
    }
    for key, value in settings.items():
        os.environ["IVRTEST_" + key] = value
    return LineConfig("ivr_test", config.local_ip, env_prefix="IVRTEST_")


def start_bridge(line):
    call_manager = CallManager(line)
    client = talk_client.start_in_background(call_manager)
    call_manager.on_incoming_call = client.on_incoming_call
    call_manager.on_call_connected = client.on_call_connected
    call_manager.on_call_ended = client.on_call_ended
    call_manager.on_call_failed = client.on_call_failed
    call_manager.on_dtmf = client.on_dtmf
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


def write_wav(path: str, chunks, rate: int):
    if not chunks:
        return ""
    with wave.open(path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(np.concatenate(chunks).astype(np.int16).tobytes())
    return path


def play(rtp, samples, seconds: float):
    spp = rtp.samples_per_packet
    interval = spp / rtp.sample_rate
    due = time.monotonic()
    deadline = due + seconds
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


async def right_number(gateway, client, line, session):
    """The case a caller is meant to be in."""
    status, elapsed, rtp = await asyncio.to_thread(
        gateway.invite, (line.local_ip, line.local_sip_port), CONFERENCE_NUMBER, CALLER)
    record_result("the call is answered without anyone being rung", status == 200,
                  f"{status} after {elapsed:.1f}s" if status else "no final response")
    if status != 200 or rtp is None:
        return

    heard = await asyncio.to_thread(gateway.heard, LISTEN_BEFORE_TYPING)
    peak = int(max((int(np.abs(c).max()) for c in heard), default=0))
    record_result("the caller hears the announcement", peak > 500,
                  f"peak {peak} in {LISTEN_BEFORE_TYPING:.0f}s")

    typed = MEETING_ID + ("#" if not PIN else "#")
    await asyncio.to_thread(gateway.keypad, typed)
    if PIN:
        heard += await asyncio.to_thread(gateway.heard, 2.0)
        await asyncio.to_thread(gateway.keypad, PIN + "#")

    entry = wait_for(lambda: (e := call_entry(client)) and e.roomid and e, 25)
    record_result("the caller is let into the conversation they named",
                  bool(entry) and entry.roomid == MEETING_ID,
                  (entry.roomid if entry else "none") + " wanted " + MEETING_ID)
    actor = (entry.actor or {}) if entry else {}
    record_result("and is a participant of it", bool(actor.get("actorId")),
                  f"{actor.get('actorType')}/{str(actor.get('actorId'))[:12]}")

    tone = (np.sin(2 * np.pi * TONE_HZ * np.arange(rtp.sample_rate) / rtp.sample_rate)
            * 8000).astype(np.int16)
    playing = asyncio.create_task(asyncio.to_thread(play, rtp, tone, RECORD_SECONDS + 12))
    published, rate = await subscribe_and_record(session, RECORD_SECONDS)
    await playing
    if len(published):
        frequency = dominant_frequency(published.astype(np.float64), rate)
        record_result("their audio arrives in that conversation", abs(frequency - TONE_HZ) < 25,
                      f"dominant {frequency:.0f} Hz")
    else:
        record_result("their audio arrives in that conversation", False, "nothing was published")

    heard += await asyncio.to_thread(gateway.heard, 0.5)
    path = write_wav(OUT_WAV, heard, rtp.sample_rate)
    if path:
        print(f"  ..    what the caller heard: {path}", flush=True)
    record_result("the hangup is acknowledged", gateway.bye() == 200)
    wait_for(lambda: call_entry(client) is None, 10)


async def wrong_numbers(gateway, client, line):
    """A caller who does not know a meeting id must not get anywhere, and
    must not be able to keep trying."""
    status, elapsed, rtp = await asyncio.to_thread(
        gateway.invite, (line.local_ip, line.local_sip_port), CONFERENCE_NUMBER, CALLER)
    if status != 200 or rtp is None:
        record_result("a wrong meeting id is refused", False, f"the call was not answered ({status})")
        return
    await asyncio.to_thread(gateway.keypad, "9876543210#" * 3, 0.15)
    ended = wait_for(lambda: gateway.received("BYE"), 60)
    record_result("wrong meeting ids end the call", bool(ended),
                  "the bridge hung up" if ended else "the call was still up after 60s")
    entry = call_entry(client)
    record_result("and let nobody into anything", not (entry and entry.roomid),
                  entry.roomid if entry and entry.roomid else "no conversation was joined")
    if not ended:
        gateway.bye()
    wait_for(lambda: call_entry(client) is None, 10)


async def main() -> int:
    if not MEETING_ID:
        print("Usage: test_dialin_ivr.py <meeting-id> [pin] - the conversation to dial into.")
        return 2
    print(f"\n== conference dial-in: {CALLER} calls {CONFERENCE_NUMBER} and keys in "
          f"{MEETING_ID}{' plus a PIN' if PIN else ''} ==", flush=True)
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
        await right_number(gateway, client, line, session)
        await wrong_numbers(gateway, client, line)
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
