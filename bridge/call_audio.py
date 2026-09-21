"""The phone's audio in the room, and the media messages around it.

Publishing is one offer addressed to this bridge's own session, which
the server routes to the room's Janus and answers. Everything else here
is what has to be true around that: being in the room first, because
`addsession` routes nothing on its own; announcing audio only once the
publisher exists, or every client in the room asks for a stream that is
not there yet and backs off to one retry every ten seconds; and routing
each WebRTC message to the connection it belongs to, publisher or
subscriber.
"""
import asyncio
import json

import talk_messages
from call_media import CallMedia
from config import config
from sip_messages import caller_display_name, caller_number
from talk_messages import FLAG_IN_CALL, FLAG_WITH_AUDIO


class CallAudio:
    """One TalkClient's publishing half, working on its state."""

    def __init__(self, client):
        self.client = client

    async def handle_message(self, message: dict):
        """Routes one WebRTC message to the call it belongs to. Which
        connection that is - the publisher or the subscriber - follows from
        who sent it, and only the call's own media knows that."""
        data = message.get("data", {})
        sender_sessionid = message.get("sender", {}).get("sessionid")
        with self.client._call_sessions_lock:
            for entry in self.client._call_sessions.values():
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
            await self.client.human_audio.answer_offer(media, sender_sessionid, data)
        elif msg_type == "candidate":
            await media.add_candidate(pc, data.get("payload"))

    async def set_incall(self, flags: int):
        """Announces this session's call state to the room. Only meaningful
        because "internal-incall" is declared in _hello - otherwise the
        server owns these flags. Talk clients start asking for this
        session's audio as soon as FLAG_WITH_AUDIO shows up here, so it is
        set once the publisher exists and cleared when the call ends."""
        if self.client.ws is None:
            return
        try:
            await self.client.ws.send(json.dumps(talk_messages.set_incall(flags)))
        except Exception as e:
            print(f"[talk] Could not update inCall flags to {flags}: {e!r}")

    async def send_offer(self, sip_call_id: str, sdp: str, display_name: str) -> None:
        await self.client.ws.send(json.dumps(talk_messages.publish_offer(
            self.client.own_sessionid, sip_call_id, sdp, display_name)))

    async def publish(self, sip_call_id: str, rtp_session, roomid: str, number: str, human_sessionid_hint: str = None):
        await self.client._join_room_for_publishing(roomid)
        if not self.client._call_still_running(sip_call_id):
            print(f"[talk] {sip_call_id} ended before publishing started - nothing to publish")
            return

        display_name = caller_display_name(number)
        with self.client._call_sessions_lock:
            waiting = self.client._call_sessions.get(sip_call_id)
            actor = waiting.actor if waiting else None
        virtual_sessionid = await self.client.phone.announce(
            sip_call_id, roomid=roomid, number=caller_number(number),
            displayname=display_name, caller=True, actor=actor)

        media = CallMedia(sip_call_id, rtp_session, on_connection_lost=self.client._hangup_sip)
        media.open_publisher(
            self.client.own_sessionid,
            on_talking=lambda talking: self.client.run_later(
                self.client.phone.publish_talking(sip_call_id, roomid, talking), f"talking({sip_call_id})"))
        with self.client._call_sessions_lock:
            # get, not setdefault: _teardown_call removing the entry is what
            # says the call is over, and recreating it here would hide that.
            entry = self.client._call_sessions.get(sip_call_id)
            if entry is not None:
                entry.virtual_sessionid = virtual_sessionid
                entry.media = media

        sdp = await media.publisher_offer()
        if not self.client._call_still_running(sip_call_id):
            # Gathering ICE takes seconds, and a caller who gives up in the
            # meantime tears the call down underneath us. Publishing anyway
            # would leave a live publisher running for a call that is gone.
            print(f"[talk] {sip_call_id} ended while gathering ICE - discarding the publisher")
            await media.close()
            await self.client.phone.withdraw(virtual_sessionid, roomid)
            return

        await self.send_offer(sip_call_id, sdp, display_name)
        print(f"[talk] Publishing call audio for {sip_call_id}"
              + (f" as virtual session {virtual_sessionid}" if virtual_sessionid else ""))

        # Only now: announcing audio any earlier makes Talk clients ask for a
        # stream that does not exist yet, and they back off to one retry
        # every 10 seconds after that. It also satisfies the signaling
        # server's rule that both sides must be in the call before either
        # may subscribe to the other, which the request below depends on.
        await self.set_incall(FLAG_IN_CALL | FLAG_WITH_AUDIO)

        # Prefer the session accept detection just saw entering the call over
        # scanning the room roster: the roster only ever gains entries, and
        # the server does not reliably announce a chat-relay session that
        # drops silently (a backgrounded mobile app) - confirmed live, that
        # made the roster hand out an hours-old dead session whose audio
        # could only ever fail with "client_not_found".
        human_sessionid = human_sessionid_hint or await self.client.human_audio.find_human()
        await self.client.phone.announce_state(sip_call_id)

        if human_sessionid:
            asyncio.ensure_future(self.client.human_audio.start(sip_call_id, media, human_sessionid))
        else:
            print(f"[talk] No other participant found in room {roomid} - phone side will not hear Talk's audio")
