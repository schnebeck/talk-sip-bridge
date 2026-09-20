"""The per-call state the Talk side accumulates."""
import unittest

from call import DIALOUT, INBOUND, Call


class DefaultsTest(unittest.TestCase):
    def test_a_new_call_carries_nothing_but_its_identity(self):
        call = Call(sip_call_id="c1", kind=INBOUND)
        self.assertFalse(call.is_publishing)
        self.assertFalse(call.waiting_for_accept)
        self.assertEqual(call.own_session_ids(), set())
        for field in ("talk_ring_opener", "talk_ring_sessionid", "accepted_sessionid",
                      "virtual_sessionid", "virtual_room_sessionid", "media"):
            with self.subTest(field=field):
                self.assertIsNone(getattr(call, field))

    def test_a_dialout_knows_its_room_from_the_start(self):
        """An inbound call has no room in the protocol and falls back to the
        line's default; a dialout is told which room it belongs to."""
        call = Call(sip_call_id="c1", kind=DIALOUT, roomid="room-token", number="621")
        self.assertEqual(call.roomid, "room-token")
        self.assertEqual(Call(sip_call_id="c2", kind=INBOUND).roomid, "")


class FakeMedia:
    def __init__(self, publishing=False):
        self.is_publishing = publishing


class PublishingTest(unittest.TestCase):
    """What separates a call carrying audio from one still ringing. The
    answer lives in its media, so a call without media is not publishing
    whatever else it has."""

    def test_a_call_without_media_is_not_publishing(self):
        self.assertFalse(Call(sip_call_id="c1", kind=INBOUND).is_publishing)

    def test_media_that_has_not_started_publishing_yet(self):
        call = Call(sip_call_id="c1", kind=INBOUND)
        call.media = FakeMedia(publishing=False)
        self.assertFalse(call.is_publishing)

    def test_media_that_is_publishing(self):
        call = Call(sip_call_id="c1", kind=INBOUND)
        call.media = FakeMedia(publishing=True)
        self.assertTrue(call.is_publishing)


class OwnSessionsTest(unittest.TestCase):
    """Mistaking one of the bridge's own sessions for a person answering is
    what makes a call pick itself up, so which ids count as ours is part of
    the call rather than of whoever asks."""

    def test_all_three_kinds_are_reported(self):
        call = Call(sip_call_id="c1", kind=INBOUND)
        call.virtual_sessionid = "phone-plate"
        call.virtual_room_sessionid = "server-assigned"
        call.talk_ring_sessionid = "ring-trigger"
        self.assertEqual(call.own_session_ids(), {"phone-plate", "server-assigned", "ring-trigger"})

    def test_unset_ones_are_left_out_rather_than_reported_as_none(self):
        """A None in this set would match a session id that is also missing,
        and quietly exclude a real participant."""
        call = Call(sip_call_id="c1", kind=INBOUND)
        call.virtual_sessionid = "phone-plate"
        self.assertEqual(call.own_session_ids(), {"phone-plate"})
        self.assertNotIn(None, call.own_session_ids())

    def test_the_session_that_answered_is_not_one_of_ours(self):
        call = Call(sip_call_id="c1", kind=INBOUND)
        call.virtual_sessionid = "phone-plate"
        call.accepted_sessionid = "human"
        self.assertNotIn("human", call.own_session_ids())


if __name__ == "__main__":
    unittest.main()
