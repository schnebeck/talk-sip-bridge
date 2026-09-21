# talk-sip-bridge - tests/hardware/talk_participant.py
# A participant in a Talk conversation that publishes a tone.
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

"""A participant in a Talk conversation that publishes a tone.

The counterpart to fake_gateway.py, on the other side of the bridge. The
gateway lets a call be placed without a telephone; this lets there be
somebody in the room without a person holding a browser open - which is
what the direction Talk -> phone needs before it can be measured at all.

It connects as an internal client, which is the one kind of client that
needs no Nextcloud account, no ticket and no Talk session: the shared
signaling secret is enough. Everything after that is what any client
does - join the room, offer its stream to its own session, let the
server's MCU answer.

What it is NOT is a stand-in for a real Talk client: it publishes one
sine tone, subscribes to nobody, and has no data channels. It exists so
the bridge has something real to subscribe to.
"""
import asyncio
import fractions
import json
import os
import pathlib
import secrets
import sys

sys.path.insert(0, os.environ.get("BRIDGE_CODE")
                or str(pathlib.Path(__file__).resolve().parent.parent.parent / "bridge"))

import av
import numpy as np
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack

import talk_messages
from config import config

SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 960          # 20ms, what every WebRTC stack expects


class ToneTrack(MediaStreamTrack):
    """A test signal, paced in real time like a microphone would be.

    Two shapes, because a steady sine hides most of what goes wrong.
    It has constant level, so it cannot show gain pumping; it never
    stops, so it cannot show a gate closing, a dropout at the start of a
    burst, or anything that only happens at the edges of speech; and any
    voice-activity detector in the path reads it as one endless
    utterance.

    `modulated` therefore alternates bursts with a quiet floor, and
    fills each burst with several voice-band tones at once - the shape
    of speech at the level a handset delivers, and the signal
    test_audio_quality.py already measures the publish path with. The
    steady sine stays available, because a single known frequency is
    what an FFT check wants when the question is only "did it arrive".
    """

    kind = "audio"

    # A burst and the pause after it, in seconds. Long enough that a
    # gate has time to close and be heard doing it.
    BURST = 1.2
    PAUSE = 0.8

    def __init__(self, frequency: float = 660.0, amplitude: int = 12000,
                 modulated: bool = False):
        super().__init__()
        self.frequency = frequency
        self.amplitude = amplitude
        self.modulated = modulated
        # Around the fundamental, not harmonics of it: harmonics would
        # pile onto the same FFT bins and read as one loud tone.
        self._partials = [frequency, frequency * 1.47, frequency * 2.31]
        self._samples = 0
        self._start = None

    def _signal(self, t: np.ndarray) -> np.ndarray:
        """What the microphone would be sending at these instants."""
        if not self.modulated:
            return np.sin(2 * np.pi * self.frequency * t) * self.amplitude
        wave = sum(np.sin(2 * np.pi * f * t) for f in self._partials) / len(self._partials)
        # Where each instant falls in the burst/pause cycle. Computed
        # from absolute time rather than counted per frame, so it cannot
        # drift against the stream's own clock.
        period = self.BURST + self.PAUSE
        phase = np.mod(t, period)
        loud = phase < self.BURST
        # Ramp the edges over 10ms: a burst that starts at full level is
        # a click, and a click is a discontinuity every measurement here
        # would report as a fault.
        edge = np.clip(np.minimum(phase, self.BURST - phase) / 0.01, 0.0, 1.0)
        envelope = np.where(loud, edge, 0.04)
        return wave * envelope * self.amplitude

    async def recv(self):
        loop = asyncio.get_running_loop()
        if self._start is None:
            self._start = loop.time()
        due = self._start + self._samples / SAMPLE_RATE
        delay = due - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)

        t = (np.arange(SAMPLES_PER_FRAME) + self._samples) / SAMPLE_RATE
        pcm = self._signal(t).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._samples
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        self._samples += SAMPLES_PER_FRAME
        return frame


class TalkParticipant:
    """One published stream in one conversation, for as long as it is
    open."""

    def __init__(self, roomid: str, frequency: float = 660.0, name: str = "Test tone",
                 modulated: bool = False):
        self.roomid = roomid
        self.frequency = frequency
        self.modulated = modulated
        self.name = name
        self.ws = None
        self.sessionid = None
        self.pc = None
        self._task = None
        self._published = asyncio.Event()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def connect(self):
        """In the room, with a session id, but publishing nothing yet.

        Separate from publishing because when the two happen decides
        which negotiation the other side runs: a participant that is
        known but silent is answered "client_not_found", which is what a
        person looks like between joining a call and their browser
        getting a stream up."""
        self.ws = await websockets.connect(config.ws_url)
        await self.ws.send(json.dumps(
            talk_messages.hello(config.internal_secret, config.backend_url)))
        await self.ws.recv()                                  # welcome
        hello = json.loads(await self.ws.recv())
        self.sessionid = hello["hello"]["sessionid"]
        await self.ws.send(json.dumps(talk_messages.join_room(self.roomid)))
        self._task = asyncio.ensure_future(self._serve())
        return self.sessionid

    async def publish(self, timeout: float = 20.0):
        await self._publish()
        await asyncio.wait_for(self._published.wait(), timeout=timeout)
        # In the call, and carrying audio - without this the server tells
        # everyone there is nothing to listen to.
        await self.ws.send(json.dumps(talk_messages.set_incall(
            talk_messages.FLAG_IN_CALL | talk_messages.FLAG_WITH_AUDIO)))
        return self.sessionid

    async def start(self, timeout: float = 20.0):
        await self.connect()
        return await self.publish(timeout)

    async def _publish(self):
        self.pc = RTCPeerConnection()
        self.pc.addTrack(ToneTrack(self.frequency, modulated=self.modulated))
        await self.pc.setLocalDescription(await self.pc.createOffer())
        while self.pc.iceGatheringState != "complete":
            await asyncio.sleep(0.1)
        await self.ws.send(json.dumps(talk_messages.publish_offer(
            self.sessionid, f"tone-{secrets.token_hex(4)}",
            self.pc.localDescription.sdp, self.name)))

    async def _serve(self):
        """The one message that matters is the MCU's answer to the offer;
        everything else is somebody else's business."""
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                if message.get("type") != "message":
                    continue
                data = message.get("message", {}).get("data", {})
                if data.get("type") == "answer" and self.pc is not None:
                    await self.pc.setRemoteDescription(RTCSessionDescription(
                        sdp=data["payload"]["sdp"], type="answer"))
                    self._published.set()
        except Exception:
            pass

    async def close(self):
        if self._task is not None:
            self._task.cancel()
        if self.pc is not None:
            await self.pc.close()
        if self.ws is not None:
            await self.ws.close()
