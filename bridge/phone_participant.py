# talk-sip-bridge - bridge/phone_participant.py
# The phone as a participant of the room.
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

"""The phone as a participant of the room.

A call has two faces in Talk. The audio rides on this bridge's own
session, because a virtual session can never carry media - in MCU mode
the server looks a publisher up by the raw session id and knows nothing
of a virtual one. What the room *shows*, and what its clients mute,
name and hang up, is the virtual session put up here.

Keeping the two apart is the whole reason this file exists: everything
in it is a name plate and its state, and none of it carries a sound.
"""
import json
import secrets

import talk_messages
from config import config
from sip_messages import caller_display_name, caller_number
from talk_messages import FLAG_IN_CALL


class PhoneParticipant:
    """One TalkClient's phone participants, working on its state."""

    def __init__(self, client):
        self.client = client

    async def announce(self, sip_call_id: str, *, roomid: str, number: str,
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
        await self.client.ws.send(json.dumps(talk_messages.add_session(
            virtual_sessionid, roomid, call_id=sip_call_id, number=number,
            displayname=displayname or number, with_audio=with_audio, actor=actor,
            incall=FLAG_IN_CALL)))
        await self.client.ws.send(json.dumps(
            talk_messages.update_session(virtual_sessionid, roomid, flags)))
        print(f"[talk] Phone participant {virtual_sessionid} announced "
              f"{'with' if with_audio else 'without'} audio"
              + (f", as {actor['actorType']}/{str(actor['actorId'])[:12]}" if actor
                 else ", known to the signaling server only"))
        return virtual_sessionid

    async def withdraw(self, virtual_sessionid: str, roomid: str):
        if not virtual_sessionid:
            return
        await self.client.ws.send(json.dumps(talk_messages.remove_session(virtual_sessionid, roomid)))

    async def announce_state(self, sip_call_id: str, peers=None):
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
        with self.client._call_sessions_lock:
            entry = self.client._call_sessions.get(sip_call_id)
            if entry is None or not entry.is_publishing:
                return
            # The caller id, not the gateway's word for the device: a
            # handset announces itself as "FritzFon schwarz", which tells
            # a room nothing, while the number identifies who is calling.
            name = caller_number(entry.number) or caller_display_name(entry.number)
            ours = {self.client.own_sessionid} | entry.own_session_ids()
            targets = set(peers) if peers is not None else self.client._room_call.others_in_call(ours)
        targets -= ours
        if not targets:
            return
        for peer in sorted(targets):
            for state, payload in (("unmute", {"name": "audio"}),
                                   ("mute", {"name": "video"}),
                                   ("nickChanged", {"name": name})):
                try:
                    await self.client.ws.send(json.dumps(talk_messages.peer_state(peer, state, payload)))
                except Exception as e:
                    print(f"[talk] Could not tell {peer} about the phone: {e!r}")
                    return
        print(f"[talk] Told {len(targets)} participant(s) that {name}'s microphone is on")

    async def publish_talking(self, sip_call_id: str, roomid: str, talking: bool):
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
        with self.client._call_sessions_lock:
            entry = self.client._call_sessions.get(sip_call_id)
            virtual = entry.virtual_sessionid if entry else None
        if not virtual or self.client.ws is None:
            return
        flags = talk_messages.FLAG_TALKING if talking else 0
        try:
            await self.client.ws.send(json.dumps(
                talk_messages.update_session(virtual, roomid, flags=flags)))
        except Exception as e:
            print(f"[talk] Could not publish the talking state for {sip_call_id}: {e!r}")
