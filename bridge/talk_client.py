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
import json
import re
import secrets
import threading
import traceback

import websockets

from sip_messages import caller_display_name, caller_number
import talk_messages
import talk_sip_bridge
import talk_ocs
from call import DIALOUT, INBOUND, Call
from call_media import CallMedia
from config import config
from dialin_ivr import DialInIvr
from room_state import RoomCallState, is_room_wide_call_end
from subscription import Action, Subscription
from talk_messages import FLAG_IN_CALL, FLAG_WITH_AUDIO, dialout_actor

# A single requestoffer is not enough: the other side's publisher may not
# exist yet when it goes out, and the signaling server answers that with
# "client_not_found" rather than queuing. Talk's own client re-requests
# every 10s for exactly this reason.
SUBSCRIBE_RETRY_INTERVAL = 5
SUBSCRIBE_MAX_ATTEMPTS = 6
# A refused answer is repaired by building the subscription again, and
# that has to be rare: each rebuild throws away one that may be seconds
# from working, and a handful in a row is not a repair but a storm -
# measured, seven in five seconds, with nothing left standing.
RESUBSCRIBE_MAX_ATTEMPTS = 2
RESUBSCRIBE_DELAY = 3

def is_conference_call(line, dialled: str, caller: str) -> bool:
    """Whether this call is one the caller gets to choose a conversation
    for - which is also a call the bridge answers by itself.

    Two conditions, and the second exists for a line that carries one
    number for everything. A conference number there is also the number
    somebody's own phone rings on, and answering every call to it would
    take their calls away; restricting it by who is calling leaves
    external calls ringing exactly as before while the gateway's own
    extensions reach the dialogue."""
    if dialled not in line.conference_numbers:
        return False
    if not line.conference_callers:
        return True
    return bool(re.fullmatch(line.conference_callers, caller_number(caller)))


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
        # Two connections, because one cannot do both jobs. The server
        # drops a "start-dialout" session from its dialout candidates the
        # moment it joins any room and only a fresh hello puts it back
        # (hub.go), so the connection that takes dialout requests must
        # never enter a room, and the one that carries a call must.
        self.ws = None              # the room connection: media, room events
        self.own_sessionid = None   # ... and its session, which publishes the audio
        self.dialout_ws = None      # the dialout connection: never in a room
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
        try:
            self.loop.run_until_complete(self._serve_both())
        finally:
            # Nothing below returns on its own, so reaching this means the
            # bridge is deaf: the SIP side keeps answering the phone and
            # nothing it does can reach Talk.
            print("[talk] ERROR the signaling client stopped - restart the daemon")

    async def _serve_both(self):
        """Both connections, each reconnecting on its own. Neither
        depends on the other being up: a dialout can be refused while the
        room side is reconnecting, and a call in progress is not
        disturbed by the dialout side dropping."""
        await asyncio.gather(self._supervise("room", self._serve_room),
                             self._supervise("dialout", self._serve_dialout))

    async def _supervise(self, role: str, serve):
        """Keeps one connection's loop alive whatever it does.

        `_serve` loops forever, so nothing should arrive here - but a
        connection that quietly stops is the worst failure this bridge
        has: calls keep being answered and none of them reaches Talk,
        with nothing in the journal to say why. Restarting is always
        better than the two of them ending together, which is what a
        bare `gather` would do."""
        while True:
            try:
                await serve()
                print(f"[talk] ERROR the {role} connection loop ended by itself - restarting")
            except Exception as e:
                print(f"[talk] ERROR the {role} connection loop failed: {e!r} - restarting")
                traceback.print_exc()
            await asyncio.sleep(config.sip_response_timeout)

    async def _serve(self, role: str, features: list, settled, handle):
        while True:
            try:
                async with websockets.connect(config.ws_url) as ws:
                    settled(ws, await self._hello(ws, role, features))
                    await self._read(role, ws, handle)
            except Exception as e:
                print(f"[talk] {role} connection lost ({e!r}), reconnecting ...")
            settled(None, None)
            await asyncio.sleep(config.sip_response_timeout)

    async def _read(self, role: str, ws, handle):
        """One message at a time, each costing only itself.

        A message that cannot be handled must not reach the connection:
        letting it through would reconnect, and a reconnect during a call
        takes that call's signaling down over a message that had nothing
        to do with it."""
        async for raw in ws:
            try:
                await handle(json.loads(raw))
            except Exception as e:
                print(f"[talk] ERROR handling a {role} message: {e!r}")
                traceback.print_exc()

    async def _serve_room(self):
        def settled(ws, sessionid):
            self.ws = ws
            self.own_sessionid = sessionid
            # A new connection is in no room and knows nothing about one.
            self._room_call = RoomCallState()
            self._room_roster = {}

        await self._serve("room", talk_messages.ROOM_FEATURES, settled, self._handle_room_message)

    async def _serve_dialout(self):
        def settled(ws, _sessionid):
            self.dialout_ws = ws

        await self._serve("dialout", talk_messages.DIALOUT_FEATURES, settled,
                          self._handle_dialout_message)

    async def _hello(self, ws, role: str, features: list) -> str:
        await ws.send(json.dumps(
            talk_messages.hello(config.internal_secret, config.backend_url, features)))
        await ws.recv()  # welcome banner
        resp = json.loads(await ws.recv())
        sessionid = resp["hello"]["sessionid"]
        print(f"[talk] {role} connection up as internal client, session {sessionid}")
        return sessionid

    async def _handle_dialout_message(self, msg: dict):
        """The dialout connection carries one kind of traffic in each
        direction: requests from Talk, and this bridge's answers about
        the call they started."""
        if msg.get("type") == "internal" and msg.get("internal", {}).get("type") == "dialout":
            await self._handle_dialout(msg)
        else:
            # Nothing else is expected here; if the server starts sending
            # something new, this is where it surfaces.
            print(f"[talk] Unhandled message on the dialout connection: "
                  f"{json.dumps(msg)[:300]}")

    # -- incoming messages from the signaling server ---------------------
    async def _handle_room_message(self, msg: dict):
        msg_type = msg.get("type")
        if msg_type == "message":
            await self._handle_webrtc_message(msg["message"])
        elif msg_type == "room" and msg.get("id") == "bridge-room":
            self._room_joined_event.set()
        elif msg_type == "error" and msg.get("id") == "bridge-room" and msg.get("error", {}).get("code") == "already_joined":
            # Joining twice - a dialout joins when it starts ringing
            # and the publisher joins again when it is answered - is
            # answered with this instead of a confirmation. It means
            # we are in the room already, which is just as good.
            self._room_joined_event.set()
        elif msg_type == "error" and str(msg.get("id", "")).startswith("bridge-subanswer-"):
            await self._subscription_answer_refused(
                str(msg.get("id"))[len("bridge-subanswer-"):], msg.get("error", {}))
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
            if config.signaling_debug:
                print(f"[talk] event {event.get('target')}/{event.get('type')}: "
                      f"{json.dumps(event)[:1500]}")
            if event.get("target") == "room" and event.get("type") == "join":
                self._handle_room_join(event.get("join") or [])
            elif event.get("target") == "room" and event.get("type") == "leave":
                with self._call_sessions_lock:
                    for sessionid in event.get("leave") or []:
                        self._room_roster.pop(sessionid, None)
            elif event.get("target") == "participants" and event.get("type") == "update":
                await self._handle_participants_update(event.get("update") or {})
        elif config.signaling_debug:
            print(f"[talk] unhandled {msg_type}: {json.dumps(msg)[:1000]}")

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
            # Any internal feature, not one particular one: this bridge
            # keeps two connections declaring different features, and
            # asking for the wrong one made it subscribe to its own room
            # connection - the caller then heard nothing from Talk.
            is_internal = (sessionid == self.own_sessionid
                           or bool(talk_messages.INTERNAL_FEATURES
                                   .intersection(item.get("features") or [])))
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
        request = dialout.get("request", {})
        number = request.get("number", "")
        actor = dialout_actor(request.get("options") or {})
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
                                                number=number, roomid=roomid, actor=actor)
        # No name plate while it only rings. A virtual session is
        # announced as being in the call, and Talk then stops the
        # ringback the caller is waiting to hear - they sit in front of
        # a line that looks connected and carries nothing. It also makes
        # the room count as occupied (`hasActiveSessionsInCall`), which
        # stops Nextcloud noticing when the call empties.
        #
        # It would buy nothing either: the one gesture that ends a
        # ringing dialout in Talk is "end meeting for everyone", and
        # that reaches a room as `PublishUsersInCallChangedAll`, which
        # notifies `*ClientSession` only (room.go) - never the owner of
        # a virtual session. Hearing it needs a connection that is in
        # the room, which this one cannot be.

        # The signaling server expects an "accepted" status synchronously
        # (within a fixed timeout) - actual ring/connect progress is
        # reported later via separate, unsolicited status updates.
        await self._send_dialout_response(request_id, roomid, call_id=call_id, status="accepted")

        # The room connection goes in now, while the phone rings, not
        # when the call connects: ending the call in Talk reaches only
        # the sessions in the room (room.go notifies `*ClientSession`
        # and nothing else), and a caller who gives up during the
        # ringing is otherwise left listening to a phone that rings on
        # until this side's own timeout - measured at twelve seconds of
        # ringing past the moment Talk ended the call. This connection
        # never takes a dialout, so being in a room costs it nothing.
        # Alongside the reply, not before it: waiting for the join
        # confirmation would hold up an answer the server times out on.
        asyncio.ensure_future(self._join_room_for_publishing(roomid))

    async def _send_dialout_response(self, request_id: str, roomid: str, *, call_id: str = None, status: str = None, error: str = None):
        """Always on the dialout connection: the server matches a reply
        against the request it is pending on, and that bookkeeping is
        per session (hub.go's ProcessResponse)."""
        message = (talk_messages.dialout_error(roomid, error, request_id) if error is not None
                   else talk_messages.dialout_status(roomid, call_id, status, request_id))
        if self.dialout_ws is None:
            print(f"[talk] No dialout connection to answer {request_id or 'a status update'} on")
            return
        await self.dialout_ws.send(json.dumps(message))

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
                # Routine: mute, unmute and nick changes are addressed to
                # the phone by every client in the room, and none of them
                # belongs to a media connection.
                if config.signaling_debug:
                    print(f"[talk] {data.get('type')} from {sender_sessionid[:12]} "
                          f"belongs to no call")
                return

        msg_type = data.get("type")
        if msg_type == "answer":
            # Answer to our own publish offer, which is self-addressed - so
            # it only ever arrives on the publishing connection.
            await media.accept_publisher_answer(data["payload"]["sdp"])
        elif msg_type == "offer" and is_subscriber:
            await self._answer_offer(media, sender_sessionid, data)
        elif msg_type == "candidate":
            await media.add_candidate(pc, data.get("payload"))

    async def _answer_offer(self, media, peer: str, data: dict):
        """Answers an offer for a subscription, if it is still the one
        being negotiated.

        The server offers again when it re-attaches its end, and answers
        naming a handle it has dropped are refused. The call's
        Subscription decides which offer counts; a second answer for an
        offer it has moved past is not sent at all."""
        state = self._subscription_for(media.sip_call_id)
        if state is None:
            return
        step = state.offer(data.get("sid"))
        if step.action is not Action.ANSWER:
            return   # audio already flows, or this negotiation is history
        answer_sdp = await media.answer_subscriber_offer(data["payload"]["sdp"])
        if answer_sdp is None or state.generation != step.generation:
            return
        await self.ws.send(json.dumps(talk_messages.subscribe_answer(
            peer, step.sid, answer_sdp, sip_call_id=media.sip_call_id)))
        state.answer_sent(step.generation)

    async def _subscription_answer_refused(self, sip_call_id: str, error: dict):
        """The server would not take our answer for the other side's
        audio: it re-attaches its own end while the publisher is not
        sending yet, without offering again, and an answer naming the
        handle it dropped is refused ("answer message sid does not match
        subscriber sid").

        What follows from that is the Subscription's decision - repair,
        or stop trying. Acting on every refusal directly is what produced
        seven rebuilds in five seconds, none of which survived the
        next."""
        with self._call_sessions_lock:
            entry = self._call_sessions.get(sip_call_id)
            media = entry.media if entry else None
            human = media.human_sessionid if media else None
            state = entry.subscription if entry else None
        if media is None or not human or state is None:
            return
        step = state.refused()
        if not step:
            return
        print(f"[talk] The server refused our answer for {human}'s audio "
              f"({error.get('code')})")
        await self._pursue(sip_call_id, media, human, step)

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
            ringing_dialout_id = None
            for call_id, entry in self._call_sessions.items():
                ours |= entry.own_session_ids()
                if entry.kind == DIALOUT and not entry.is_publishing:
                    # Placed, not answered yet: there is no media and no
                    # virtual session, but there is a phone ringing.
                    ringing_dialout_id = call_id
                if entry.is_publishing:
                    active_call_id = call_id
                elif entry.waiting_for_accept:
                    waiting_call_id = call_id
                    waiting_entry = entry
        if active_call_id is not None and entered:
            # Whoever just joined has heard none of what the phone
            # announced when the call started.
            await self._announce_phone_state(active_call_id, peers=entered)

        if active_call_id is None and waiting_call_id is None and ringing_dialout_id is None:
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

        if ringing_dialout_id is not None and left and not self._room_call.anyone_in_call_besides(ours):
            # The last person left while the phone was still ringing.
            self._hangup_sip("Nobody is left in the call - withdrawing the outbound call")
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
            await self.ws.send(json.dumps(talk_messages.set_incall(flags)))
        except Exception as e:
            print(f"[talk] Could not update inCall flags to {flags}: {e!r}")

    # -- virtual session management (addsession/removesession) -----------
    async def _add_virtual_session(self, sip_call_id: str, *, roomid: str, number: str,
                                   displayname: str = "", caller: bool = True,
                                   actor: dict = None) -> str:
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
        if config.phone_participant == "none":
            print(f"[talk] No phone participant for {sip_call_id} "
                  f"(BRIDGE_PHONE_PARTICIPANT=none) - the call shows as this bridge's "
                  f"own session, named after the caller")
            return None
        virtual_sessionid = f"phone-{secrets.token_hex(8)}"
        with_audio = config.phone_participant == "audio"
        flags = talk_messages.incall_flags(with_audio, actor)
        # Joined first with the bare in-call flag, then updated to the
        # full set. Adding a session tells Nextcloud what it joined with
        # but leaves the signaling server's own in-call set untouched
        # (Room.AddSession), and while the session is not in that set
        # every client asking for the phone's stream is refused. A change
        # through update_session is what puts it there.
        await self.ws.send(json.dumps(talk_messages.add_session(
            virtual_sessionid, roomid, call_id=sip_call_id, number=number,
            displayname=displayname or number, with_audio=with_audio, actor=actor,
            incall=FLAG_IN_CALL)))
        await self.ws.send(json.dumps(
            talk_messages.update_session(virtual_sessionid, roomid, flags)))
        print(f"[talk] Phone participant {virtual_sessionid} announced "
              f"{'with' if with_audio else 'without'} audio"
              + (f", as {actor['actorType']}/{str(actor['actorId'])[:12]}" if actor
                 else ", known to the signaling server only"))
        return virtual_sessionid

    async def _remove_virtual_session(self, virtual_sessionid: str, roomid: str):
        if not virtual_sessionid:
            return
        await self.ws.send(json.dumps(talk_messages.remove_session(virtual_sessionid, roomid)))

    # -- the room this bridge is in --------------------------------------
    async def _join_room_for_publishing(self, roomid: str) -> None:
        """Puts the room connection in the room. Two things need it, and
        they happen at different moments: publishing the call's audio -
        being in the room is what makes the server route this session's
        self-addressed offer to the room's Janus, which addsession alone
        does not - and hearing what the room does, which has to start
        while the phone is still ringing.

        Only ever the room connection: this costs a connection its
        dialout eligibility for good (see docs/CONCEPT.md point 3)."""
        if self.ws is None:
            print(f"[talk] No room connection to join {roomid} with")
            return
        self._room_joined_event.clear()
        await self.ws.send(json.dumps(talk_messages.join_room(roomid)))
        try:
            await asyncio.wait_for(self._room_joined_event.wait(), timeout=5)
        except asyncio.TimeoutError:
            print(f"[talk] Warning: no room-join confirmation for {roomid} within 5s, carrying on")

    async def _leave_room(self) -> None:
        """Leaves whatever room the room connection is in, once the call
        it was there for is over. Staying would leave the bridge counted
        among the room's sessions long after the call - and a room that
        still holds a session is a room Nextcloud thinks somebody is
        in."""
        if self.ws is None:
            return
        try:
            await self.ws.send(json.dumps(talk_messages.leave_room()))
        except Exception as e:
            print(f"[talk] Could not leave the room: {e!r}")
            return
        self._room_call = RoomCallState()
        self._room_roster = {}

    async def _send_publish_offer(self, sip_call_id: str, sdp: str, display_name: str) -> None:
        await self.ws.send(json.dumps(talk_messages.publish_offer(
            self.own_sessionid, sip_call_id, sdp, display_name)))

    async def _publish_call_audio(self, sip_call_id: str, rtp_session, roomid: str, number: str, human_sessionid_hint: str = None):
        await self._join_room_for_publishing(roomid)
        if not self._call_still_running(sip_call_id):
            print(f"[talk] {sip_call_id} ended before publishing started - nothing to publish")
            return

        display_name = caller_display_name(number)
        with self._call_sessions_lock:
            waiting = self._call_sessions.get(sip_call_id)
            actor = waiting.actor if waiting else None
        virtual_sessionid = await self._add_virtual_session(
            sip_call_id, roomid=roomid, number=caller_number(number),
            displayname=display_name, caller=True, actor=actor)

        media = CallMedia(sip_call_id, rtp_session, on_connection_lost=self._hangup_sip)
        media.open_publisher(
            self.own_sessionid,
            on_talking=lambda talking: _run_coro_logged(
                self._publish_talking(sip_call_id, roomid, talking), self.loop,
                f"talking({sip_call_id})"))
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
        print(f"[talk] Publishing call audio for {sip_call_id}"
              + (f" as virtual session {virtual_sessionid}" if virtual_sessionid else ""))

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
        await self._announce_phone_state(sip_call_id)

        if human_sessionid:
            asyncio.ensure_future(self._subscribe_human_audio(sip_call_id, media, human_sessionid))
        else:
            print(f"[talk] No other participant found in room {roomid} - phone side will not hear Talk's audio")

    async def _publish_talking(self, sip_call_id: str, roomid: str, talking: bool):
        """Tells the room whether the caller is speaking.

        Through the session's own flags, which is the one channel that
        says something *about the phone*: the server publishes them as a
        "participants"/"flags" event naming the phone's session, while
        anything this bridge sends as a message is stamped with the
        bridge's own session id and belongs to no tile a client draws.

        The absence of FLAG_MUTED_SPEAKING is the statement that matters
        - it says the microphone is on. A session left at zero flags is
        one the server mentions to nobody (room.go skips flags == 0), and
        clients then infer the microphone from the audio, which is the
        muted marker coming and going with the caller's speech."""
        with self._call_sessions_lock:
            entry = self._call_sessions.get(sip_call_id)
            virtual = entry.virtual_sessionid if entry else None
        if not virtual or self.ws is None:
            return
        flags = talk_messages.FLAG_TALKING if talking else 0
        try:
            await self.ws.send(json.dumps(
                talk_messages.update_session(virtual, roomid, flags=flags)))
        except Exception as e:
            print(f"[talk] Could not publish the talking state for {sip_call_id}: {e!r}")

    async def _announce_phone_state(self, sip_call_id: str, peers=None):
        """Tells the others what the phone's microphone and camera are
        doing, and what to call it.

        Talk's own client does exactly this on joining and again for
        everyone who joins later (`_sendCurrentStateTo`): unmute or mute
        per media kind, plus its name. A participant that announces
        nothing leaves the others to guess from the audio level, which is
        the muted-microphone marker appearing and disappearing with the
        caller's speech.

        A phone is always unmuted - there is no mute button on this side
        of the call - and never has video."""
        with self._call_sessions_lock:
            entry = self._call_sessions.get(sip_call_id)
            if entry is None or not entry.is_publishing:
                return
            # The caller id, not the gateway's word for the device: a
            # handset announces itself as "FritzFon schwarz", which tells
            # a room nothing, while the number identifies who is calling.
            name = caller_number(entry.number) or caller_display_name(entry.number)
            ours = {self.own_sessionid} | entry.own_session_ids()
            targets = set(peers) if peers is not None else self._room_call.others_in_call(ours)
        targets -= ours
        if not targets:
            return
        for peer in sorted(targets):
            for state, payload in (("unmute", {"name": "audio"}),
                                   ("mute", {"name": "video"}),
                                   ("nickChanged", {"name": name})):
                try:
                    await self.ws.send(json.dumps(talk_messages.peer_state(peer, state, payload)))
                except Exception as e:
                    print(f"[talk] Could not tell {peer} about the phone: {e!r}")
                    return
        print(f"[talk] Told {len(targets)} participant(s) that {name}'s microphone is on")

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
        """Asks for the other side's audio and follows the negotiation to
        audio or to giving up.

        What to do at each turn is decided by the call's Subscription (see
        subscription.py); everything here is the doing - sending, waiting,
        rebuilding. Splitting it that way is what keeps two repairs from
        running at once, which is what tore the return direction down."""
        def on_receiving(track):
            print(f"[talk] Receiving audio from {human_sessionid} for {sip_call_id}")

        media.open_subscriber(human_sessionid, on_receiving=on_receiving)
        state = self._subscription_for(sip_call_id)
        if state is None:
            return

        def flowing():
            state.media_arrived()
            print(f"[talk] Talk's audio reaches the phone for {sip_call_id}")

        media.on_media_flowing = flowing
        await self._pursue(sip_call_id, media, human_sessionid, state.start())

    def _subscription_for(self, sip_call_id: str):
        with self._call_sessions_lock:
            entry = self._call_sessions.get(sip_call_id)
            if entry is None:
                return None
            if entry.subscription is None:
                entry.subscription = Subscription(
                    max_attempts=SUBSCRIBE_MAX_ATTEMPTS,
                    request_delay=SUBSCRIBE_RETRY_INTERVAL,
                    rebuild_delay=RESUBSCRIBE_DELAY)
            return entry.subscription

    async def _pursue(self, sip_call_id: str, media, human_sessionid: str, step):
        """Carries out one step of the negotiation, and the waiting it
        asks for."""
        if not step:
            return
        if step.delay:
            await asyncio.sleep(step.delay)
        with self._call_sessions_lock:
            entry = self._call_sessions.get(sip_call_id)
            still_the_call = entry is not None and entry.media is media
            state = entry.subscription if entry else None
        if not still_the_call or state is None:
            return  # the call ended, or a newer subscription replaced this one
        if media.human_sessionid != human_sessionid:
            # The call is now listening to somebody else. A step decided
            # for the previous one would ask the server about a session
            # this call has nothing to do with any more.
            return
        if not state.still_current(step):
            # Decided before the wait, overtaken during it: an offer
            # arrived, audio started flowing, or the attempts ran out.
            # Acting anyway is how a repair reaches into a working
            # connection and closes it.
            return

        if step.action is Action.GIVE_UP:
            print(f"[talk] No audio from {human_sessionid} for {sip_call_id} after "
                  f"{state.attempts} attempts - the phone side stays silent for this call")
            return
        if step.action is Action.REBUILD:
            if not await media.prepare_for_new_offer():
                return
            print(f"[talk] Subscribing to {human_sessionid} again for {sip_call_id}")
        if step.action in (Action.REQUEST, Action.REBUILD):
            try:
                await self.ws.send(json.dumps(
                    talk_messages.request_offer(sip_call_id, human_sessionid)))
            except Exception as e:
                print(f"[talk] Could not request audio from {human_sessionid}: {e!r}")
                return
            print(f"[talk] Requested audio from {human_sessionid} for {sip_call_id} "
                  f"(attempt {state.attempts}/{SUBSCRIBE_MAX_ATTEMPTS})")
            # Nothing may arrive at all: the server answers a request for
            # a publisher that does not exist yet with an error, and
            # sometimes with silence.
            asyncio.ensure_future(self._watch_for_offer(sip_call_id, media, human_sessionid,
                                                        state.generation))

    async def _watch_for_offer(self, sip_call_id: str, media, human_sessionid: str,
                               generation: int):
        await asyncio.sleep(SUBSCRIBE_RETRY_INTERVAL)
        with self._call_sessions_lock:
            entry = self._call_sessions.get(sip_call_id)
            state = entry.subscription if entry and entry.media is media else None
        if (state is None or state.working or state.generation != generation
                or media.human_sessionid != human_sessionid):
            return  # something else happened in the meantime; not our turn
        await self._pursue(sip_call_id, media, human_sessionid, state.no_publisher())

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
        await self._leave_room()

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
            ask_for_a_room = bool(entry and entry.awaits_meeting_id and not entry.roomid)
        roomid = self._entry_roomid(call_id)

        async def _connected():
            nonlocal roomid
            if ask_for_a_room:
                roomid = await self._ask_which_room(call_id, rtp)
                if not roomid:
                    return  # the caller has already been hung up on
            await self._publish_call_audio(call_id, rtp, roomid, number, human_sessionid_hint=human_sessionid_hint)
            if direction == "outbound":
                await self._send_dialout_status(call_id, roomid, "connected")

        _run_coro_logged(_connected(), self.loop, f"on_call_connected({call_id})")

    def on_call_ended(self, *, call_id, reason):
        with self._call_sessions_lock:
            entry = self._call_sessions.get(call_id)
            ivr = entry.ivr if entry else None
        if ivr is not None:
            # A caller who hangs up mid-prompt: without this the dialogue
            # keeps playing tones into a closed session until it times out.
            ivr.stop()
        roomid = self._entry_roomid(call_id)
        _run_coro_logged(self._teardown_call(call_id, roomid), self.loop, f"on_call_ended({call_id})")

    def on_call_failed(self, *, call_id, reason):
        """An outbound call that never got as far as being answered -
        refused, unanswered, or ended while it still rang.

        Telling Talk is only half of it: whatever the ringing put up has
        to come down again, the name plate above all. Nothing else does
        it for such a call, because on_call_ended belongs to calls that
        were established. Measured when it was missing: a refused call
        left its phone participant in the room for good, and the next
        call added a second one beside it."""
        roomid = self._entry_roomid(call_id)

        async def _failed():
            await self._send_dialout_status(call_id, roomid, "rejected")
            await self._teardown_call(call_id, roomid)

        _run_coro_logged(_failed(), self.loop, f"on_call_failed({call_id})")

    def on_incoming_call(self, *, call_id, caller, dialled=""):
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
        conference = is_conference_call(self.call_manager.line, dialled, caller)
        with self._call_sessions_lock:
            self._call_sessions[call_id] = Call(sip_call_id=call_id, kind=INBOUND, number=caller,
                                                number_dialled=dialled,
                                                awaits_meeting_id=conference)
        if conference:
            # Nothing to ring and nobody to ask: the number belongs to the
            # bridge and to no one person, so the call is answered and the
            # caller is asked which conversation they want once the audio
            # is up (see _ask_which_room). This is decided here rather
            # than twice, because answering below reaches the connected
            # callback before this method returns.
            print(f"[talk] Answering {call_id} on conference number {dialled} "
                  f"- the caller will be asked for a meeting id")
            self.call_manager.answer()
            return
        if config.auto_answer_calls:
            print(f"[talk] Incoming call {call_id} from {caller} - answering with real audio")
            self.call_manager.answer()
            return
        _run_coro_logged(self._handle_incoming_ring(call_id, caller, dialled), self.loop,
                         f"on_incoming_call({call_id})")

    async def _room_for_inbound_call(self, line, caller: str, dialled: str):
        """Which room an incoming call belongs in, and who the caller is
        in it.

        Two answers, and they differ in more than the token. The line's
        configured room is a standing conversation the caller is a guest
        of nobody in - Nextcloud never learns they exist. Direct dial-in
        asks Nextcloud instead: it creates a conversation for this one
        call and makes the caller a participant of it, which is the only
        way anything but the signaling server knows who is calling.

        Which of the two a call gets is decided by the number it was
        placed to, not by the line it arrived on: only the numbers named
        in the line's dialin_numbers are the bridge's own. Everything else
        rings the configured room, including every number the mapping does
        not mention - a line that shares its number with a person's phone
        simply maps nothing.

        Falls back to the configured room whenever dial-in is not set up
        or has nothing to say - a caller nobody can place is still a
        caller, and leaving them ringing would be worse."""
        number = line.dialin_numbers.get(dialled, "")
        if not (config.sip_shared_secret and number):
            return line.default_room_token, None
        room = await asyncio.to_thread(
            # The endpoint's "caller" is a phone number: Nextcloud names
            # the conversation after it and gives the guest that name. A
            # handset's display name would put "FritzFon schwarz" there.
            talk_sip_bridge.direct_dial_in, number, caller_number(caller))
        if not room:
            return line.default_room_token, None
        actor = {"actorType": room.get("actorType"), "actorId": room.get("actorId")}
        print(f"[talk] Nextcloud made a conversation for this call: {room['token']} "
              f"with the caller as {actor['actorType']}/{str(actor['actorId'])[:12]}")
        if not (actor["actorType"] and actor["actorId"]):
            return room["token"], None
        return room["token"], actor

    async def _ask_which_room(self, call_id: str, rtp) -> str:
        """Runs the dialogue that decides where this call goes.

        Everything about it happens on the call's own audio, before Talk
        has heard of it - there is no room to publish into until the
        caller has named one. A caller who names none is hung up on, which
        is the only honest end: the bridge has nowhere to put them."""
        ivr = DialInIvr(rtp, prompt_wav=config.ivr_prompt_wav,
                        pin_prompt_wav=config.ivr_pin_prompt_wav)
        with self._call_sessions_lock:
            entry = self._call_sessions.get(call_id)
            if entry is None:
                return ""
            entry.ivr = ivr
        try:
            room = await asyncio.to_thread(ivr.run)
        finally:
            with self._call_sessions_lock:
                entry = self._call_sessions.get(call_id)
                if entry is not None:
                    entry.ivr = None
        if not room:
            print(f"[talk] {call_id} named no conversation - ending the call")
            await asyncio.to_thread(self.call_manager.hangup)
            return ""
        actor = {"actorType": room.get("actorType"), "actorId": room.get("actorId")}
        with self._call_sessions_lock:
            entry = self._call_sessions.get(call_id)
            if entry is None:
                return ""
            entry.roomid = room["token"]
            if actor["actorType"] and actor["actorId"]:
                entry.actor = actor
        print(f"[talk] {call_id} dialled into conversation {room['token']} "
              f"as {actor['actorType']}/{str(actor['actorId'])[:12]}")
        return room["token"]

    def on_dtmf(self, *, call_id, digit):
        """A key press. It belongs to the dialogue if one is running;
        otherwise nothing in this bridge acts on keys."""
        with self._call_sessions_lock:
            entry = self._call_sessions.get(call_id)
            ivr = entry.ivr if entry else None
        if ivr is not None:
            ivr.press(digit)

    async def _handle_incoming_ring(self, call_id: str, caller: str, dialled: str = ""):
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
        roomid, dialin = await self._room_for_inbound_call(line, caller, dialled)
        if dialin:
            with self._call_sessions_lock:
                entry = self._call_sessions.get(call_id)
                if entry is not None:
                    entry.roomid = roomid
                    entry.actor = dialin
        if dialin:
            # Nextcloud made this conversation for this one call and owns
            # it; the bridge's notify account is not in it and cannot ring
            # anybody there. Ringing is Nextcloud's own business here - it
            # knows whose number was dialled - and the caller is already a
            # participant waiting in the room. What is left for the bridge
            # is to answer, and it does so without asking
            # config.auto_answer_calls: that switch guards numbers that
            # might belong to a person, and this number is in
            # dialin_numbers, which says it belongs to the bridge.
            print(f"[talk] Answering {call_id}, dialled {dialled}, "
                  f"into its own conversation {roomid}")
            self.call_manager.answer()
            return
        if not roomid or not line.notify_user or not line.notify_app_password:
            print(f"[talk] Incoming call {call_id} from {caller} on line {line.id} - "
                  f"no notify_user/notify_app_password/default room configured, leaving it ringing")
            return

        self._room_joined_event.clear()
        await self.ws.send(json.dumps(talk_messages.join_room(roomid)))
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
