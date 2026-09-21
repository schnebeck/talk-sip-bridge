#!/usr/bin/env python3
# talk-sip-bridge - tests/hardware/test_two_publishers.py
# How many people in a room a caller can hear at once.
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
"""How many people in a room a caller can hear at once.

test_talk_to_phone.py measures one participant reaching the telephone,
and it names that participant itself so that the negotiation is what is
under test. This measures something else: **two** participants publish
different tones, the bridge chooses on its own, and the caller's audio
is searched for both.

The question is whether a phone in a meeting hears the meeting or hears
one person. Reading the code says one - `call_media.py` holds a single
subscriber and a single `human_sessionid`. This turns that reading into
a measurement, because the difference decides whether several people in
one conversation with a caller is a feature or a trap.

The bridge is told which of the two to listen to, the same way
test_talk_to_phone.py does, for two reasons. `talk_participant.py`
connects as an internal client, and the bridge deliberately never counts
one of those as a person to listen to - so left alone it finds nobody
here. And naming one is what makes the measurement sharp: if the other
tone arrives as well, a call can carry more than one participant; if it
does not, it cannot. Which participant gets chosen in a real room is a
different question, and not this one.

Nothing human and no telephone: `fake_gateway.py` places the call,
`talk_participant.py` plays the tones.

    set -a; . /etc/talk-sip-bridge/env; set +a
    <the deployment venv>/bin/python3 tests/hardware/test_two_publishers.py <meeting-id>

The caller's own audio is written to OUT_WAV if that is set.
"""

import os
import pathlib
import sys

sys.path.insert(0, os.environ.get("BRIDGE_CODE")
                or str(pathlib.Path(__file__).resolve().parent.parent.parent / "bridge"))

import asyncio

import numpy as np

from config import config

import test_dialin_ivr as scenario
from talk_participant import TalkParticipant

MEETING_ID = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("IVR_MEETING_ID", "")
# Far apart, and neither a harmonic of the other, so a spectrum cannot
# confuse them. Both inside the band every codec here carries.
TONE_A = float(os.environ.get("TONE_A_HZ", "440"))
TONE_B = float(os.environ.get("TONE_B_HZ", "1150"))
LISTEN_SECONDS = float(os.environ.get("TALK_LISTEN_SECONDS", "12"))
OUT_WAV = os.environ.get("OUT_WAV", "")
CODEC = {"pcmu": 0, "g722": 9}[os.environ.get("TALK_CODEC", "pcmu").lower()]

results = []


def record_result(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def energy_at(pcm: np.ndarray, rate: int, frequency: float) -> float:
    """How much of the signal sits at one frequency, in dB below the
    loudest thing in the recording. 0 dB means it *is* the loudest."""
    signal = pcm.astype(np.float64) * np.hanning(len(pcm))
    spectrum = np.abs(np.fft.rfft(signal))
    freqs = np.fft.rfftfreq(len(signal), 1 / rate)
    band = np.abs(freqs - frequency) < 25
    if not band.any() or not spectrum.max():
        return -99.0
    return float(20 * np.log10((spectrum[band].max() or 1e-9) / spectrum.max()))


async def main() -> int:
    if not MEETING_ID:
        print("Usage: test_two_publishers.py <meeting-id>")
        return 2
    print(f"\n== Two participants, {TONE_A:.0f} Hz and {TONE_B:.0f} Hz, "
          f"in {MEETING_ID}. What does the caller hear? ==", flush=True)

    line = scenario.build_line()
    gateway = scenario.FakeGateway(config.local_ip, scenario.GW_SIP_PORT, scenario.GW_RTP_PORT)
    client, call_manager = scenario.start_bridge(line)
    first = second = None
    try:
        session = scenario.wait_for(lambda: client.own_sessionid, 20)
        record_result("the bridge reaches the signaling server", bool(session))
        if not session:
            return 1

        # Both in the room and publishing before the call, so neither can
        # be missed for having arrived late. Which one the bridge picks is
        # the whole question, so nothing here names one.
        first = TalkParticipant(MEETING_ID, frequency=TONE_A)
        session_a = await first.connect()
        await first.publish()
        second = TalkParticipant(MEETING_ID, frequency=TONE_B)
        session_b = await second.connect()
        await second.publish()
        record_result("two participants are publishing",
                      bool(session_a) and bool(session_b) and session_a != session_b,
                      f"{str(session_a)[:8]} at {TONE_A:.0f} Hz, "
                      f"{str(session_b)[:8]} at {TONE_B:.0f} Hz")

        status, elapsed, rtp = await asyncio.to_thread(
            gateway.invite, (line.local_ip, line.local_sip_port),
            scenario.CONFERENCE_NUMBER, scenario.CALLER, None, 30.0, CODEC)
        record_result("the call is answered", status == 200, f"{status} after {elapsed:.1f}s")
        if status != 200 or rtp is None:
            return 1
        # Let the announcement start before typing. The first press is
        # dropped if it arrives before the dialogue exists to receive it,
        # and a meeting id one digit short is refused - which looks like
        # a bridge fault and is a test that typed too early.
        await asyncio.to_thread(gateway.heard, 3.0, [])
        await asyncio.to_thread(gateway.keypad, MEETING_ID + "#")
        entry = scenario.wait_for(lambda: (e := scenario.call_entry(client)) and e.roomid and e, 25)
        record_result("the caller is in the conversation",
                      bool(entry) and entry.roomid == MEETING_ID,
                      entry.roomid if entry else "none")
        if not entry:
            return 1

        # Both, by name. The bridge would find neither on its own:
        # talk_participant.py connects as an internal client, and the
        # roster deliberately never offers one of those as somebody to
        # listen to. Naming them is what puts the question to the
        # mixing path rather than to the roster.
        for named in (session_a, session_b) if config.mix_participants else (session_a,):
            entry.subscriptions.pop(named, None)
            asyncio.ensure_future(client.human_audio.start(
                entry.sip_call_id, entry.media, named))

        # The dialogue's accepted chime is still queued and its second
        # note is 1320 Hz, which would read as a tone nobody published.
        await asyncio.sleep(4)
        while not rtp.recv_queue.empty():
            rtp.recv_queue.get_nowait()

        heard = await asyncio.to_thread(gateway.heard, LISTEN_SECONDS, [])
        pcm = np.concatenate(heard) if heard else np.array([], dtype=np.int16)
        if not len(pcm):
            record_result("the caller receives audio at all", False, "nothing arrived")
            return 1

        record_result("the caller receives audio at all", True,
                      f"{len(pcm) / rtp.sample_rate:.1f}s, peak {int(np.abs(pcm).max())}")
        carrying = set(entry.media.receiving_from)
        wanted = {session_a, session_b} if config.mix_participants else {session_a}
        # Not an exact count: a real room can hold sessions this test
        # did not put there - a browser left open from an earlier run
        # is a participant like any other, and the bridge subscribes to
        # it too. What matters is that the ones under test are carried.
        record_result("both participants are carried at once"
                      if config.mix_participants else
                      "the one participant is carried",
                      wanted <= carrying,
                      f"{len(carrying)} of {len(entry.media.subscribers)} "
                      f"subscription(s) delivering")

        level_a = energy_at(pcm, rtp.sample_rate, TONE_A)
        level_b = energy_at(pcm, rtp.sample_rate, TONE_B)
        print(f"\n  {TONE_A:.0f} Hz in the caller's audio: {level_a:6.1f} dB", flush=True)
        print(f"  {TONE_B:.0f} Hz in the caller's audio: {level_b:6.1f} dB", flush=True)

        # A tone that was carried dominates its own recording; one that
        # was not is down in the noise. Anything between the two is the
        # interesting answer and is reported rather than judged.
        both = level_a > -20 and level_b > -20
        print(f"  mixing: {'on' if config.mix_participants else 'off'}, "
              f"{len(entry.media.subscribers)} subscription(s), "
              f"carrying {len(entry.media.receiving_from)}", flush=True)
        # Phrased so that today's behaviour passes and a change fails:
        # this is a measurement of a known limit, not a defect hunt. The
        # day a call can carry two participants, the second line here is
        # what says so.
        record_result(f"the subscribed participant is carried ({TONE_A:.0f} Hz)",
                      level_a > -20, f"{level_a:.1f} dB")
        if config.mix_participants:
            record_result(f"the second one is carried too ({TONE_B:.0f} Hz)",
                          level_b > -20, f"{level_b:.1f} dB")
        else:
            record_result(f"the other one is not, as expected ({TONE_B:.0f} Hz)",
                          level_b <= -20,
                          f"{level_b:.1f} dB - a call carries one participant")
        if config.mix_participants and both:
            print("\n  Measured: with BRIDGE_MIX_PARTICIPANTS the caller hears "
                  "both participants.", flush=True)
        elif not config.mix_participants and level_a > -20 and level_b <= -20:
            print("\n  Measured: without it, a caller hears the one participant "
                  "the bridge\n  subscribed to and nothing of the other - "
                  "which is the default.", flush=True)
        else:
            print("\n  Measured: neither shape. Look at the levels above.", flush=True)

        if OUT_WAV:
            import wave
            with wave.open(OUT_WAV, "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(rtp.sample_rate)
                out.writeframes(pcm.astype(np.int16).tobytes())
            print(f"  ..    what the caller received: {OUT_WAV}", flush=True)
    finally:
        for participant in (first, second):
            if participant is not None:
                try:
                    await participant.close()
                except Exception:
                    pass
        try:
            call_manager.hangup()
        except Exception:
            pass
        gateway.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\nRESULT: {passed}/{len(results)} checks passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
