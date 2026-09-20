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
daemon.py) and bridges to sip_call.CallManager, whose callbacks fire from
plain worker threads via asyncio.run_coroutine_threadsafe.
"""
import asyncio
import hashlib
import hmac
import json
import re
import secrets
import threading
import traceback

import websockets

import talk_ocs
from call import DIALOUT, INBOUND, Call
from call_media import CallMedia
from config import config
from room_state import FLAG_IN_CALL, RoomCallState, is_room_wide_call_end

# Call flags, shared by Talk's clients and the signaling server. Talk's own
# clients only ever subscribe to a participant carrying AUDIO or VIDEO
# (spreed's webrtc.js: userHasStreams()), which is what makes the
# distinction below matter rather than being cosmetic.
FLAG_WITH_AUDIO = 2
FLAG_WITH_PHONE = 8

# A single requestoffer is not enough: the other side's publisher may not
# exist yet when it goes out, and the signaling server answers that with
# "client_not_found" rather than queuing. Talk's own client re-requests
# every 10s for exactly this reason.
SUBSCRIBE_RETRY_INTERVAL = 5
SUBSCRIBE_MAX_ATTEMPTS = 6

def _sip_display_name(from_header: str) -> str:
    """Reduces a SIP From header to something usable as a participant name
    in Talk - the caller's display name if it sent one, else the user part
    of the SIP URI. A plain dialled number passes through unchanged."""
    quoted = re.match(r'\s*"([^"]+)"', from_header)
    if quoted:
        return quoted.group(1)
    uri_user = re.search(r"sip:([^@;>]+)", from_header)
    if uri_user:
        return uri_user.group(1)
    return from_header.split(";")[0].strip()


def _run_coro_logged(coro, loop, label: str):
    """asyncio.run_coroutine_threadsafe() returns a concurrent.futures.Future
    whose exception is silently dropped unless something calls .result() on
    it - unlike a plain asyncio Task, it does NOT log on garbage collection.
    Every sip_call.CallManager callback in this module schedules its async
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


class TalkClient:
    def __init__(self, call_manager):
        self.call_manager = call_manager
        self.ws = None
        self.own_sessionid = None
        self.loop = None
        self._call_sessions = {}  # sip call_id -> Call
        self._call_sessions_lock = threading.Lock()  # entries are written from both the asyncio loop and SIP worker threads
        self._room_joined_event = asyncio.Event()
        self._room_roster = {}  # room sessionid -> {"is_human": bool} - who else is in the room, for subscribing to their audio
        self._room_call = RoomCallState()

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
                # "internal-incall" makes this client responsible for its own
                # inCall flags (see _set_incall). Without it the server marks
                # the session as in-call with audio the moment it connects -
                # so every Talk client in the room asks for this session's
                # audio stream immediately, long before any call exists,
                # gets "client_not_found", and then only retries every 10
                # seconds. That alone keeps the first seconds of a real call
                # silent. With the flag, the session announces audio only
                # once its publisher actually exists.
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
        self._room_call = RoomCallState()  # a new connection knows nothing about any room yet
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
                self._hangup_sip("Hangup control received - ending the active call")
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
                            entry.virtual_room_sessionid = sessionid

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
            self._call_sessions[call_id] = Call(sip_call_id=call_id, kind=DIALOUT,
                                                number=number, roomid=roomid)
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
        """Routes one WebRTC message to the call it belongs to. Which
        connection that is - the publisher or the subscriber - follows from
        who sent it, and only the call's own media knows that."""
        data = message.get("data", {})
        sender_sessionid = message.get("sender", {}).get("sessionid")
        with self._call_sessions_lock:
            for entry in self._call_sessions.values():
                match = entry.media.peer_for(sender_sessionid) if entry.media else None
                if match:
                    media, (pc, is_subscriber) = entry.media, match
                    break
            else:
                print(f"[talk] DEBUG unmatched webrtc message from sender={sender_sessionid} data.type={data.get('type')}")
                return

        msg_type = data.get("type")
        if msg_type == "answer":
            # Answer to our own publish offer, which is self-addressed - so
            # it only ever arrives on the publishing connection.
            await media.accept_publisher_answer(data["payload"]["sdp"])
        elif msg_type == "offer" and is_subscriber:
            answer_sdp = await media.answer_subscriber_offer(data["payload"]["sdp"])
            if answer_sdp is None:
                return  # a late duplicate; answering it would reset a working connection
            peer = sender_sessionid
            await self.ws.send(json.dumps({
                "id": f"bridge-subanswer-{secrets.token_hex(4)}", "type": "message",
                "message": {
                    "recipient": {"type": "session", "sessionid": peer},
                    "data": {
                        "to": peer, "type": "answer", "sid": data.get("sid"), "roomType": "video",
                        "payload": {"type": "answer", "sdp": answer_sdp},
                    },
                },
            }))
        elif msg_type == "candidate":
            await media.add_candidate(pc, data.get("payload"))

    async def _handle_participants_update(self, update: dict):
        """The room's two answers this bridge acts on: somebody joined the
        call it is ringing for, or the call it is in has ended.

        Besides per-session updates the server also broadcasts a room-wide
        "the call itself ended" (an "all" entry with a lowercase "incall"),
        which is what Talk's own "end call" button produces."""
        entered, left = self._room_call.apply(update)

        with self._call_sessions_lock:
            ours = {self.own_sessionid}
            active_call_id = None
            waiting_call_id = None
            waiting_entry = None
            for call_id, entry in self._call_sessions.items():
                ours |= entry.own_session_ids()
                if entry.is_publishing:
                    active_call_id = call_id
                elif entry.waiting_for_accept:
                    waiting_call_id = call_id
                    waiting_entry = entry
        if active_call_id is None and waiting_call_id is None:
            return

        if waiting_call_id is not None:
            accepted_sessionid = self._room_call.accepted_by(entered, ours)
            if accepted_sessionid is None:
                return
            print(f"[talk] Human joined the call - accepting {waiting_call_id}")
            with self._call_sessions_lock:
                entry = self._call_sessions.get(waiting_call_id)
                if entry is not None:
                    entry.waiting_for_accept = False  # avoid double-triggering answer()
                    # Whose audio to subscribe to: the session that just
                    # joined, rather than whatever the room roster offers -
                    # that can still hold sessions which dropped without a
                    # "leave", and asking one of those for its audio fails
                    # with client_not_found.
                    entry.accepted_sessionid = accepted_sessionid
            # Answer first: everything the caller hears from here on waits
            # on this, and stopping the ring costs two OCS round trips that
            # have nothing to do with the phone line. A failure here must
            # not reach the message loop - losing the signaling connection
            # over one call that cannot be answered would take every later
            # call down with it.
            try:
                self.call_manager.answer()
            except Exception as e:
                print(f"[talk] Answering {waiting_call_id} failed: {e!r}")
                traceback.print_exc()
            opener = waiting_entry.talk_ring_opener
            if opener is not None:
                _run_coro_logged(
                    self._stop_talk_ring(self._entry_roomid(waiting_call_id), opener, self.call_manager.line),
                    self.loop, f"stop ring for {waiting_call_id}")
            return

        if update.get("all"):
            if is_room_wide_call_end(update):
                self._hangup_sip("Room call ended - ending SIP side")
            return

        if not left:
            return
        if not self._room_call.anyone_in_call_besides(ours):
            self._hangup_sip(f"Everyone but this bridge left the call ({len(left)} session(s)) - ending SIP side")

    def _call_still_running(self, sip_call_id: str) -> bool:
        """_teardown_call removes the call's entry, so its presence is what
        says the call is still worth working on - relevant across the
        seconds that publishing spends gathering ICE."""
        with self._call_sessions_lock:
            return sip_call_id in self._call_sessions

    def _hangup_sip(self, reason: str = None):
        """Ends the SIP call behind the room's call. There is no call
        manager when this client runs without a SIP side, as
        test_publish_and_verify.py drives it - then there is nothing to
        hang up, and reaching for it would kill the message loop."""
        if reason:
            print(f"[talk] {reason}")
        if self.call_manager is not None:
            self.call_manager.hangup()

    async def _set_incall(self, flags: int):
        """Announces this session's call state to the room. Only meaningful
        because "internal-incall" is declared in _hello - otherwise the
        server owns these flags. Talk clients start asking for this
        session's audio as soon as FLAG_WITH_AUDIO shows up here, so it is
        set once the publisher exists and cleared when the call ends."""
        if self.ws is None:
            return
        try:
            await self.ws.send(json.dumps({
                "type": "internal",
                "internal": {"type": "incall", "incall": {"incall": flags}},
            }))
        except Exception as e:
            print(f"[talk] Could not update inCall flags to {flags}: {e!r}")

    # -- virtual session management (addsession/removesession) -----------
    async def _add_virtual_session(self, sip_call_id: str, *, roomid: str, number: str, caller: bool) -> str:
        """Adds the phone participant Talk shows in the room. This is a
        name plate only: in MCU mode a virtual session can never carry
        media, because publishers exist exclusively under a real client
        session's own id (the signaling server looks a publisher up by the
        raw recipient session id, with no virtual-to-owner mapping). The
        call's audio therefore rides on this bridge's own session, which
        carries the caller's name via the publish offer's "nick".

        FLAG_WITH_AUDIO is deliberately absent: it would make Talk clients
        request a stream from this session that cannot exist, leaving them
        retrying against a participant that never answers. The flags have
        to be spelled out because "internal-incall" turns off the server's
        own default for virtual sessions too."""
        virtual_sessionid = f"phone-{secrets.token_hex(8)}"
        await self.ws.send(json.dumps({
            "type": "internal",
            "internal": {
                "type": "addsession",
                "addsession": {
                    "sessionid": virtual_sessionid,
                    "roomid": roomid,
                    "incall": FLAG_IN_CALL | FLAG_WITH_PHONE,
                    # No "options" actor here, which is why Talk shows the
                    # caller as "Gast": passing actorType/actorId would have
                    # the signaling server register this session with
                    # Nextcloud as that actor, but Nextcloud rejects an
                    # actor that is not already an invited participant of
                    # the room ("The user is not invited to this room"), and
                    # the whole addsession fails with it. Naming a caller
                    # properly needs a real phone attendee in the room
                    # first, which an inbound call has no way to create.
                    # displayname is the field Talk renders participants by
                    # (a real user arrives as user.displayname too) - without
                    # it the caller shows up as "Gast".
                    "user": {"type": "phone", "callid": sip_call_id, "number": number,
                             "displayname": number},
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
    async def _join_room_for_publishing(self, roomid: str) -> None:
        """Publishing needs the bridge in the room: that is what makes the
        signaling server route its self-addressed offer to the room's Janus
        and answer it. addsession alone does not.

        It costs this connection its dialout eligibility for as long as it
        lives (see docs/CONCEPT.md point 3), which is why _teardown_call
        reconnects afterwards."""
        self._room_joined_event.clear()
        await self.ws.send(json.dumps({"id": "bridge-room", "type": "room", "room": {"roomid": roomid}}))
        try:
            await asyncio.wait_for(self._room_joined_event.wait(), timeout=5)
        except asyncio.TimeoutError:
            print(f"[talk] Warning: no room-join confirmation for {roomid} within 5s, publishing anyway")

    async def _send_publish_offer(self, sip_call_id: str, sdp: str, display_name: str) -> None:
        await self.ws.send(json.dumps({
            "id": f"bridge-offer-{sip_call_id}", "type": "message",
            "message": {
                "recipient": {"type": "session", "sessionid": self.own_sessionid},
                "data": {
                    "to": self.own_sessionid, "type": "offer", "sid": secrets.token_hex(8),
                    "roomType": "video",
                    # This nick names the tile that actually carries the
                    # call's audio - the phone participant added via
                    # addsession is a name plate without media.
                    "payload": {"nick": display_name, "type": "offer", "sdp": sdp},
                    "audiocodec": "opus",
                },
            },
        }))

    async def _publish_call_audio(self, sip_call_id: str, rtp_session, roomid: str, number: str, human_sessionid_hint: str = None):
        await self._join_room_for_publishing(roomid)
        if not self._call_still_running(sip_call_id):
            print(f"[talk] {sip_call_id} ended before publishing started - nothing to publish")
            return

        display_name = _sip_display_name(number)
        virtual_sessionid = await self._add_virtual_session(sip_call_id, roomid=roomid, number=display_name, caller=True)

        media = CallMedia(sip_call_id, rtp_session, on_connection_lost=self._hangup_sip)
        media.open_publisher(self.own_sessionid)
        with self._call_sessions_lock:
            # get, not setdefault: _teardown_call removing the entry is what
            # says the call is over, and recreating it here would hide that.
            entry = self._call_sessions.get(sip_call_id)
            if entry is not None:
                entry.virtual_sessionid = virtual_sessionid
                entry.media = media

        sdp = await media.publisher_offer()
        if not self._call_still_running(sip_call_id):
            # Gathering ICE takes seconds, and a caller who gives up in the
            # meantime tears the call down underneath us. Publishing anyway
            # would leave a live publisher running for a call that is gone.
            print(f"[talk] {sip_call_id} ended while gathering ICE - discarding the publisher")
            await media.close()
            await self._remove_virtual_session(virtual_sessionid, roomid)
            return

        await self._send_publish_offer(sip_call_id, sdp, display_name)
        print(f"[talk] Publishing call audio for {sip_call_id} as virtual session {virtual_sessionid}")

        # Only now: announcing audio any earlier makes Talk clients ask for a
        # stream that does not exist yet, and they back off to one retry
        # every 10 seconds after that. It also satisfies the signaling
        # server's rule that both sides must be in the call before either
        # may subscribe to the other, which the request below depends on.
        await self._set_incall(FLAG_IN_CALL | FLAG_WITH_AUDIO)

        # Prefer the session accept detection just saw entering the call over
        # scanning the room roster: the roster only ever gains entries, and
        # the server does not reliably announce a chat-relay session that
        # drops silently (a backgrounded mobile app) - confirmed live, that
        # made the roster hand out an hours-old dead session whose audio
        # could only ever fail with "client_not_found".
        human_sessionid = human_sessionid_hint or await self._find_human_in_room()
        if human_sessionid:
            asyncio.ensure_future(self._subscribe_human_audio(sip_call_id, media, human_sessionid))
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
    async def _subscribe_human_audio(self, sip_call_id: str, media, human_sessionid: str):
        """Asks for the other side's audio until an offer arrives. One
        request is not enough: their publisher may not exist yet, and the
        server rejects such a request with "client_not_found" rather than
        queuing it."""
        def on_receiving(track):
            print(f"[talk] Receiving audio from {human_sessionid} for {sip_call_id}")

        media.open_subscriber(human_sessionid, on_receiving=on_receiving)

        for attempt in range(SUBSCRIBE_MAX_ATTEMPTS):
            with self._call_sessions_lock:
                entry = self._call_sessions.get(sip_call_id)
                still_current = entry is not None and entry.media is media
            if not still_current:
                return  # call ended, or a newer subscription replaced this one
            try:
                await self.ws.send(json.dumps({
                    "id": f"bridge-reqoffer-{sip_call_id}", "type": "message",
                    "message": {
                        "recipient": {"type": "session", "sessionid": human_sessionid},
                        "data": {"type": "requestoffer", "roomType": "video"},
                    },
                }))
            except Exception as e:
                print(f"[talk] Could not request audio from {human_sessionid}: {e!r}")
                return
            print(f"[talk] Requested audio from {human_sessionid} for {sip_call_id} "
                  f"(attempt {attempt + 1}/{SUBSCRIBE_MAX_ATTEMPTS})")
            try:
                await asyncio.wait_for(media.offer_arrived.wait(), timeout=SUBSCRIBE_RETRY_INTERVAL)
                return
            except asyncio.TimeoutError:
                continue
        print(f"[talk] No audio offer from {human_sessionid} for {sip_call_id} - "
              f"phone side stays silent for this call")

    async def _teardown_call(self, sip_call_id: str, roomid: str):
        with self._call_sessions_lock:
            entry = self._call_sessions.pop(sip_call_id, None)
        if not entry:
            return
        if entry.waiting_for_accept and entry.talk_ring_opener is not None:
            # The call ended (cancelled - a physical phone in the same
            # parallel ring group answered first, or the caller hung up)
            # before a human joined it in Talk - leave the ring-trigger call/
            # room session, nothing else to publish/remove.
            await self._stop_talk_ring(roomid, entry.talk_ring_opener, self.call_manager.line)
        was_publishing = entry.is_publishing
        if entry.media:
            await entry.media.close()
        if entry.virtual_sessionid:
            await self._remove_virtual_session(entry.virtual_sessionid, roomid)
        if was_publishing:
            # The publisher is gone, so stop advertising audio - otherwise
            # Talk clients keep asking this session for a stream that no
            # longer exists.
            await self._set_incall(0)
        if entry.kind == DIALOUT:
            await self._send_dialout_status(sip_call_id, roomid, "cleared")
        print(f"[talk] Call {sip_call_id} ended, virtual session removed")
        if entry.talk_ring_opener is not None and not entry.waiting_for_accept:
            # This bridge started the room's call for this phone call and
            # somebody answered it, so it ends with the phone call too -
            # see talk_ocs.end_room_call for what being left in it does.
            # A call nobody answered is not ended here: it was already
            # given up by _stop_talk_ring above.
            line = self.call_manager.line
            await asyncio.to_thread(talk_ocs.end_room_call, roomid,
                                    line.notify_user, line.notify_app_password)
        if was_publishing or entry.waiting_for_accept:
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
            entry = self._call_sessions.get(call_id)
            return (entry.roomid if entry else "") or self.call_manager.line.default_room_token

    # -- thread-safe entry points for sip_call.CallManager callbacks ------
    def on_call_connected(self, *, call_id, direction, rtp):
        with self._call_sessions_lock:
            entry = self._call_sessions.get(call_id)
            number = entry.number if entry else ""
            human_sessionid_hint = entry.accepted_sessionid if entry else None
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
            self._call_sessions[call_id] = Call(sip_call_id=call_id, kind=INBOUND, number=caller)
        if config.auto_answer_calls:
            print(f"[talk] Incoming call {call_id} from {caller} - answering with real audio")
            self.call_manager.answer()
            return
        _run_coro_logged(self._handle_incoming_ring(call_id, caller), self.loop, f"on_incoming_call({call_id})")

    async def _handle_incoming_ring(self, call_id: str, caller: str):
        """Rings Talk for an inbound call and waits for somebody to answer.

        The order matters. Joining the room first establishes what the
        room's call looks like before this call exists, because answering
        is recognised as a change to that (see room_state.RoomCallState), and
        arming the call before ringing closes the window in between.
        Ringing first lets a fast answer land in the very snapshot that
        establishes the starting state, where it is indistinguishable from
        someone who was already in a call - confirmed live: a client that
        answered within a second was never noticed and the phone rang
        out."""
        line = self.call_manager.line
        roomid = line.default_room_token
        if not roomid or not line.notify_user or not line.notify_app_password:
            print(f"[talk] Incoming call {call_id} from {caller} on line {line.id} - "
                  f"no notify_user/notify_app_password/default room configured, leaving it ringing")
            return

        self._room_joined_event.clear()
        await self.ws.send(json.dumps({"id": "bridge-room", "type": "room", "room": {"roomid": roomid}}))
        try:
            # Confirmation normally arrives in milliseconds. Waiting longer
            # than this would eat into the caller's patience for no gain -
            # the room state that answering is compared against also
            # arrives while the OCS calls below are still running.
            await asyncio.wait_for(self._room_joined_event.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            print(f"[talk] No room-join confirmation for {roomid} yet, ringing anyway")

        with self._call_sessions_lock:
            entry = self._call_sessions.get(call_id)
            if entry is None:
                return  # the caller gave up while the room was being joined
            entry.waiting_for_accept = True

        opener, ring_sessionid = await asyncio.to_thread(
            talk_ocs.start_ring, roomid, line.notify_user, line.notify_app_password)
        if opener is None:
            with self._call_sessions_lock:
                entry = self._call_sessions.get(call_id)
                if entry is not None:
                    entry.waiting_for_accept = False
            return

        with self._call_sessions_lock:
            entry = self._call_sessions.get(call_id)
            if entry is not None:
                entry.talk_ring_opener = opener
                entry.talk_ring_sessionid = ring_sessionid
        print(f"[talk] Ringing {line.notify_user} for call {call_id} from {caller}, watching room {roomid} for accept")

    async def _stop_talk_ring(self, roomid: str, opener, line):
        await asyncio.to_thread(talk_ocs.stop_ring, opener, roomid, line.notify_user, line.notify_app_password)


def start_in_background(call_manager) -> TalkClient:
    client = TalkClient(call_manager)
    thread = threading.Thread(target=client.run_forever, daemon=True)
    thread.start()
    return client
