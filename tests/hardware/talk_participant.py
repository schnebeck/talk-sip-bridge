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
    """A steady sine, paced in real time like a microphone would be."""

    kind = "audio"

    def __init__(self, frequency: float = 660.0, amplitude: int = 12000):
        super().__init__()
        self.frequency = frequency
        self.amplitude = amplitude
        self._samples = 0
        self._start = None

    async def recv(self):
        loop = asyncio.get_running_loop()
        if self._start is None:
            self._start = loop.time()
        due = self._start + self._samples / SAMPLE_RATE
        delay = due - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)

        t = (np.arange(SAMPLES_PER_FRAME) + self._samples) / SAMPLE_RATE
        pcm = (np.sin(2 * np.pi * self.frequency * t) * self.amplitude).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._samples
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        self._samples += SAMPLES_PER_FRAME
        return frame


class TalkParticipant:
    """One published stream in one conversation, for as long as it is
    open."""

    def __init__(self, roomid: str, frequency: float = 660.0, name: str = "Test tone"):
        self.roomid = roomid
        self.frequency = frequency
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

    async def start(self, timeout: float = 20.0):
        self.ws = await websockets.connect(config.ws_url)
        await self.ws.send(json.dumps(
            talk_messages.hello(config.internal_secret, config.backend_url)))
        await self.ws.recv()                                  # welcome
        hello = json.loads(await self.ws.recv())
        self.sessionid = hello["hello"]["sessionid"]
        await self.ws.send(json.dumps(talk_messages.join_room(self.roomid)))

        self._task = asyncio.ensure_future(self._serve())
        await self._publish()
        await asyncio.wait_for(self._published.wait(), timeout=timeout)
        # In the call, and carrying audio - without this the server tells
        # everyone there is nothing to listen to.
        await self.ws.send(json.dumps(talk_messages.set_incall(
            talk_messages.FLAG_IN_CALL | talk_messages.FLAG_WITH_AUDIO)))
        return self.sessionid

    async def _publish(self):
        self.pc = RTCPeerConnection()
        self.pc.addTrack(ToneTrack(self.frequency))
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
