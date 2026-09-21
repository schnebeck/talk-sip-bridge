# talk-sip-bridge - bridge/inbound_call.py
# A call arriving from the phone, up to the point where there is audio to
# publish.
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

"""A call arriving from the phone, up to the point where there is audio
to publish.

Three questions have to be answered before this bridge can do anything
with an inbound call, and none of them is about media: which
conversation the call belongs in, who the caller is in it, and whether
anybody is going to pick up. The signaling protocol answers none of
them - it has no ringing or accept/decline exchange at all, because
`addsession` represents a call that is already connected and dialout is
Talk's own business. So the answers come from Nextcloud's OCS API, from
the number that was dialled, and sometimes from the caller's own keypad.

This is the client's inbound half, split out because it is a different
job from carrying a call: it works on the client's own state and hands
back a room to publish into.
"""
import asyncio
import json
import re

import talk_messages
import talk_ocs
import talk_sip_bridge
from config import config
from dialin_ivr import DialInIvr
from sip_messages import caller_number


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


class InboundCalls:
    """The inbound half of one TalkClient, working on its state."""

    def __init__(self, client):
        self.client = client

    async def room_for(self, line, caller: str, dialled: str):
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

    async def ask_which_room(self, call_id: str, rtp) -> str:
        """Runs the dialogue that decides where this call goes.

        Everything about it happens on the call's own audio, before Talk
        has heard of it - there is no room to publish into until the
        caller has named one. A caller who names none is hung up on, which
        is the only honest end: the bridge has nowhere to put them."""
        ivr = DialInIvr(rtp, prompt_wav=config.ivr_prompt_wav,
                        pin_prompt_wav=config.ivr_pin_prompt_wav)
        with self.client._call_sessions_lock:
            entry = self.client._call_sessions.get(call_id)
            if entry is None:
                return ""
            entry.ivr = ivr
        try:
            room = await asyncio.to_thread(ivr.run)
        finally:
            with self.client._call_sessions_lock:
                entry = self.client._call_sessions.get(call_id)
                if entry is not None:
                    entry.ivr = None
        if not room:
            print(f"[talk] {call_id} named no conversation - ending the call")
            await asyncio.to_thread(self.client.call_manager.hangup)
            return ""
        actor = {"actorType": room.get("actorType"), "actorId": room.get("actorId")}
        with self.client._call_sessions_lock:
            entry = self.client._call_sessions.get(call_id)
            if entry is None:
                return ""
            entry.roomid = room["token"]
            if actor["actorType"] and actor["actorId"]:
                entry.actor = actor
        print(f"[talk] {call_id} dialled into conversation {room['token']} "
              f"as {actor['actorType']}/{str(actor['actorId'])[:12]}")
        return room["token"]

    async def ring(self, call_id: str, caller: str, dialled: str = ""):
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
        line = self.client.call_manager.line
        roomid, dialin = await self.room_for(line, caller, dialled)
        if dialin:
            with self.client._call_sessions_lock:
                entry = self.client._call_sessions.get(call_id)
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
            self.client.call_manager.answer()
            return
        if not roomid or not line.notify_user or not line.notify_app_password:
            print(f"[talk] Incoming call {call_id} from {caller} on line {line.id} - "
                  f"no notify_user/notify_app_password/default room configured, leaving it ringing")
            return

        self.client._room_joined_event.clear()
        await self.client.ws.send(json.dumps(talk_messages.join_room(roomid)))
        try:
            # Confirmation normally arrives in milliseconds. Waiting longer
            # than this would eat into the caller's patience for no gain -
            # the room state that answering is compared against also
            # arrives while the OCS calls below are still running.
            await asyncio.wait_for(self.client._room_joined_event.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            print(f"[talk] No room-join confirmation for {roomid} yet, ringing anyway")

        with self.client._call_sessions_lock:
            entry = self.client._call_sessions.get(call_id)
            if entry is None:
                return  # the caller gave up while the room was being joined
            entry.waiting_for_accept = True

        opener, ring_sessionid = await asyncio.to_thread(
            talk_ocs.start_ring, roomid, line.notify_user, line.notify_app_password)
        if opener is None:
            with self.client._call_sessions_lock:
                entry = self.client._call_sessions.get(call_id)
                if entry is not None:
                    entry.waiting_for_accept = False
            return

        with self.client._call_sessions_lock:
            entry = self.client._call_sessions.get(call_id)
            if entry is not None:
                entry.talk_ring_opener = opener
                entry.talk_ring_sessionid = ring_sessionid
        print(f"[talk] Ringing {line.notify_user} for call {call_id} from {caller}, watching room {roomid} for accept")

    async def stop_ring(self, roomid: str, opener, line):
        await asyncio.to_thread(talk_ocs.stop_ring, opener, roomid, line.notify_user, line.notify_app_password)
