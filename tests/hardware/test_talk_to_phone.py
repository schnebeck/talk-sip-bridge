#!/usr/bin/env python3
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
import time

import numpy as np

from config import config
from subscription import State

import test_dialin_ivr as scenario
from talk_participant import TalkParticipant

MEETING_ID = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("IVR_MEETING_ID", "")
TONE_HZ = float(os.environ.get("TALK_TONE_HZ", "660"))
LISTEN_SECONDS = float(os.environ.get("TALK_LISTEN_SECONDS", "12"))

results = []


def record_result(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def dominant_frequency(pcm: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(pcm.astype(np.float64) * np.hanning(len(pcm))))
    return float(np.fft.rfftfreq(len(pcm), 1 / rate)[spectrum.argmax()])


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

        # Somebody in the room first: the bridge looks for a publisher
        # when the call starts, and one that appears later is only found
        # again through a repair.
        participant = TalkParticipant(MEETING_ID, frequency=TONE_HZ)
        published = await participant.start()
        record_result("a participant publishes into the conversation", bool(published),
                      published or "no session")

        status, elapsed, rtp = await asyncio.to_thread(
            gateway.invite, (line.local_ip, line.local_sip_port),
            scenario.CONFERENCE_NUMBER, scenario.CALLER)
        record_result("the call is answered", status == 200, f"{status} after {elapsed:.1f}s")
        # What the phone side is actually decoding. The frequency check
        # below reads the recording at this rate, so a rate that is not
        # the negotiated one turns a correct tone into a wrong one.
        record_result("the codec is the one the caller offered",
                      rtp is not None and rtp.payload_type == 0,
                      f"payload type {rtp.payload_type if rtp else '-'}, "
                      f"{rtp.sample_rate if rtp else '-'} Hz")
        if status != 200 or rtp is None:
            return 1
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
        entry.subscription = None
        entry.media.subscriber_receiving = False
        asyncio.ensure_future(client._subscribe_human_audio(
            entry.sip_call_id, entry.media, published))

        # The dialogue's own "you are in" chime is still in the caller's
        # queue at this point, and its second note is 1320 Hz - measuring
        # into that reads the bridge's own signal as the participant's.
        await asyncio.sleep(2)
        while not rtp.recv_queue.empty():
            rtp.recv_queue.get_nowait()

        heard = await asyncio.to_thread(gateway.heard, LISTEN_SECONDS)
        pcm = np.concatenate(heard) if heard else np.array([], dtype=np.int16)
        state = entry.subscription
        record_result("the negotiation ends in audio flowing",
                      state is not None and state.state is State.FLOWING,
                      f"{state.state.value}, {state.attempts} attempt(s)" if state else "none")
        if len(pcm):
            peak = int(np.abs(pcm).max())
            frequency = dominant_frequency(pcm, rtp.sample_rate)
            record_result("the tone reaches the telephone", abs(frequency - TONE_HZ) < 30,
                          f"dominant {frequency:.0f} Hz, peak {peak}")
        else:
            record_result("the tone reaches the telephone", False, "the phone received nothing")

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
