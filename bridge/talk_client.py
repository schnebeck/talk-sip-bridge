"""Connects to the Nextcloud Talk standalone signaling server as an
"internal client" with the "start-dialout" feature flag, so Talk's own
native call UI (not a custom chat-bot command, see docs/CONCEPT.md) can
trigger outbound calls and receive real, named "phone" participants for
inbound ones.

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


def _upsample_linear(pcm: np.ndarray, factor: int) -> np.ndarray:
    """Linear interpolation, not sample repetition - repetition produces a
    staircase waveform (harsh, aliased); this is a large audible
    improvement for a small amount of code, without adding a dependency
    for full sinc-based resampling."""
    n_in = len(pcm)
    x_out = np.linspace(0, n_in - 1, n_in * factor)
    return np.interp(x_out, np.arange(n_in), pcm).astype(np.int16)


class SipAudioTrack(AudioStreamTrack):
    """Reads decoded PCM (8kHz mono) from an RtpSession's receive queue,
    upsampled to AUDIO_SAMPLE_RATE."""

    def __init__(self, rtp_session):
        super().__init__()
        self.rtp_session = rtp_session
        self._pts = 0

    async def recv(self):
        try:
            pcm_8k = await asyncio.to_thread(self.rtp_session.recv_queue.get, True, RTP_QUEUE_POLL_INTERVAL)
        except Exception:
            pcm_8k = np.zeros(160, dtype=np.int16)
        pcm_48k = _upsample_linear(pcm_8k, UPSAMPLE_FACTOR)
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
        self._room_joined_event = asyncio.Event()

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
                # Deliberately NOT declaring "internal-incall": that tells
                # the server this client will manage its own inCall/
                # publishing-audio flags, which nothing here currently does.
                # Without it, the server sets both automatically on connect,
                # which is what makes the self-addressed publish offer
                # below actually work.
                "features": ["start-dialout"],
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
            if msg_type == "internal" and msg.get("internal", {}).get("type") == "dialout":
                await self._handle_dialout(msg)
            elif msg_type == "message":
                await self._handle_webrtc_message(msg["message"])
            elif msg_type == "room" and msg.get("id") == "bridge-room":
                self._room_joined_event.set()
            elif msg_type == "control" and msg.get("control", {}).get("data", {}).get("type") == "hangup":
                # Sent when the call's virtual phone session is disinvited
                # (the room participant hung up in Talk, or the room's call
                # ended) or targeted with an explicit hangup control message
                # - the server rewrites the recipient to our own session
                # either way. Only one call is ever active, so there is
                # nothing else to disambiguate against.
                print("[talk] Hangup control received - ending the active call")
                self.call_manager.hangup()

    async def _handle_dialout(self, msg: dict):
        """Talk's native "call a phone number" UI triggers this. The
        request carries the room id it's for - there is no separate
        mechanism to learn it, and no default to fall back to."""
        request_id = msg.get("id", "")
        dialout = msg["internal"]["dialout"]
        roomid = dialout.get("roomid", "")
        number = dialout.get("request", {}).get("number", "")
        if config.dialout_strip_prefix and number.startswith(config.dialout_strip_prefix):
            number = number[len(config.dialout_strip_prefix):]
        print(f"[talk] Dialout request for {number} in room {roomid}")
        result = self.call_manager.dial(number)
        if "error" in result:
            await self._send_dialout_response(request_id, roomid, error=result["error"])
            return
        call_id = result["call_id"]
        with self._call_sessions_lock:
            self._call_sessions[call_id] = {"kind": "dialout", "number": number, "roomid": roomid}
        # The signaling server expects an "accepted" status synchronously
        # (within a fixed timeout) - actual ring/connect progress is
        # reported later via separate, unsolicited status updates.
        await self._send_dialout_response(request_id, roomid, call_id=call_id, status="accepted")

    async def _send_dialout_response(self, request_id: str, roomid: str, *, call_id: str = None, status: str = None, error: str = None):
        dialout_payload = {"roomid": roomid}
        if error is not None:
            dialout_payload["type"] = "error"
            dialout_payload["error"] = {"code": "call_failed", "message": error}
        else:
            dialout_payload["type"] = "status"
            dialout_payload["status"] = {"callid": call_id, "status": status}
        envelope = {"type": "internal", "internal": {"type": "dialout", "dialout": dialout_payload}}
        if request_id:
            envelope["id"] = request_id
        await self.ws.send(json.dumps(envelope))

    async def _send_dialout_status(self, call_id: str, roomid: str, status: str):
        """Unsolicited status update (ringing/connected/rejected/cleared) -
        not correlated to a request id, unlike the initial "accepted" reply."""
        await self._send_dialout_response("", roomid, call_id=call_id, status=status)

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
        # Join the room so the signaling server routes our self-addressed
        # offer to that room's Janus instance and generates a real SDP
        # answer - a plain addsession alone does not do this. This makes us
        # briefly ineligible for a new dialout request (the signaling server
        # excludes any internal session that is in a room from its dialout
        # candidates) - acceptable since only one call is ever handled at a
        # time here anyway; _teardown_call leaves the room again once done.
        self._room_joined_event.clear()
        await self.ws.send(json.dumps({"id": "bridge-room", "type": "room", "room": {"roomid": roomid}}))
        try:
            await asyncio.wait_for(self._room_joined_event.wait(), timeout=5)
        except asyncio.TimeoutError:
            print(f"[talk] Warning: no room-join confirmation for {roomid} within 5s, publishing anyway")
        virtual_sessionid = await self._add_virtual_session(sip_call_id, roomid=roomid, number=number, caller=True)
        pc = RTCPeerConnection()
        pc.addTrack(SipAudioTrack(rtp_session))

        # Ending a call in Talk's UI does not send an explicit hangup control
        # message for this room type - what actually happens is the Janus
        # publisher gets torn down at the DTLS level. That alone does not
        # reliably move iceConnectionState to "closed"/"failed" in aiortc,
        # but connectionState (which also factors in the DTLS/SCTP state)
        # does. This is the only observed signal that the human ended the
        # call, so it drives the actual SIP hangup - without it, the phone
        # side stays connected indefinitely regardless of what Talk shows.
        hangup_triggered = False

        def maybe_hangup(source: str, state: str):
            nonlocal hangup_triggered
            print(f"[talk] Publish {source}: {state}")
            if not hangup_triggered and state in ("failed", "closed", "disconnected"):
                hangup_triggered = True
                self.call_manager.hangup()

        @pc.on("iceconnectionstatechange")
        async def on_ice_state_change():
            maybe_hangup("ICE state", pc.iceConnectionState)

        @pc.on("connectionstatechange")
        async def on_connection_state_change():
            maybe_hangup("connection state", pc.connectionState)

        with self._call_sessions_lock:
            entry = self._call_sessions.setdefault(sip_call_id, {})
            entry["virtual_sessionid"] = virtual_sessionid
            entry["pc"] = pc
            # The offer is addressed to our OWN session, not the virtual
            # one - a virtual session (addsession) only represents the call
            # in the participant list, it has no real client attached that
            # could answer a WebRTC offer. Publishing as ourselves is what
            # makes the signaling server/Janus generate the SDP answer.
            entry["ws_peer_sessionid"] = self.own_sessionid

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await self.ws.send(json.dumps({
            "id": f"bridge-offer-{sip_call_id}", "type": "message",
            "message": {
                "recipient": {"type": "session", "sessionid": self.own_sessionid},
                "data": {
                    "to": self.own_sessionid, "type": "offer", "sid": secrets.token_hex(8), "roomType": "video",
                    "payload": {"nick": number, "type": "offer", "sdp": pc.localDescription.sdp},
                    "audiocodec": "opus",
                },
            },
        }))
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
        if entry.get("kind") == "dialout":
            await self._send_dialout_status(sip_call_id, roomid, "cleared")
        print(f"[talk] Call {sip_call_id} ended, virtual session removed")
        if "pc" in entry:
            # The signaling server permanently drops a "start-dialout"
            # session from its dialout candidates the moment it joins any
            # room (confirmed in its own source - there is no code path
            # that re-adds it, including on leaving the room again). The
            # only way to become dialout-eligible again is a fresh
            # connection with a new hello, so force a reconnect - handled
            # by the retry loop in _connect_and_serve.
            await self.ws.close()

    def _entry_roomid(self, call_id: str) -> str:
        # Dialout calls carry their own room id, learned from the request
        # that started them. Inbound calls have no such association in the
        # protocol - config.default_room_token is the only option there.
        with self._call_sessions_lock:
            return self._call_sessions.get(call_id, {}).get("roomid") or config.default_room_token

    # -- thread-safe entry points for sip_core.CallManager callbacks ------
    def on_call_connected(self, *, call_id, direction, rtp):
        with self._call_sessions_lock:
            number = self._call_sessions.get(call_id, {}).get("number", "")
        roomid = self._entry_roomid(call_id)

        async def _connected():
            await self._publish_call_audio(call_id, rtp, roomid, number)
            if direction == "outbound":
                await self._send_dialout_status(call_id, roomid, "connected")

        asyncio.run_coroutine_threadsafe(_connected(), self.loop)

    def on_call_ended(self, *, call_id, reason):
        roomid = self._entry_roomid(call_id)
        asyncio.run_coroutine_threadsafe(self._teardown_call(call_id, roomid), self.loop)

    def on_call_failed(self, *, call_id, reason):
        roomid = self._entry_roomid(call_id)
        asyncio.run_coroutine_threadsafe(self._send_dialout_status(call_id, roomid, "rejected"), self.loop)

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
