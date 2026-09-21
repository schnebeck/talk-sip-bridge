"""A room that looks empty for a moment.

The bridge ends the phone call when the last person leaves, which is
right: nobody should be left talking to an empty room. What is not
right is believing the first report of it.

A client that saves a new microphone leaves the call and comes back
within a second. So does one that reloads the page, or loses its
network for a breath. Measured before this waited: the media settings
were saved mid-conversation and the phone was hung up 900 ms later.

An explicit end - "end meeting for everyone", or the phone participant
being hung up - takes a different path and is acted on at once. Only
the inference "nobody seems to be here" waits.
"""
import asyncio
import threading
import unittest
from unittest import mock

from tests.support import needs_media_stack

try:
    import room_presence
    import talk_client
    from call import DIALOUT, Call
    from room_state import FLAG_IN_CALL, RoomCallState
except ImportError:  # no media stack; every test here is skipped
    talk_client = None

HUMAN = "the-person"


def room_with(*sessions):
    state = RoomCallState()
    state.apply({"users": [{"sessionId": s, "inCall": FLAG_IN_CALL} for s in sessions]})
    return state


def client(room, *, has_call=True):
    c = talk_client.TalkClient.__new__(talk_client.TalkClient)
    c._call_sessions_lock = threading.Lock()
    c.own_sessionid = "bridge-session"
    c._call_sessions = ({"call-1": Call(sip_call_id="call-1", kind=DIALOUT,
                                        number="620", roomid="room-token")}
                        if has_call else {})
    c._room_call = room
    c.call_manager = mock.Mock()
    return c


def confirm(c, comes_back=None, grace=0.02):
    """Runs the delayed check, optionally letting somebody rejoin while
    it waits. Returns the reasons the SIP side was ended with."""
    reasons = []
    c._hangup_sip = lambda reason=None: reasons.append(reason)

    async def scenario():
        presence = room_presence.RoomPresence(c)
        task = asyncio.ensure_future(presence._confirm_empty("everyone left"))
        if comes_back is not None:
            await asyncio.sleep(grace / 2)
            c._room_call = room_with(comes_back)
        await task

    loop = asyncio.new_event_loop()
    # Patched on the object room_presence is holding: the module bound it
    # at import, and reloading config gives a new one it never sees.
    with mock.patch.object(room_presence.config, "empty_room_grace", grace):
        try:
            loop.run_until_complete(scenario())
        finally:
            loop.close()
    return reasons


@needs_media_stack
class EmptyRoomTest(unittest.TestCase):
    def test_a_room_that_stays_empty_ends_the_call(self):
        self.assertEqual(confirm(client(room_with())), ["everyone left"])

    def test_a_room_that_fills_up_again_does_not(self):
        """The one this exists for: saving a microphone setting is a
        leave and a join, not the end of a conversation - measured at
        7.1 seconds between the two."""
        self.assertEqual(confirm(client(room_with()), comes_back=HUMAN), [])

    def test_the_bridges_own_sessions_do_not_count_as_company(self):
        """The phone's name plate and the bridge's own session are in
        the call too, and a room holding only those is empty."""
        c = client(room_with("bridge-session"))
        self.assertEqual(confirm(c), ["everyone left"])

    def test_a_call_that_ended_meanwhile_is_not_hung_up_again(self):
        """The caller may have hung up during the wait, and the entry
        is gone - hanging up then would reach whatever call came next."""
        self.assertEqual(confirm(client(room_with(), has_call=False)), [])

    def test_it_waits_rather_than_deciding_at_once(self):
        """Without the wait there is nothing to come back to. Run in a
        loop, because that is where the room's events are handled."""
        c = client(room_with())
        reasons = []
        c._hangup_sip = lambda reason=None: reasons.append(reason)

        async def scenario():
            room_presence.RoomPresence(c).hang_up_if_still_empty("everyone left")
            decided_at_once = list(reasons)
            await asyncio.sleep(room_presence.config.empty_room_grace + 0.05)
            return decided_at_once, list(reasons)

        loop = asyncio.new_event_loop()
        with mock.patch.object(room_presence.config, "empty_room_grace", 0.02):
            try:
                at_once, eventually = loop.run_until_complete(scenario())
            finally:
                loop.close()
        self.assertEqual(at_once, [], "the call was ended before anyone could return")
        self.assertEqual(eventually, ["everyone left"], "and then never ended at all")


if __name__ == "__main__":
    unittest.main()
