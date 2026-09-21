# talk-sip-bridge - bridge/talk_client.py
# The bridge's two connections to Talk's signaling server, and what arrives
# on them.
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

"""The bridge's two connections to Talk's signaling server, and what arrives
on them.

Both are "internal clients"; the one carrying the "start-dialout" feature
flag is what lets Talk's own native call UI (not a custom chat-bot command,
see docs/CONCEPT.md) trigger outbound calls and receive real, named "phone"
participants for inbound ones.

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
import threading

import talk_messages
import talk_ocs
from call import DIALOUT, INBOUND, Call
from config import config
from call_audio import CallAudio
from dialout import Dialout
from human_audio import HumanAudio
from inbound_call import InboundCalls, is_conference_call
from phone_participant import PhoneParticipant
from room_presence import RoomPresence
import background
import signaling
from room_state import RoomCallState




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
        self.inbound = InboundCalls(self)
        self.human_audio = HumanAudio(self)
        self.audio = CallAudio(self)
        self.dialout = Dialout(self)
        self.presence = RoomPresence(self)
        self.phone = PhoneParticipant(self)

    # -- lifecycle, run from a background thread -----------------------
    def run_later(self, coro, label: str):
        """Runs a coroutine on this client's loop, from wherever.

        Every CallManager callback arrives on a plain worker thread and
        has work for the loop; this is the one way across. It also keeps
        the loop out of the callers' hands, which is worth something:
        handing the wrong one over is silent, and the work simply never
        runs."""
        return background.run_logged(coro, self.loop, label)

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
        await asyncio.gather(signaling.supervise("room", self._serve_room),
                             signaling.supervise("dialout", self._serve_dialout))




    async def _serve_room(self):
        def settled(ws, sessionid):
            self.ws = ws
            self.own_sessionid = sessionid
            # A new connection is in no room and knows nothing about one.
            self._room_call = RoomCallState()
            self._room_roster = {}

        await signaling.serve("room", talk_messages.ROOM_FEATURES, settled,
                              self._handle_room_message)

    async def _serve_dialout(self):
        def settled(ws, _sessionid):
            self.dialout_ws = ws

        await signaling.serve("dialout", talk_messages.DIALOUT_FEATURES, settled,
                              self.dialout.handle_message)



    # -- incoming messages from the signaling server ---------------------
    async def _handle_room_message(self, msg: dict):
        msg_type = msg.get("type")
        if msg_type == "message":
            await self.audio.handle_message(msg["message"])
        elif msg_type == "room" and msg.get("id") == "bridge-room":
            self._room_joined_event.set()
        elif msg_type == "error" and msg.get("id") == "bridge-room" and msg.get("error", {}).get("code") == "already_joined":
            # Joining twice - a dialout joins when it starts ringing
            # and the publisher joins again when it is answered - is
            # answered with this instead of a confirmation. It means
            # we are in the room already, which is just as good.
            self._room_joined_event.set()
        elif msg_type == "error" and str(msg.get("id", "")).startswith("bridge-subanswer-"):
            await self.human_audio.answer_refused(
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
                self.presence.joined(event.get("join") or [])
            elif event.get("target") == "room" and event.get("type") == "leave":
                with self._call_sessions_lock:
                    for sessionid in event.get("leave") or []:
                        self._room_roster.pop(sessionid, None)
            elif event.get("target") == "participants" and event.get("type") == "update":
                await self.presence.updated(event.get("update") or {})
        elif config.signaling_debug:
            print(f"[talk] unhandled {msg_type}: {json.dumps(msg)[:1000]}")









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
            await self.inbound.stop_ring(roomid, entry.talk_ring_opener, self.call_manager.line)
        was_publishing = entry.is_publishing
        if entry.media:
            await entry.media.close()
        if entry.virtual_sessionid:
            await self.phone.withdraw(entry.virtual_sessionid, roomid)
        if was_publishing:
            # The publisher is gone, so stop advertising audio - otherwise
            # Talk clients keep asking this session for a stream that no
            # longer exists.
            await self.audio.set_incall(0)
        if entry.kind == DIALOUT:
            await self.dialout.report(sip_call_id, roomid, "cleared")
        print(f"[talk] Call {sip_call_id} ended, virtual session removed")
        if entry.talk_ring_opener is not None and not entry.waiting_for_accept:
            # This bridge started the room's call for this phone call and
            # somebody answered it, so it ends with the phone call too -
            # see talk_ocs.end_room_call for what being left in it does.
            # A call nobody answered is not ended here: it was already
            # given up by InboundCalls.stop_ring above.
            line = self.call_manager.line
            await asyncio.to_thread(talk_ocs.end_room_call, roomid,
                                    line.notify_user, line.notify_app_password)
        await self.presence.leave()

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
                roomid = await self.inbound.ask_which_room(call_id, rtp)
                if not roomid:
                    return  # the caller has already been hung up on
            await self.audio.publish(call_id, rtp, roomid, number, human_sessionid_hint=human_sessionid_hint)
            if direction == "outbound":
                await self.dialout.report(call_id, roomid, "connected")

        self.run_later(_connected(), f"on_call_connected({call_id})")

    def on_call_ended(self, *, call_id, reason):
        with self._call_sessions_lock:
            entry = self._call_sessions.get(call_id)
            ivr = entry.ivr if entry else None
        if ivr is not None:
            # A caller who hangs up mid-prompt: without this the dialogue
            # keeps playing tones into a closed session until it times out.
            ivr.stop()
        roomid = self._entry_roomid(call_id)
        self.run_later(self._teardown_call(call_id, roomid), f"on_call_ended({call_id})")

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
            await self.dialout.report(call_id, roomid, "rejected")
            await self._teardown_call(call_id, roomid)

        self.run_later(_failed(), f"on_call_failed({call_id})")

    def on_incoming_call(self, *, call_id, caller, dialled=""):
        # The signaling protocol has no ringing/accept-decline exchange for
        # inbound calls ("addsession" represents an already-connected call,
        # and dialout is Talk-initiated only), so there is no native way to
        # ask before picking up over the signaling protocol itself.
        # config.auto_answer_calls picks up immediately with no Talk-side
        # involvement; short of that, a line with notify_user configured
        # instead rings a real Nextcloud notification and waits for a human
        # to join the call in Talk (see InboundCalls.ring) - e.g. to let
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
            # is up (see InboundCalls.ask_which_room). This is decided here rather
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
        self.run_later(self.inbound.ring(call_id, caller, dialled), f"on_incoming_call({call_id})")



    def on_dtmf(self, *, call_id, digit):
        """A key press. It belongs to the dialogue if one is running;
        otherwise nothing in this bridge acts on keys."""
        with self._call_sessions_lock:
            entry = self._call_sessions.get(call_id)
            ivr = entry.ivr if entry else None
        if ivr is not None:
            ivr.press(digit)




def start_in_background(call_manager) -> TalkClient:
    client = TalkClient(call_manager)
    thread = threading.Thread(target=client.run_forever, daemon=True)
    thread.start()
    return client
