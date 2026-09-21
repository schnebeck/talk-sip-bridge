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
    from room_state import FLAG_IN_CALL, FLAG_WITH_AUDIO, RoomCallState
    from subscription import Action
except ImportError:  # no media stack; every test here is skipped
    talk_client = None

HUMAN = "8Td-GS0EtQpgYvlozxrA8UPtN6nLyoVqimLy0fsP4fKEIyRKc22F"
CALL = "dialin-1"


def in_call(**flags):
    """A room whose sessions are in the call with the given flags."""
    state = RoomCallState()
    state.apply({"users": [{"sessionId": s, "inCall": f} for s, f in flags.items()]})
    return state


def client_with_call(*, publishing=True, subscribed=False, roster=None, room=None):
    client = talk_client.TalkClient.__new__(talk_client.TalkClient)
    client._call_sessions_lock = threading.Lock()
    entry = Call(sip_call_id=CALL, kind=INBOUND, number="**620", roomid="room-token")
    entry.media = mock.Mock(is_publishing=publishing, subscriber_alive=subscribed)
    type(entry).is_publishing = property(lambda self: publishing)
    client._call_sessions = {CALL: entry}
    client._room_roster = roster if roster is not None else {HUMAN: {"is_human": True}}
    client._room_call = room if room is not None else in_call(**{HUMAN: FLAG_IN_CALL | FLAG_WITH_AUDIO})
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

    def test_a_living_subscription_is_never_replaced(self):
        """Replacing one that works tears down a connection carrying
        audio."""
        self.assertEqual(catch_up(client_with_call(subscribed=True), {HUMAN}), [])

    def test_a_dead_subscription_is_rebuilt(self):
        """A client that changes its microphone tears its publisher
        down and comes back a second later; the subscription to it does
        not survive that, and nothing else would ever rebuild it."""
        self.assertEqual(catch_up(client_with_call(subscribed=False), {HUMAN}),
                         [(CALL, HUMAN)])

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


@needs_media_stack
class WhoIsWorthListeningToTest(unittest.TestCase):
    """Several people can arrive in one event. A participant whose
    permissions do not let them speak joins without the audio flag, and
    subscribing to them spends the attempts on a stream that will never
    exist."""

    OTHER = "other-person-session"

    def client(self, **flags):
        return client_with_call(
            roster={self.OTHER: {"is_human": True}, HUMAN: {"is_human": True}},
            room=in_call(**flags))

    def test_the_one_carrying_audio_is_preferred(self):
        client = self.client(**{self.OTHER: FLAG_IN_CALL,
                                HUMAN: FLAG_IN_CALL | FLAG_WITH_AUDIO})
        self.assertEqual(catch_up(client, {self.OTHER, HUMAN}), [(CALL, HUMAN)])

    def test_somebody_silent_is_still_better_than_nobody(self):
        """The flags are the server's word, and this bridge has been
        wrong about them before - so a participant without the flag is
        the fallback, not a reason to give up."""
        client = self.client(**{self.OTHER: FLAG_IN_CALL})
        self.assertEqual(catch_up(client, {self.OTHER}), [(CALL, self.OTHER)])

    def test_the_choice_does_not_depend_on_set_ordering(self):
        """Two arriving together must resolve the same way twice, or a
        retry picks a different peer than the attempt before it."""
        flags = {self.OTHER: FLAG_IN_CALL, HUMAN: FLAG_IN_CALL}
        picks = {catch_up(self.client(**flags), {self.OTHER, HUMAN})[0][1]
                 for _ in range(10)}
        self.assertEqual(len(picks), 1, f"picked {picks}")

    def test_a_room_that_knows_nothing_yet_still_yields_somebody(self):
        client = client_with_call(room=RoomCallState())
        self.assertEqual(catch_up(client, {HUMAN}), [(CALL, HUMAN)])


@needs_media_stack
class SecondSubscriptionTest(unittest.TestCase):
    """A rebuilt subscription is a new negotiation.

    The state machine is what decides whether anything is sent at all,
    and the one from the last subscription has already reached FLOWING.
    Handing it back means the rebuild asks the machine what to do, is
    told "nothing", and goes quiet - measured: a client came back from
    changing its microphone, the bridge said it was asking for their
    audio, and not one message went out."""

    def call(self):
        client = client_with_call()
        entry = client._call_sessions[CALL]
        return client, entry

    def test_the_first_subscription_starts_from_nothing(self):
        client, entry = self.call()
        state = human_audio.HumanAudio(client).restart_state_for(CALL)
        self.assertEqual(state.start().action, Action.REQUEST)

    def test_a_rebuild_does_not_inherit_a_flowing_machine(self):
        client, entry = self.call()
        audio = human_audio.HumanAudio(client)
        first = audio.restart_state_for(CALL)
        first.start()
        first.offer("sid-1")
        first.media_arrived()
        self.assertTrue(first.working, "the first subscription never got going")

        second = audio.restart_state_for(CALL)
        self.assertIsNot(second, first)
        self.assertEqual(second.start().action, Action.REQUEST,
                         "the rebuilt subscription asked for nothing")

    def test_the_call_keeps_the_new_machine(self):
        """Everything else - the retries, the refusals - looks the
        subscription up on the call, and has to find the current one."""
        client, entry = self.call()
        audio = human_audio.HumanAudio(client)
        audio.restart_state_for(CALL)
        second = audio.restart_state_for(CALL)
        self.assertIs(audio.state_for(CALL), second)
        self.assertIs(entry.subscription, second)

    def test_a_call_that_has_ended_gets_no_machine(self):
        client, _ = self.call()
        client._call_sessions.clear()
        self.assertIsNone(human_audio.HumanAudio(client).restart_state_for(CALL))


if __name__ == "__main__":
    unittest.main()
