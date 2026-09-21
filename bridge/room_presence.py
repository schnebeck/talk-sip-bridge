"""Who is in the room, and what that means for the call.

Two events carry it, and neither says what it means on its own. A
`room`/`join` lists everyone present - people, phones and bridges alike
- and telling them apart is what decides whom the caller's audio comes
from. A `participants`/`update` says who is in the *call*, and the same
message means "somebody answered", "everybody has gone" or "the call
was ended for everyone" depending on what the room looked like before.

Which is why the answer is never read off the state: it is read off the
change (see room_state.RoomCallState). The server keeps listing
sessions whose clients are long gone, and reading state cannot tell
that from somebody picking up.
"""
import asyncio
import json
import traceback

import talk_messages

from call import DIALOUT
from room_state import RoomCallState, is_room_wide_call_end


class RoomPresence:
    """One TalkClient's view of its room, working on its state."""

    def __init__(self, client):
        self.client = client

    def joined(self, join_entries: list):
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
            is_internal = (sessionid == self.client.own_sessionid
                           or bool(talk_messages.INTERNAL_FEATURES
                                   .intersection(item.get("features") or [])))
            with self.client._call_sessions_lock:
                self.client._room_roster[sessionid] = {"is_human": not is_phone and not is_internal}
            if is_phone:
                call_id = user.get("callid")
                if call_id:
                    with self.client._call_sessions_lock:
                        entry = self.client._call_sessions.get(call_id)
                        if entry is not None:
                            entry.virtual_room_sessionid = sessionid

    async def updated(self, update: dict):
        """The room's two answers this bridge acts on: somebody joined the
        call it is ringing for, or the call it is in has ended.

        Besides per-session updates the server also broadcasts a room-wide
        "the call itself ended" (an "all" entry with a lowercase "incall"),
        which is what Talk's own "end call" button produces."""
        entered, left = self.client._room_call.apply(update)

        with self.client._call_sessions_lock:
            ours = {self.client.own_sessionid}
            active_call_id = None
            waiting_call_id = None
            waiting_entry = None
            ringing_dialout_id = None
            for call_id, entry in self.client._call_sessions.items():
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
            await self.client.phone.announce_state(active_call_id, peers=entered)

        if active_call_id is None and waiting_call_id is None and ringing_dialout_id is None:
            return

        if waiting_call_id is not None:
            accepted_sessionid = self.client._room_call.accepted_by(entered, ours)
            if accepted_sessionid is None:
                return
            print(f"[talk] Human joined the call - accepting {waiting_call_id}")
            with self.client._call_sessions_lock:
                entry = self.client._call_sessions.get(waiting_call_id)
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
                self.client.call_manager.answer()
            except Exception as e:
                print(f"[talk] Answering {waiting_call_id} failed: {e!r}")
                traceback.print_exc()
            opener = waiting_entry.talk_ring_opener
            if opener is not None:
                self.client.run_later(
                    self.client.inbound.stop_ring(self.client._entry_roomid(waiting_call_id), opener,
                                           self.client.call_manager.line),
                    f"stop ring for {waiting_call_id}")
            return

        if update.get("all"):
            if is_room_wide_call_end(update):
                self.client._hangup_sip("Room call ended - ending SIP side")
            return

        if ringing_dialout_id is not None and left and not self.client._room_call.anyone_in_call_besides(ours):
            # The last person left while the phone was still ringing.
            self.client._hangup_sip("Nobody is left in the call - withdrawing the outbound call")
            return

        if not left:
            return
        if not self.client._room_call.anyone_in_call_besides(ours):
            self.client._hangup_sip(f"Everyone but this bridge left the call ({len(left)} session(s)) - ending SIP side")

    async def join(self, roomid: str) -> None:
        """Puts the room connection in the room. Two things need it, and
        they happen at different moments: publishing the call's audio -
        being in the room is what makes the server route this session's
        self-addressed offer to the room's Janus, which addsession alone
        does not - and hearing what the room does, which has to start
        while the phone is still ringing.

        Only ever the room connection: this costs a connection its
        dialout eligibility for good (see docs/CONCEPT.md point 3)."""
        if self.client.ws is None:
            print(f"[talk] No room connection to join {roomid} with")
            return
        self.client._room_joined_event.clear()
        await self.client.ws.send(json.dumps(talk_messages.join_room(roomid)))
        try:
            await asyncio.wait_for(self.client._room_joined_event.wait(), timeout=5)
        except asyncio.TimeoutError:
            print(f"[talk] Warning: no room-join confirmation for {roomid} within 5s, carrying on")

    async def leave(self) -> None:
        """Leaves whatever room the room connection is in, once the call
        it was there for is over. Staying would leave the bridge counted
        among the room's sessions long after the call - and a room that
        still holds a session is a room Nextcloud thinks somebody is
        in."""
        if self.client.ws is None:
            return
        try:
            await self.client.ws.send(json.dumps(talk_messages.leave_room()))
        except Exception as e:
            print(f"[talk] Could not leave the room: {e!r}")
            return
        self.client._room_call = RoomCallState()
        self.client._room_roster = {}
