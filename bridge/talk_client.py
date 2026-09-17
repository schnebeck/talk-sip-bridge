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
import base64
import fractions
import hashlib
import hmac
import http.cookiejar
import json
import secrets
import threading
import traceback
import urllib.error
import urllib.request

import numpy as np
import websockets
from aiortc import RTCIceCandidate, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AudioStreamTrack
from av import AudioFrame

from agc import Agc
from config import config

AUDIO_SAMPLE_RATE = 48000
RTP_QUEUE_POLL_INTERVAL = 0.02  # matches one 20ms RTP packet - keeps frame pacing real-time


def _resample_linear(pcm: np.ndarray, in_rate: int, out_rate: int) -> np.ndarray:
    """Linear interpolation, not sample repetition/decimation - repetition
    produces a staircase waveform (harsh, aliased); this is a large audible
    improvement for a small amount of code, without adding a dependency for
    full sinc-based resampling. Used in both directions: SIP audio (8/16kHz)
    up to Talk's 48kHz, and Talk's audio back down to the SIP call's rate."""
    if in_rate == out_rate:
        return pcm
    n_in = len(pcm)
    if n_in == 0:
        return pcm
    n_out = max(1, round(n_in * out_rate / in_rate))
    x_out = np.linspace(0, n_in - 1, n_out)
    return np.interp(x_out, np.arange(n_in), pcm).astype(np.int16)


def _frame_to_mono_pcm(frame: AudioFrame) -> np.ndarray:
    """aiortc audio frames may be stereo - the SIP side only ever carries
    mono, so any inbound-from-Talk audio is downmixed here before it can be
    resampled/sent."""
    arr = frame.to_ndarray()
    channels = len(frame.layout.channels) if frame.layout else 1
    if arr.ndim == 2 and arr.shape[0] > 1:
        mono = arr.astype(np.float64).mean(axis=0)
    else:
        row = arr.flatten()
        mono = row.reshape(-1, channels).astype(np.float64).mean(axis=1) if channels > 1 else row.astype(np.float64)
    return np.clip(mono, -32768, 32767).astype(np.int16)


def _parse_ice_candidate(cand_str: str, sdp_mid=None, sdp_mline_index=0) -> RTCIceCandidate:
    parts = cand_str.replace("candidate:", "").split()
    return RTCIceCandidate(
        component=int(parts[1]), foundation=parts[0], ip=parts[4], port=int(parts[5]),
        priority=int(parts[3]), protocol=parts[2], type=parts[7],
        sdpMid=sdp_mid, sdpMLineIndex=sdp_mline_index,
    )


def _talk_ocs_request(opener, base: str, auth_header: str, method: str, path: str, body: dict = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"OCS-APIREQUEST": "true", "Accept": "application/json", "Authorization": auth_header}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base}{path}", data=data, method=method, headers=headers)
    with opener.open(req, timeout=5) as resp:
        return resp.read()


def _talk_ring_start_sync(roomid: str, nc_user: str, nc_app_password: str):
    """Uses Talk's own OCS call-signaling API (POST .../call/{token}) to
    make the bridge's Nextcloud account one more device joining the room's
    call - confirmed live that this is what actually triggers real ringing
    (push notification, full-screen call UI) on every other device logged
    into that account or already in the room, not just a chat message or a
    custom notification.

    Joining the call requires an existing room session first - confirmed
    live that calling the call endpoint directly, without having joined the
    room, fails with 404 (Talk's RequireParticipant check rejects it). The
    room join is cookie/session-based (like a browser), so the returned
    opener/cookie jar has to be kept and reused for the matching
    _talk_ring_stop_sync call - joining a fresh session there and leaving
    immediately would end the call before anyone had a chance to answer it.

    Returns (opener, session_id) on success, or (None, None) if the feature
    isn't configured or the calls failed. session_id is the room session id
    Talk assigned to this triggering join (from the join-room response) -
    the signaling server broadcasts this same account joining the call to
    every room member including our own internal client, so it has to be
    recognized and excluded from "a human accepted" detection, or the
    bridge would immediately mistake its own ring-trigger for an accept."""
    if not nc_user or not nc_app_password:
        return None, None
    base = config.backend_url.rstrip('/')
    auth_header = "Basic " + base64.b64encode(f"{nc_user}:{nc_app_password}".encode()).decode()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        join_resp = _talk_ocs_request(opener, base, auth_header, "POST", f"/ocs/v2.php/apps/spreed/api/v4/room/{roomid}/participants/active", {})
        session_id = json.loads(join_resp)["ocs"]["data"].get("sessionId")
        _talk_ocs_request(opener, base, auth_header, "POST", f"/ocs/v2.php/apps/spreed/api/v4/call/{roomid}", {"flags": 1})
        return opener, session_id
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        print(f"[talk] Starting Talk call ring for room {roomid} failed: {e!r}")
        return None, None


def _talk_ring_stop_sync(opener, roomid: str, nc_user: str, nc_app_password: str):
    """Ends what _talk_ring_start_sync started - leaves the call, then the
    room, using the same session (opener) so Talk attributes it to the
    right participant."""
    base = config.backend_url.rstrip('/')
    auth_header = "Basic " + base64.b64encode(f"{nc_user}:{nc_app_password}".encode()).decode()
    try:
        _talk_ocs_request(opener, base, auth_header, "DELETE", f"/ocs/v2.php/apps/spreed/api/v4/call/{roomid}?all=false")
        _talk_ocs_request(opener, base, auth_header, "DELETE", f"/ocs/v2.php/apps/spreed/api/v4/room/{roomid}/participants/active")
    except (urllib.error.URLError, OSError) as e:
        print(f"[talk] Stopping Talk call ring for room {roomid} failed: {e!r}")


def _run_coro_logged(coro, loop, label: str):
    """asyncio.run_coroutine_threadsafe() returns a concurrent.futures.Future
    whose exception is silently dropped unless something calls .result() on
    it - unlike a plain asyncio Task, it does NOT log on garbage collection.
    Every sip_core.CallManager callback in this module schedules its async
    work this way from a plain worker thread, so without this wrapper any
    exception anywhere in that coroutine (offer/answer negotiation, codec
    setup, ...) simply vanishes with zero trace, no matter how bad."""
    future = asyncio.run_coroutine_threadsafe(coro, loop)

    def _log_if_failed(f):
        exc = f.exception()
        if exc is not None:
            print(f"[talk] ERROR in {label}: {exc!r}")
            traceback.print_exception(type(exc), exc, exc.__traceback__)

    future.add_done_callback(_log_if_failed)
    return future


class SipAudioTrack(AudioStreamTrack):
    """Reads decoded PCM (mono, at the RtpSession's negotiated sample rate -
    8kHz for PCMU, 16kHz for G.722) from an RtpSession's receive queue,
    upsampled to AUDIO_SAMPLE_RATE."""

    def __init__(self, rtp_session):
        super().__init__()
        self.rtp_session = rtp_session
        self._pts = 0
        self._silence = np.zeros(rtp_session.samples_per_packet, dtype=np.int16)
        self._agc = Agc(target_peak=config.agc_target_peak, max_gain=config.agc_max_gain) if config.agc_enabled else None

    async def recv(self):
        try:
            pcm_in = await asyncio.to_thread(self.rtp_session.recv_queue.get, True, RTP_QUEUE_POLL_INTERVAL)
        except Exception:
            pcm_in = self._silence
        if self._agc is not None:
            pcm_in = self._agc.process(pcm_in)
        pcm_48k = _resample_linear(pcm_in, self.rtp_session.sample_rate, AUDIO_SAMPLE_RATE)
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
        self._room_roster = {}  # room sessionid -> {"is_human": bool} - who else is in the room, for subscribing to their audio

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
            elif msg_type == "error" and msg.get("id") == "bridge-room" and msg.get("error", {}).get("code") == "already_joined":
                # Our own internal session never explicitly leaves a room
                # between calls (see _handle_incoming_ring/_publish_call_audio),
                # so a later join attempt for the same room routinely hits
                # this instead of a real join confirmation - it means we are
                # in the room already, which is just as good.
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
            elif msg_type == "event":
                event = msg.get("event", {})
                print(f"[talk] DEBUG event target={event.get('target')} type={event.get('type')} raw={json.dumps(event)[:1500]}")
                if event.get("target") == "room" and event.get("type") == "join":
                    self._handle_room_join(event.get("join") or [])
                elif event.get("target") == "room" and event.get("type") == "leave":
                    with self._call_sessions_lock:
                        for sessionid in event.get("leave") or []:
                            self._room_roster.pop(sessionid, None)
                elif event.get("target") == "participants" and event.get("type") == "update":
                    await self._handle_participants_update(event.get("update") or {})
            else:
                print(f"[talk] DEBUG other message type={msg_type} raw={json.dumps(msg)[:1500]}")

    def _handle_room_join(self, join_entries: list):
        """Tracks room roster (for finding a human to subscribe to, see
        _subscribe_human_audio) and, for our own virtual "phone" session
        specifically, its server-assigned room session id - unrelated to the
        name we picked for it in addsession/removesession.
        _handle_participants_update needs that id to recognize our own
        virtual session in the room roster and not mistake it for another
        participant still on the call. Matched via the call id we set in
        addsession's user.callid, which round-trips back to us unchanged."""
        for item in join_entries:
            sessionid = item.get("sessionid")
            if not sessionid:
                continue
            user = item.get("user") or {}
            is_phone = user.get("type") == "phone"
            is_internal = "start-dialout" in (item.get("features") or [])
            with self._call_sessions_lock:
                self._room_roster[sessionid] = {"is_human": not is_phone and not is_internal}
            if is_phone:
                call_id = user.get("callid")
                if call_id:
                    with self._call_sessions_lock:
                        entry = self._call_sessions.get(call_id)
                        if entry is not None:
                            entry["virtual_room_sessionid"] = sessionid

    async def _handle_dialout(self, msg: dict):
        """Talk's native "call a phone number" UI triggers this. The
        request carries the room id it's for - there is no separate
        mechanism to learn it, and no default to fall back to."""
        request_id = msg.get("id", "")
        dialout = msg["internal"]["dialout"]
        roomid = dialout.get("roomid", "")
        number = dialout.get("request", {}).get("number", "")
        line = self.call_manager.line
        if line.dialout_strip_prefix and number.startswith(line.dialout_strip_prefix):
            number = number[len(line.dialout_strip_prefix):]
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
        with self._call_sessions_lock:
            for entry in self._call_sessions.values():
                if entry.get("ws_peer_sessionid") == sender_sessionid and "pc" in entry:
                    pc, peer, is_subscriber = entry["pc"], self.own_sessionid, False
                    break
                if entry.get("human_sessionid") == sender_sessionid and "sub_pc" in entry:
                    pc, peer, is_subscriber = entry["sub_pc"], sender_sessionid, True
                    break
            else:
                print(f"[talk] DEBUG unmatched webrtc message from sender={sender_sessionid} data.type={data.get('type')}")
                return

        msg_type = data.get("type")
        if msg_type == "answer":
            # Answer to our own publish offer (self-addressed - see
            # _publish_call_audio). Only ever happens on the publish pc.
            await pc.setRemoteDescription(RTCSessionDescription(sdp=data["payload"]["sdp"], type="answer"))
        elif msg_type == "offer" and is_subscriber:
            # The server's offer for the stream we requested via
            # requestoffer (see _subscribe_human_audio) - we answer it.
            await pc.setRemoteDescription(RTCSessionDescription(sdp=data["payload"]["sdp"], type="offer"))
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            await self.ws.send(json.dumps({
                "id": f"bridge-subanswer-{secrets.token_hex(4)}", "type": "message",
                "message": {
                    "recipient": {"type": "session", "sessionid": peer},
                    "data": {
                        "to": peer, "type": "answer", "sid": data.get("sid"), "roomType": "video",
                        "payload": {"type": "answer", "sdp": pc.localDescription.sdp},
                    },
                },
            }))
        elif msg_type == "candidate":
            cand_data = data.get("payload", {}).get("candidate", {})
            cand_str = cand_data.get("candidate", "")
            if not cand_str:
                return
            try:
                await pc.addIceCandidate(_parse_ice_candidate(
                    cand_str, sdp_mid=cand_data.get("sdpMid"), sdp_mline_index=cand_data.get("sdpMLineIndex", 0)))
            except Exception as e:
                print(f"[talk] Could not add ICE candidate: {e!r}")

    async def _handle_participants_update(self, update: dict):
        """Primary call-end signal. Confirmed via the signaling server's own
        logs: when the human clicks "Anruf beenden", only their own
        publisher/room gets torn down - our publisher (the SIP call's audio)
        is never touched, so iceconnectionstatechange/connectionstatechange
        on our own RTCPeerConnection (see _publish_call_audio) never fire in
        this case. FlagInCall = 1 in the server's own bitmask, so
        inCall & 1 == 0 means a participant is not (or no longer) in the
        call.

        This event has two observed shapes, both confirmed via the
        signaling server's own source (server/room.go):
        - Room.PublishUsersInCallChanged (backend-driven "incall" updates):
          carries a "changed" list of just the sessions whose flag moved.
        - Room.NotifySessionChanged -> publishUsersChangedWithInternal (a
          browser client leaving the call - the path actually taken by
          Talk's "Anruf beenden" button): carries no "changed" list at all,
          only a full "users" room-membership snapshot with each entry's
          current inCall value - so leaving has to be inferred from that
          snapshot rather than a delta.

        Also doubles, symmetrically, as the accept signal for a call still
        waiting on _handle_incoming_ring: there the question is the opposite
        one - has a human just joined the call - checked against the same
        three event shapes."""
        with self._call_sessions_lock:
            own_and_virtual = {self.own_sessionid}
            active_call_id = None
            waiting_call_id = None
            waiting_entry = None
            for call_id, entry in self._call_sessions.items():
                if entry.get("virtual_sessionid"):
                    own_and_virtual.add(entry["virtual_sessionid"])
                if entry.get("virtual_room_sessionid"):
                    own_and_virtual.add(entry["virtual_room_sessionid"])
                if "pc" in entry:
                    active_call_id = call_id
                elif entry.get("waiting_for_accept"):
                    waiting_call_id = call_id
                    waiting_entry = entry
        if active_call_id is None and waiting_call_id is None:
            return

        def is_in_call(raw) -> bool:
            try:
                return bool(int(raw) & 1)
            except (TypeError, ValueError):
                return False

        if waiting_call_id is not None:
            # Deliberately no "all: true" check here, unlike the hangup
            # detection below: our own ring-trigger session (see
            # _handle_incoming_ring/_talk_ring_start_sync) is the one
            # putting the room into its call state in the first place, and
            # that transition is exactly what "all: true" reports - reacting
            # to it would make the bridge mistake its own ring for a human
            # accepting. A real human joining afterwards is a per-session
            # change instead ("changed" delta or a "users" snapshot), which
            # can be filtered by session id, so only those are treated as an
            # accept signal.
            exclude = own_and_virtual | {waiting_entry.get("talk_ring_sessionid")}
            accepted_sessionid = None
            for item in update.get("changed") or []:
                session_id = item.get("sessionId") or item.get("sessionid")
                if session_id and session_id not in exclude and "inCall" in item and is_in_call(item["inCall"]):
                    accepted_sessionid = session_id
                    break
            if accepted_sessionid is None:
                for u in update.get("users") or []:
                    session_id = u.get("sessionId") or u.get("sessionid")
                    if session_id and session_id not in exclude and is_in_call(u.get("inCall")):
                        accepted_sessionid = session_id
                        break
            if accepted_sessionid is not None:
                print(f"[talk] Human joined the call - accepting {waiting_call_id}")
                with self._call_sessions_lock:
                    entry = self._call_sessions.get(waiting_call_id)
                    if entry is not None:
                        entry["waiting_for_accept"] = False  # avoid double-triggering answer()
                        # _find_human_in_room()'s room-roster scan can pick a
                        # stale entry (a chat-relay session that silently
                        # dropped without the signaling server ever sending
                        # a "room"/"leave" for it - confirmed live: it kept
                        # returning an hours-old dead session, and requesting
                        # its audio failed with "client_not_found") - the
                        # session id that just got detected as in-call right
                        # here is the ground truth, so use it directly
                        # instead of trusting the roster.
                        entry["accepted_sessionid"] = accepted_sessionid
                opener = waiting_entry.get("talk_ring_opener")
                if opener is not None:
                    # The ring-trigger session's job is done now that a real
                    # client has joined - leave it so the bridge doesn't
                    # linger as a phantom extra participant.
                    await self._stop_talk_ring(self._entry_roomid(waiting_call_id), opener, self.call_manager.line)
                self.call_manager.answer()
            return

        if update.get("all"):
            # Room.PublishUsersInCallChangedAll: a whole-room "the call
            # itself ended" broadcast - no changed/users list, just a
            # room-wide incall value (lowercase json key, unlike the
            # per-session "inCall" used elsewhere). This is what actually
            # fires when "Anruf beenden" is clicked - confirmed via a live
            # capture of the raw event.
            in_call_raw = update.get("incall", update.get("inCall"))
            if in_call_raw is not None and not is_in_call(in_call_raw):
                print("[talk] Room call ended - ending SIP side")
                self.call_manager.hangup()
            return

        for item in update.get("changed") or []:
            session_id = item.get("sessionId") or item.get("sessionid")
            if not session_id or session_id in own_and_virtual:
                continue
            if "inCall" in item and not is_in_call(item["inCall"]):
                print(f"[talk] Participant {session_id} left the call - ending SIP side")
                self.call_manager.hangup()
                return

        users = update.get("users")
        if users is not None:
            anyone_else_in_call = any(
                is_in_call(u.get("inCall"))
                for u in users
                if (u.get("sessionId") or u.get("sessionid")) not in own_and_virtual
            )
            if not anyone_else_in_call:
                print("[talk] No other participant left in the call - ending SIP side")
                self.call_manager.hangup()

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
    async def _publish_call_audio(self, sip_call_id: str, rtp_session, roomid: str, number: str, human_sessionid_hint: str = None):
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

        async def poll_connection_state():
            # Redundant against the event handlers above: observed at least
            # once that neither fired even though the connection had
            # genuinely gone bad, leaving the phone call connected
            # indefinitely with nothing to end it. Polling is a fallback,
            # not the primary mechanism - state changes are still normally
            # caught immediately by the events.
            while not hangup_triggered:
                await asyncio.sleep(5)
                if pc.connectionState in ("failed", "closed", "disconnected") or \
                        pc.iceConnectionState in ("failed", "closed", "disconnected"):
                    maybe_hangup("state poll", pc.connectionState)
                    return

        asyncio.ensure_future(poll_connection_state())

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

        # Prefer the session id the accept-detection just confirmed as
        # in-call over scanning the room roster: the roster only ever gains
        # entries (see _handle_room_join) and the signaling server does not
        # reliably send a "room"/"leave" for a chat-relay session that just
        # silently drops (e.g. the mobile app backgrounded) - confirmed
        # live, this made _find_human_in_room() keep returning an hours-old
        # dead session, and requesting its audio failed with
        # "client_not_found" instead of ever reaching the real one.
        human_sessionid = human_sessionid_hint or await self._find_human_in_room()
        if human_sessionid:
            asyncio.ensure_future(self._subscribe_human_audio(sip_call_id, rtp_session, human_sessionid))
        else:
            print(f"[talk] No other participant found in room {roomid} - phone side will not hear Talk's audio")

    async def _find_human_in_room(self, retries: int = 5, delay: float = 0.3) -> str | None:
        """The room roster (see _handle_room_join) is populated from events
        that normally arrive before we even start publishing (the human is
        the one who triggered this call by already being in the room) - the
        retry loop only covers the rare case where our own room-join
        confirmation raced ahead of the "room"/"join" broadcast for them."""
        for _ in range(retries):
            with self._call_sessions_lock:
                for sessionid, info in self._room_roster.items():
                    if info.get("is_human"):
                        return sessionid
            await asyncio.sleep(delay)
        return None

    # -- subscribing to the human's audio, to relay it to the phone -------
    async def _subscribe_human_audio(self, sip_call_id: str, rtp_session, human_sessionid: str):
        sub_pc = RTCPeerConnection()

        @sub_pc.on("track")
        def on_track(track):
            if track.kind != "audio":
                return
            task = asyncio.ensure_future(self._relay_human_audio(sip_call_id, rtp_session, track))
            with self._call_sessions_lock:
                entry = self._call_sessions.get(sip_call_id)
                if entry is not None:
                    entry["relay_task"] = task

        with self._call_sessions_lock:
            entry = self._call_sessions.setdefault(sip_call_id, {})
            entry["sub_pc"] = sub_pc
            entry["human_sessionid"] = human_sessionid

        await self.ws.send(json.dumps({
            "id": f"bridge-reqoffer-{sip_call_id}", "type": "message",
            "message": {
                "recipient": {"type": "session", "sessionid": human_sessionid},
                "data": {"type": "requestoffer", "roomType": "video"},
            },
        }))
        print(f"[talk] Requested audio from {human_sessionid} for {sip_call_id}")

    async def _relay_human_audio(self, sip_call_id: str, rtp_session, track):
        """Reads Talk's audio (48kHz, from whatever the human's device
        captured) and forwards it to the phone side, downsampled to the RTP
        session's negotiated codec rate. Ends naturally when the subscriber
        pc is closed (_teardown_call) - track.recv() then raises."""
        try:
            while True:
                frame = await track.recv()
                pcm = _frame_to_mono_pcm(frame)
                pcm_out = _resample_linear(pcm, frame.sample_rate, rtp_session.sample_rate)
                await asyncio.to_thread(rtp_session.send_pcm, pcm_out)
        except Exception as e:
            print(f"[talk] Human audio relay for {sip_call_id} ended ({e!r})")

    async def _teardown_call(self, sip_call_id: str, roomid: str):
        with self._call_sessions_lock:
            entry = self._call_sessions.pop(sip_call_id, None)
        if not entry:
            return
        if entry.get("waiting_for_accept") and entry.get("talk_ring_opener") is not None:
            # The call ended (cancelled - a physical phone in the same
            # parallel ring group answered first, or the caller hung up)
            # before a human joined it in Talk - leave the ring-trigger call/
            # room session, nothing else to publish/remove.
            await self._stop_talk_ring(roomid, entry["talk_ring_opener"], self.call_manager.line)
        if "relay_task" in entry:
            entry["relay_task"].cancel()
        if "sub_pc" in entry:
            await entry["sub_pc"].close()
        if "pc" in entry:
            await entry["pc"].close()
        if "virtual_sessionid" in entry:
            await self._remove_virtual_session(entry["virtual_sessionid"], roomid)
        if entry.get("kind") == "dialout":
            await self._send_dialout_status(sip_call_id, roomid, "cleared")
        print(f"[talk] Call {sip_call_id} ended, virtual session removed")
        if "pc" in entry or entry.get("waiting_for_accept"):
            # The signaling server permanently drops a "start-dialout"
            # session from its dialout candidates the moment it joins any
            # room (confirmed in its own source - there is no code path
            # that re-adds it, including on leaving the room again) - and
            # _handle_incoming_ring joins the room too, just to watch for an
            # accept. The only way to become dialout-eligible again is a
            # fresh connection with a new hello, so force a reconnect -
            # handled by the retry loop in _connect_and_serve.
            await self.ws.close()

    def _entry_roomid(self, call_id: str) -> str:
        # Dialout calls carry their own room id, learned from the request
        # that started them. Inbound calls have no such association in the
        # protocol - this line's own configured default room is the only
        # option there.
        with self._call_sessions_lock:
            return self._call_sessions.get(call_id, {}).get("roomid") or self.call_manager.line.default_room_token

    # -- thread-safe entry points for sip_core.CallManager callbacks ------
    def on_call_connected(self, *, call_id, direction, rtp):
        with self._call_sessions_lock:
            entry = self._call_sessions.get(call_id, {})
            number = entry.get("number", "")
            human_sessionid_hint = entry.get("accepted_sessionid")
        roomid = self._entry_roomid(call_id)

        async def _connected():
            await self._publish_call_audio(call_id, rtp, roomid, number, human_sessionid_hint=human_sessionid_hint)
            if direction == "outbound":
                await self._send_dialout_status(call_id, roomid, "connected")

        _run_coro_logged(_connected(), self.loop, f"on_call_connected({call_id})")

    def on_call_ended(self, *, call_id, reason):
        roomid = self._entry_roomid(call_id)
        _run_coro_logged(self._teardown_call(call_id, roomid), self.loop, f"on_call_ended({call_id})")

    def on_call_failed(self, *, call_id, reason):
        roomid = self._entry_roomid(call_id)
        _run_coro_logged(self._send_dialout_status(call_id, roomid, "rejected"), self.loop, f"on_call_failed({call_id})")

    def on_incoming_call(self, *, call_id, caller):
        # The signaling protocol has no ringing/accept-decline exchange for
        # inbound calls ("addsession" represents an already-connected call,
        # and dialout is Talk-initiated only), so there is no native way to
        # ask before picking up over the signaling protocol itself.
        # config.auto_answer_calls picks up immediately with no Talk-side
        # involvement; short of that, a line with notify_user configured
        # instead rings a real Nextcloud notification and waits for a human
        # to join the call in Talk (see _handle_incoming_ring) - e.g. to let
        # Talk act as one more device in a FritzBox-side parallel ring
        # group, racing the physical phones. A line with neither just rings
        # unnoticed by Talk, same as any other registered phone nobody
        # happens to pick up.
        with self._call_sessions_lock:
            self._call_sessions[call_id] = {"kind": "inbound", "number": caller}
        if config.auto_answer_calls:
            print(f"[talk] Incoming call {call_id} from {caller} - answering with real audio")
            self.call_manager.answer()
            return
        _run_coro_logged(self._handle_incoming_ring(call_id, caller), self.loop, f"on_incoming_call({call_id})")

    async def _handle_incoming_ring(self, call_id: str, caller: str):
        line = self.call_manager.line
        roomid = line.default_room_token
        if not roomid or not line.notify_user or not line.notify_app_password:
            print(f"[talk] Incoming call {call_id} from {caller} on line {line.id} - "
                  f"no notify_user/notify_app_password/default room configured, leaving it ringing")
            return
        opener, ring_sessionid = await asyncio.to_thread(
            _talk_ring_start_sync, roomid, line.notify_user, line.notify_app_password)
        if opener is None:
            return
        self._room_joined_event.clear()
        await self.ws.send(json.dumps({"id": "bridge-room", "type": "room", "room": {"roomid": roomid}}))
        try:
            await asyncio.wait_for(self._room_joined_event.wait(), timeout=5)
        except asyncio.TimeoutError:
            print(f"[talk] Warning: no room-join confirmation for {roomid} while waiting for {call_id} to be accepted")
        with self._call_sessions_lock:
            entry = self._call_sessions.get(call_id)
            if entry is not None:
                entry["waiting_for_accept"] = True
                entry["talk_ring_opener"] = opener
                entry["talk_ring_sessionid"] = ring_sessionid
        print(f"[talk] Ringing {line.notify_user} for call {call_id} from {caller}, watching room {roomid} for accept")

    async def _stop_talk_ring(self, roomid: str, opener, line):
        await asyncio.to_thread(_talk_ring_stop_sync, opener, roomid, line.notify_user, line.notify_app_password)


def start_in_background(call_manager) -> TalkClient:
    client = TalkClient(call_manager)
    thread = threading.Thread(target=client.run_forever, daemon=True)
    thread.start()
    return client
