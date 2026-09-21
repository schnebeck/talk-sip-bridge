"""Somebody joining after the phone is already in the room.

The bridge looks for a participant to listen to once, when the call
connects. For a dialout that is always enough - the person placing the
call is already there. A caller who dials in arrives in an empty room,
and that one look finds nobody: without this, the phone is heard in
Talk and hears nothing back, for the whole call, with the journal
saying so once and then nothing.

Measured before it was fixed: the caller dialled in at 11:50:41, the
person joined at 11:50:54, the bridge announced the phone to them, and
never asked for their audio.
"""
import asyncio
import threading
import unittest
from unittest import mock

from tests.support import needs_media_stack

try:
    import human_audio
    import talk_client
    from call import INBOUND, Call
except ImportError:  # no media stack; every test here is skipped
    talk_client = None

HUMAN = "8Td-GS0EtQpgYvlozxrA8UPtN6nLyoVqimLy0fsP4fKEIyRKc22F"
CALL = "dialin-1"


def client_with_call(*, publishing=True, listening_to=None, roster=None):
    client = talk_client.TalkClient.__new__(talk_client.TalkClient)
    client._call_sessions_lock = threading.Lock()
    entry = Call(sip_call_id=CALL, kind=INBOUND, number="**620", roomid="room-token")
    entry.media = mock.Mock(is_publishing=publishing, human_sessionid=listening_to)
    type(entry).is_publishing = property(lambda self: publishing)
    client._call_sessions = {CALL: entry}
    client._room_roster = roster if roster is not None else {HUMAN: {"is_human": True}}
    return client


def catch_up(client, entered):
    """What the bridge does when those sessions enter the call."""
    started = []

    async def start(sip_call_id, media, human_sessionid):
        started.append((sip_call_id, human_sessionid))

    audio = human_audio.HumanAudio(client)
    audio.start = start
    asyncio.new_event_loop().run_until_complete(
        audio.start_for_late_joiner(CALL, entered))
    return started


@needs_media_stack
class LateJoinerTest(unittest.TestCase):
    def test_the_person_who_joins_is_asked_for_their_audio(self):
        self.assertEqual(catch_up(client_with_call(), {HUMAN}), [(CALL, HUMAN)])

    def test_nobody_is_asked_twice(self):
        """A subscription already running must not be replaced - that is
        how a working connection gets torn down and rebuilt."""
        client = client_with_call(listening_to="somebody-else")
        self.assertEqual(catch_up(client, {HUMAN}), [])

    def test_a_call_that_is_not_publishing_is_left_alone(self):
        """There is nothing to carry the audio into yet."""
        self.assertEqual(catch_up(client_with_call(publishing=False), {HUMAN}), [])

    def test_a_phone_joining_is_not_somebody_to_listen_to(self):
        """The room's other arrivals include this bridge's own name
        plates, which carry no media at all."""
        client = client_with_call(roster={"phone-1": {"is_human": False}})
        self.assertEqual(catch_up(client, {"phone-1"}), [])

    def test_a_session_the_roster_does_not_know_is_not_used(self):
        """Asking a session the roster never announced fails with
        client_not_found and spends one of the attempts."""
        self.assertEqual(catch_up(client_with_call(roster={}), {"who-is-this"}), [])

    def test_a_call_that_has_ended_is_left_alone(self):
        client = client_with_call()
        client._call_sessions.clear()
        self.assertEqual(catch_up(client, {HUMAN}), [])

    def test_the_person_is_picked_out_of_a_mixed_arrival(self):
        client = client_with_call(roster={"phone-1": {"is_human": False},
                                          HUMAN: {"is_human": True}})
        self.assertEqual(catch_up(client, {"phone-1", HUMAN}), [(CALL, HUMAN)])


if __name__ == "__main__":
    unittest.main()
