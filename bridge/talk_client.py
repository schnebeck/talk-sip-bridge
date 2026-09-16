"""Connects to the Nextcloud Talk standalone signaling server as an
"internal client" with the "start-dialout" and "internal-incall" feature
flags, so Talk's own native call UI (not a custom chat-bot command, see
docs/CONCEPT.md) can trigger outbound calls and receive real, named
"phone" participants for inbound ones.

Protocol reference (public, no reference implementation found anywhere -
built directly against this documentation, see docs/CONCEPT.md):
https://nextcloud-spreed-signaling.readthedocs.io/en/latest/standalone-signaling-api-v1/
sections "Internal clients", "Client features", "Dialout session",
"Start dialout from a room", "Add/update/remove virtual session".

This module owns its own asyncio event loop (run in a background thread by
daemon.py) and bridges to sip_core.CallManager, whose callbacks fire from
plain worker threads via asyncio.run_coroutine_threadsafe.
"""
import asyncio
import fractions
import hashlib
import hmac
import json
import secrets
import threading

import numpy as np
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AudioStreamTrack
from av import AudioFrame

from config import config

AUDIO_SAMPLE_RATE = 48000
SIP_SAMPLE_RATE = 8000
UPSAMPLE_FACTOR = AUDIO_SAMPLE_RATE // SIP_SAMPLE_RATE
RTP_QUEUE_POLL_INTERVAL = 0.02  # matches one 20ms RTP packet - keeps frame pacing real-time


class SipAudioTrack(AudioStreamTrack):
    """Reads decoded PCM (8kHz mono) from an RtpSession's receive queue,
    upsampled to AUDIO_SAMPLE_RATE by simple repetition."""

    def __init__(self, rtp_session):
        super().__init__()
        self.rtp_session = rtp_session
        self._pts = 0

    async def recv(self):
        try:
            pcm_8k = await asyncio.to_thread(self.rtp_session.recv_queue.get, True, RTP_QUEUE_POLL_INTERVAL)
        except Exception:
            pcm_8k = np.zeros(160, dtype=np.int16)
        pcm_48k = np.repeat(pcm_8k, UPSAMPLE_FACTOR)
        frame = AudioFrame.from_ndarray(pcm_48k.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = AUDIO_SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, AUDIO_SAMPLE_RATE)
        self._pts += len(pcm_48k)
        return frame


class TalkClient:
    def __init__(self, call_manager):
        self.call_manager = call_manager
        self.ws = None
        self.own_sessionid = None
        self.loop = None
        self._call_sessions = {}  # sip call_id -> {"virtual_sessionid", "pc"}
        self._call_sessions_lock = threading.Lock()  # entries are written from both the asyncio loop and SIP worker threads

    # -- lifecycle, run from a background thread -----------------------
    def run_forever(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect_and_serve())

    async def _connect_and_serve(self):
        while True:
            try:
                async with websockets.connect(config.ws_url) as ws:
                    self.ws = ws
                    await self._hello()
                    await self._message_loop()
            except Exception as e:
                print(f"[talk] Connection lost ({e!r}), reconnecting ...")
                self.ws = None
                await asyncio.sleep(config.sip_response_timeout)

    async def _hello(self):
        random_str = secrets.token_hex(32)
        token = hmac.new(config.internal_secret.encode(), random_str.encode(), hashlib.sha256).hexdigest()
        await self.ws.send(json.dumps({
            "id": "bridge-hello", "type": "hello",
            "hello": {
                "version": "1.0",
                "features": ["start-dialout", "internal-incall"],
                "auth": {
                    "type": "internal",
                    "params": {"random": random_str, "token": token, "backend": config.backend_url},
                },
            },
        }))
        await self.ws.recv()  # welcome banner
        resp = json.loads(await self.ws.recv())
        self.own_sessionid = resp["hello"]["sessionid"]
        print(f"[talk] Connected as internal client, session {self.own_sessionid}")

    # -- incoming messages from the signaling server ---------------------
    async def _message_loop(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            msg_type = msg.get("type")
            if msg_type == "dialout":
                await self._handle_dialout(msg["dialout"])
            elif msg_type == "message":
                await self._handle_webrtc_message(msg["message"])

    async def _handle_dialout(self, dialout: dict):
        """Talk's native "call a phone number" UI triggers this."""
        number = dialout.get("number", "")
        print(f"[talk] Dialout request for {number}")
        result = self.call_manager.dial(number)
        if "error" in result:
            await self._send_dialout_error(result["error"])
            return
        call_id = result["call_id"]
        with self._call_sessions_lock:
            self._call_sessions[call_id] = {"kind": "dialout", "number": number}

    async def _send_dialout_result(self, call_id_sip: str):
        with self._call_sessions_lock:
            session = dict(self._call_sessions.get(call_id_sip, {}))
        await self.ws.send(json.dumps({
            "type": "dialout",
            "dialout": {"callid": session.get("virtual_sessionid", call_id_sip)},
        }))

    async def _send_dialout_error(self, message: str):
        await self.ws.send(json.dumps({
            "type": "dialout",
            "dialout": {"error": {"code": "call_failed", "message": message}},
        }))

    async def _handle_webrtc_message(self, message: dict):
        data = message.get("data", {})
        sender_sessionid = message.get("sender", {}).get("sessionid")
        entry = self._session_for_ws_peer(sender_sessionid)
        if not entry or "pc" not in entry:
            return
        pc = entry["pc"]
        if data.get("type") == "answer":
            await pc.setRemoteDescription(RTCSessionDescription(sdp=data["payload"]["sdp"], type="answer"))

    def _session_for_ws_peer(self, sessionid):
        with self._call_sessions_lock:
            for entry in self._call_sessions.values():
                if entry.get("ws_peer_sessionid") == sessionid:
                    return entry
        return None

    # -- virtual session management (addsession/removesession) -----------
    async def _add_virtual_session(self, sip_call_id: str, *, roomid: str, number: str, caller: bool) -> str:
        virtual_sessionid = f"phone-{secrets.token_hex(8)}"
        await self.ws.send(json.dumps({
            "type": "internal",
            "internal": {
                "type": "addsession",
                "addsession": {
                    "sessionid": virtual_sessionid,
                    "roomid": roomid,
                    "user": {"type": "phone", "callid": sip_call_id, "number": number},
                },
            },
        }))
        return virtual_sessionid

    async def _remove_virtual_session(self, virtual_sessionid: str, roomid: str):
        await self.ws.send(json.dumps({
            "type": "internal",
            "internal": {
                "type": "removesession",
                "removesession": {"sessionid": virtual_sessionid, "roomid": roomid},
            },
        }))

    # -- publishing SIP call audio into the room --------------------------
    async def _publish_call_audio(self, sip_call_id: str, rtp_session, roomid: str, number: str):
        virtual_sessionid = await self._add_virtual_session(sip_call_id, roomid=roomid, number=number, caller=True)
        pc = RTCPeerConnection()
        pc.addTrack(SipAudioTrack(rtp_session))
        with self._call_sessions_lock:
            entry = self._call_sessions.setdefault(sip_call_id, {})
            entry["virtual_sessionid"] = virtual_sessionid
            entry["pc"] = pc
            entry["ws_peer_sessionid"] = virtual_sessionid

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await self.ws.send(json.dumps({
            "id": f"bridge-offer-{sip_call_id}", "type": "message",
            "message": {
                "recipient": {"type": "session", "sessionid": virtual_sessionid},
                "data": {
                    "to": virtual_sessionid, "type": "offer", "sid": secrets.token_hex(8), "roomType": "video",
                    "payload": {"nick": number, "type": "offer", "sdp": pc.localDescription.sdp},
                    "audiocodec": "opus",
                },
            },
        }))
        await self._send_dialout_result(sip_call_id)
        print(f"[talk] Publishing call audio for {sip_call_id} as virtual session {virtual_sessionid}")

    async def _teardown_call(self, sip_call_id: str, roomid: str):
        with self._call_sessions_lock:
            entry = self._call_sessions.pop(sip_call_id, None)
        if not entry:
            return
        if "pc" in entry:
            await entry["pc"].close()
        if "virtual_sessionid" in entry:
            await self._remove_virtual_session(entry["virtual_sessionid"], roomid)

    # -- thread-safe entry points for sip_core.CallManager callbacks ------
    def on_call_connected(self, *, call_id, direction, rtp):
        with self._call_sessions_lock:
            number = self._call_sessions.get(call_id, {}).get("number", "")
        asyncio.run_coroutine_threadsafe(
            self._publish_call_audio(call_id, rtp, config.default_room_token, number),
            self.loop,
        )

    def on_call_ended(self, *, call_id, reason):
        asyncio.run_coroutine_threadsafe(
            self._teardown_call(call_id, config.default_room_token),
            self.loop,
        )

    def on_call_failed(self, *, call_id, reason):
        asyncio.run_coroutine_threadsafe(self._send_dialout_error(reason), self.loop)

    def on_incoming_call(self, *, call_id, caller):
        # The signaling protocol has no ringing/accept-decline exchange for
        # inbound calls ("addsession" represents an already-connected call,
        # and dialout is Talk-initiated only), so there is no native way to
        # ask before picking up. Answering is therefore off unless
        # config.auto_answer_calls is explicitly enabled - an unanswered
        # call just keeps ringing (or is picked up elsewhere), same as any
        # other registered phone that nobody happens to pick up.
        with self._call_sessions_lock:
            self._call_sessions[call_id] = {"kind": "inbound", "number": caller}
        if not config.auto_answer_calls:
            print(f"[talk] Incoming call {call_id} from {caller} - auto-answer disabled, leaving it ringing")
            return
        print(f"[talk] Incoming call {call_id} from {caller} - answering with real audio")
        self.call_manager.answer()


def start_in_background(call_manager) -> TalkClient:
    client = TalkClient(call_manager)
    thread = threading.Thread(target=client.run_forever, daemon=True)
    thread.start()
    return client
