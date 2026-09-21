#!/usr/bin/env python3
# talk-sip-bridge - tests/hardware/test_talk_to_phone.py
# The direction that was never machine-verified: Talk to the telephone.
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

"""The direction that was never machine-verified: Talk to the telephone.

Everything else here measures the phone's audio arriving in a room. This
measures the other way - a participant in the room plays a tone, and the
tone is looked for in the RTP the caller's own session receives.

Nothing human is involved. fake_gateway.py places the call and listens
on the phone's side; talk_participant.py sits in the conversation and
publishes; between them runs the whole subscription negotiation this
bridge kept getting wrong: asking for an offer, answering the one that
is still alive, repairing a refused answer without tearing down what
works (see subscription.py).

    set -a; . /etc/talk-sip-bridge/env; set +a
    <the deployment venv>/bin/python3 tests/hardware/test_talk_to_phone.py <meeting-id>
"""

import os
import pathlib
import sys

sys.path.insert(0, os.environ.get("BRIDGE_CODE")
                or str(pathlib.Path(__file__).resolve().parent.parent.parent / "bridge"))

import asyncio

import numpy as np

from config import config
from subscription import State

import test_dialin_ivr as scenario
from talk_participant import TalkParticipant

MEETING_ID = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("IVR_MEETING_ID", "")
TONE_HZ = float(os.environ.get("TALK_TONE_HZ", "660"))
LISTEN_SECONDS = float(os.environ.get("TALK_LISTEN_SECONDS", "12"))
OUT_WAV = os.environ.get("OUT_WAV", "")
# Seconds to wait before the participant publishes, 0 for "already
# there". Late is the interesting case: it is what a person does.
JOIN_LATE = float(os.environ.get("TALK_JOIN_LATE", "0"))
# Which codec the call runs at. A real call to this gateway negotiates
# G.722, and its 16kHz resample from Talk's 48kHz for the way back
# is a different operation than PCMU's 8kHz.
CODEC = {"pcmu": 0, "g722": 9}[os.environ.get("TALK_CODEC", "pcmu").lower()]

results = []


async def _no_human():
    """Nobody for the bridge to pick on its own - see below."""
    return None


def record_result(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def dominant_frequency(pcm: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(pcm.astype(np.float64) * np.hanning(len(pcm))))
    return float(np.fft.rfftfreq(len(pcm), 1 / rate)[spectrum.argmax()])


def glitches(pcm: np.ndarray, rate: int, frequency: float):
    """Where a steady tone stops being steady.

    A sine of known frequency has a known largest step between samples;
    anything past that is a discontinuity - a click, a dropped packet, a
    resampler starting over. Reported with the gaps between them,
    because an artefact that repeats at a fixed interval names its own
    cause: once per second is something on a one-second timer, every
    20ms is per packet.
    """
    signal = pcm.astype(np.float64)
    amplitude = np.abs(signal).max() or 1.0
    steps = np.abs(np.diff(signal))
    largest_expected = amplitude * 2 * np.pi * frequency / rate
    at = np.flatnonzero(steps > 3 * largest_expected)
    # One click spans a few samples; count it once.
    if len(at):
        at = at[np.insert(np.diff(at) > rate // 100, 0, True)]
    return at / rate, (np.diff(at) / rate if len(at) > 1 else np.array([]))


def tone_report(pcm: np.ndarray, rate: int, frequency: float) -> str:
    """What the tone looks like on arrival, beyond its pitch."""
    signal = pcm.astype(np.float64)
    seconds = len(signal) / rate
    spectrum = np.abs(np.fft.rfft(signal * np.hanning(len(signal))))
    freqs = np.fft.rfftfreq(len(signal), 1 / rate)
    fundamental = np.abs(freqs - frequency) < 15
    rest = spectrum.copy()
    rest[fundamental] = 0
    purity = 20 * np.log10((rest.max() or 1e-9) / (spectrum.max() or 1e-9))
    where, gaps = glitches(pcm, rate, frequency)
    report = (f"{seconds:.1f}s, everything but the tone at {purity:.0f} dB, "
              f"{len(where)} discontinuit{'y' if len(where) == 1 else 'ies'}")
    if len(gaps):
        report += f", every {gaps.mean():.2f}s (+-{gaps.std():.2f})"
    return report


async def main() -> int:
    if not MEETING_ID:
        print("Usage: test_talk_to_phone.py <meeting-id>")
        return 2
    print(f"\n== Talk -> phone: a participant plays {TONE_HZ:.0f} Hz into {MEETING_ID} ==",
          flush=True)

    line = scenario.build_line()
    gateway = scenario.FakeGateway(config.local_ip, scenario.GW_SIP_PORT, scenario.GW_RTP_PORT)
    client, call_manager = scenario.start_bridge(line)
    participant = None
    try:
        session = scenario.wait_for(lambda: client.own_sessionid, 20)
        record_result("the bridge reaches the signaling server", bool(session))
        if not session:
            return 1
        # The bridge picks somebody out of the room roster when the call
        # starts, and that roster also holds sessions from hours ago. Two
        # subscriptions on one call share its negotiation and cancel each
        # other out, so the choosing is taken out of this test - what is
        # under test is the negotiation with a publisher it names.
        client.human_audio.find_human = lambda *a, **kw: _no_human()

        # When the participant appears decides which negotiation runs.
        # Before the call, the first request succeeds and there is one
        # attempt and no waiting - the easy timing, and the one this
        # test used to have only. After it, the first requests are
        # answered "client_not_found", retries are armed, and the offer
        # arrives while one of them is still pending: the timing of a
        # real call, where a person joins after the phone is in the
        # room. A retry that fires into the connection that meanwhile
        # started working is exactly what broke one.
        participant = TalkParticipant(MEETING_ID, frequency=TONE_HZ)
        published = await participant.connect()
        if not JOIN_LATE:
            await participant.publish()
            record_result("a participant publishes into the conversation", bool(published),
                          published or "no session")

        status, elapsed, rtp = await asyncio.to_thread(
            gateway.invite, (line.local_ip, line.local_sip_port),
            scenario.CONFERENCE_NUMBER, scenario.CALLER, None, 30.0, CODEC)
        record_result("the call is answered", status == 200, f"{status} after {elapsed:.1f}s")
        # What the phone side is actually decoding. The frequency check
        # below reads the recording at this rate, so a rate that is not
        # the negotiated one turns a correct tone into a wrong one.
        record_result("the codec is the one the caller offered",
                      rtp is not None and rtp.payload_type == CODEC,
                      f"payload type {rtp.payload_type if rtp else '-'}, "
                      f"{rtp.sample_rate if rtp else '-'} Hz")
        if status != 200 or rtp is None:
            return 1
        # Let the announcement start before typing. A press that
        # arrives before the dialogue exists to receive it is dropped,
        # and a meeting id one digit short is refused - which reads as
        # a bridge fault and is a test that was too quick.
        await asyncio.to_thread(gateway.heard, 3.0)
        await asyncio.to_thread(gateway.keypad, MEETING_ID + "#")
        entry = scenario.wait_for(lambda: (e := scenario.call_entry(client)) and e.roomid and e, 25)
        record_result("the caller is in the conversation", bool(entry) and entry.roomid == MEETING_ID,
                      entry.roomid if entry else "none")
        if not entry:
            return 1

        # Which participant to listen to is the bridge's own choice, out
        # of a room roster that also holds sessions from hours ago; it
        # normally starts a negotiation with one of those before this
        # test gets a word in. What is under test is the negotiation, so
        # the publisher is named here and the machine started over.
        entry.subscriptions.pop(published, None)
        entry.media.subscriber_receiving = False
        asyncio.ensure_future(client.human_audio.start(
            entry.sip_call_id, entry.media, published))

        if JOIN_LATE:
            # By now the bridge has asked for audio nobody is sending,
            # been told "client_not_found", and armed a retry. The offer
            # arrives while that retry is still pending - the timing a
            # person produces, and the one that broke a call.
            await asyncio.sleep(JOIN_LATE)
            await participant.publish()
            record_result("a participant publishes into the conversation", True,
                          f"after {JOIN_LATE:.0f}s, the way a person joins")

        # The dialogue's own "you are in" chime is still in the caller's
        # queue at this point, and its second note is 1320 Hz - measuring
        # into that reads the bridge's own signal as the participant's.
        await asyncio.sleep(2)
        while not rtp.recv_queue.empty():
            rtp.recv_queue.get_nowait()

        arrivals = []
        heard = await asyncio.to_thread(gateway.heard, LISTEN_SECONDS, arrivals)
        pcm = np.concatenate(heard) if heard else np.array([], dtype=np.int16)
        state = entry.subscriptions.get(published)
        record_result("the negotiation ends in audio flowing",
                      state is not None and state.state is State.FLOWING,
                      f"{state.state.value}, {state.attempts} attempt(s)" if state else "none")
        if len(pcm):
            peak = int(np.abs(pcm).max())
            frequency = dominant_frequency(pcm, rtp.sample_rate)
            record_result("the tone reaches the telephone", abs(frequency - TONE_HZ) < 30,
                          f"dominant {frequency:.0f} Hz, peak {peak}")
            where, gaps = glitches(pcm, rtp.sample_rate, TONE_HZ)
            record_result("it arrives without interruptions", len(where) == 0,
                          tone_report(pcm, rtp.sample_rate, TONE_HZ))
            if OUT_WAV:
                import wave
                with wave.open(OUT_WAV, "wb") as out:
                    out.setnchannels(1)
                    out.setsampwidth(2)
                    out.setframerate(rtp.sample_rate)
                    out.writeframes(pcm.astype(np.int16).tobytes())
                print(f"  ..    what the telephone received: {OUT_WAV}", flush=True)
                if len(where):
                    print("  ..    interruptions at: "
                          + ", ".join(f"{s:.2f}s" for s in where[:12]), flush=True)
        else:
            record_result("the tone reaches the telephone", False, "the phone received nothing")

        # How steady the sending is, which is what a click actually is:
        # every packet can be perfect and the call still crack once a
        # second if the sender stalls that often.
        gaps = np.diff(np.array(arrivals)) if len(arrivals) > 2 else np.array([])
        if len(gaps):
            expected = rtp.samples_per_packet / rtp.sample_rate
            # 1.5x the packet interval is already a hesitation a jitter
            # buffer has to absorb; 3x is one it cannot.
            hesitations = np.flatnonzero(gaps > 1.5 * expected)
            stalls = np.flatnonzero(gaps > 3 * expected)
            detail = (f"{len(gaps) + 1} packets, median {np.median(gaps) * 1000:.1f} ms, "
                      f"worst {gaps.max() * 1000:.0f} ms, "
                      f"{len(hesitations)} over {1.5 * expected * 1000:.0f} ms, "
                      f"{len(stalls)} over {3 * expected * 1000:.0f} ms")
            if len(hesitations) > 1:
                spacing = np.diff(np.array(arrivals)[hesitations + 1])
                detail += (f"; the long ones every {spacing.mean():.2f}s "
                           f"(+-{spacing.std():.2f})")
            record_result("the packets arrive evenly", len(stalls) == 0, detail)

        record_result("the hangup is acknowledged", gateway.bye() == 200)
        scenario.wait_for(lambda: scenario.call_entry(client) is None, 10)
    finally:
        if participant is not None:
            await participant.close()
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
