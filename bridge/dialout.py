"""Talk asking this bridge to call a phone.

One connection carries nothing but this: the request in, the answer
out, and the status updates that follow the call until it is over. It
is its own connection because a session that joins a room stops being a
dialout candidate for good, and this one therefore never joins one -
see docs/SIGNALING-API.md, "Two connections, one per role".

What the room side then does with the call is elsewhere; all that
belongs here is the conversation with Talk about it.
"""
import asyncio
import json

import talk_messages
from call import DIALOUT, Call
from talk_messages import dialout_actor


class Dialout:
    """One TalkClient's dialout half, working on its state."""

    def __init__(self, client):
        self.client = client

    async def handle_message(self, msg: dict):
        """The dialout connection carries one kind of traffic in each
        direction: requests from Talk, and this bridge's answers about
        the call they started."""
        if msg.get("type") == "internal" and msg.get("internal", {}).get("type") == "dialout":
            await self.requested(msg)
        else:
            # Nothing else is expected here; if the server starts sending
            # something new, this is where it surfaces.
            print(f"[talk] Unhandled message on the dialout connection: "
                  f"{json.dumps(msg)[:300]}")

    async def requested(self, msg: dict):
        """Talk's native "call a phone number" UI triggers this. The
        request carries the room id it's for - there is no separate
        mechanism to learn it, and no default to fall back to."""
        request_id = msg.get("id", "")
        dialout = msg["internal"]["dialout"]
        roomid = dialout.get("roomid", "")
        request = dialout.get("request", {})
        number = request.get("number", "")
        actor = dialout_actor(request.get("options") or {})
        line = self.client.call_manager.line
        if line.dialout_strip_prefix and number.startswith(line.dialout_strip_prefix):
            number = number[len(line.dialout_strip_prefix):]
        print(f"[talk] Dialout request for {number} in room {roomid}")
        result = self.client.call_manager.dial(number)
        if "error" in result:
            await self.reply(request_id, roomid, error=result["error"])
            return
        call_id = result["call_id"]
        with self.client._call_sessions_lock:
            self.client._call_sessions[call_id] = Call(sip_call_id=call_id, kind=DIALOUT,
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
        await self.reply(request_id, roomid, call_id=call_id, status="accepted")

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
        asyncio.ensure_future(self.client._join_room_for_publishing(roomid))

    async def reply(self, request_id: str, roomid: str, *, call_id: str = None, status: str = None, error: str = None):
        """Always on the dialout connection: the server matches a reply
        against the request it is pending on, and that bookkeeping is
        per session (hub.go's ProcessResponse)."""
        message = (talk_messages.dialout_error(roomid, error, request_id) if error is not None
                   else talk_messages.dialout_status(roomid, call_id, status, request_id))
        if self.client.dialout_ws is None:
            print(f"[talk] No dialout connection to answer {request_id or 'a status update'} on")
            return
        await self.client.dialout_ws.send(json.dumps(message))

    async def report(self, call_id: str, roomid: str, status: str):
        """Unsolicited status update (ringing/connected/rejected/cleared) -
        not correlated to a request id, unlike the initial "accepted" reply."""
        await self.reply("", roomid, call_id=call_id, status=status)
