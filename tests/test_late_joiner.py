# talk-sip-bridge - tests/test_late_joiner.py
# Somebody joining after the phone is already in the room.
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
    entry.media = mock.Mock(is_publishing=publishing, subscriber_alive=subscribed,
                            subscribers={HUMAN: object()} if subscribed else {})
    # Per participant, not "is anybody being carried": a Mock answers
    # every call with something truthy, which would read as "already
    # listening to them" for everybody and start nothing at all.
    entry.media.alive_for = lambda sessionid: subscribed
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


PERSON = "a-person-session"


@needs_media_stack
class SecondSubscriptionTest(unittest.TestCase):
    """A rebuilt subscription is a new negotiation.

    One machine per participant: sharing one across several would let a
    repair for somebody whose stream never came tear down the
    connection to somebody whose did.

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
        state = human_audio.HumanAudio(client).restart_state_for(CALL, PERSON)
        self.assertEqual(state.start().action, Action.REQUEST)

    def test_a_rebuild_does_not_inherit_a_flowing_machine(self):
        client, entry = self.call()
        audio = human_audio.HumanAudio(client)
        first = audio.restart_state_for(CALL, PERSON)
        first.start()
        first.offer("sid-1")
        first.media_arrived()
        self.assertTrue(first.working, "the first subscription never got going")

        second = audio.restart_state_for(CALL, PERSON)
        self.assertIsNot(second, first)
        self.assertEqual(second.start().action, Action.REQUEST,
                         "the rebuilt subscription asked for nothing")

    def test_the_call_keeps_the_new_machine(self):
        """Everything else - the retries, the refusals - looks the
        subscription up on the call, and has to find the current one."""
        client, entry = self.call()
        audio = human_audio.HumanAudio(client)
        audio.restart_state_for(CALL, PERSON)
        second = audio.restart_state_for(CALL, PERSON)
        self.assertIs(audio.state_for(CALL, PERSON), second)
        self.assertIs(entry.subscriptions[PERSON], second)

    def test_each_participant_gets_their_own_machine(self):
        """Sharing one would let a repair for somebody whose stream
        never came tear down the connection to somebody whose did."""
        client, entry = self.call()
        audio = human_audio.HumanAudio(client)
        first = audio.restart_state_for(CALL, PERSON)
        other = audio.restart_state_for(CALL, "somebody-else")
        self.assertIsNot(first, other)
        self.assertIs(audio.state_for(CALL, PERSON), first)
        self.assertEqual(set(entry.subscriptions), {PERSON, "somebody-else"})

    def test_a_call_that_has_ended_gets_no_machine(self):
        client, _ = self.call()
        client._call_sessions.clear()
        self.assertIsNone(human_audio.HumanAudio(client).restart_state_for(CALL, PERSON))


if __name__ == "__main__":
    unittest.main()


@needs_media_stack
class WhoToCarryTest(unittest.TestCase):
    """How many participants a call listens to, and which.

    One by default. A telephone call carries one stream, so hearing
    more than one person means summing them, and that path has never
    run against a telephone - see mixer.py and docs/CONFIG.md.
    """

    OTHER = "other-person-session"
    THIRD = "third-person-session"

    def audio(self, **flags):
        client = client_with_call(
            roster={s: {"is_human": True} for s in (HUMAN, self.OTHER, self.THIRD)},
            room=in_call(**flags))
        return human_audio.HumanAudio(client)

    def everyone(self):
        return self.audio(**{s: FLAG_IN_CALL | FLAG_WITH_AUDIO
                             for s in (HUMAN, self.OTHER, self.THIRD)})

    def test_one_participant_unless_mixing_is_switched_on(self):
        with mock.patch.object(human_audio.config, "mix_participants", False):
            self.assertEqual(len(self.everyone().targets()), 1)

    def test_everybody_once_it_is(self):
        with mock.patch.object(human_audio.config, "mix_participants", True), \
                mock.patch.object(human_audio.config, "max_mixed_sources", 6):
            self.assertEqual(set(self.everyone().targets()),
                             {HUMAN, self.OTHER, self.THIRD})

    def test_a_large_room_is_capped(self):
        """Each source is its own subscription and its own decoder."""
        with mock.patch.object(human_audio.config, "mix_participants", True), \
                mock.patch.object(human_audio.config, "max_mixed_sources", 2):
            self.assertEqual(len(self.everyone().targets()), 2)

    def test_the_cap_is_never_nobody(self):
        """A misconfigured zero would silence the call entirely."""
        with mock.patch.object(human_audio.config, "mix_participants", True), \
                mock.patch.object(human_audio.config, "max_mixed_sources", 0):
            self.assertEqual(len(self.everyone().targets()), 1)

    def test_those_carrying_audio_come_first(self):
        """With a cap, who is dropped matters: somebody whose
        permissions do not let them speak has nothing to contribute."""
        audio = self.audio(**{HUMAN: FLAG_IN_CALL,
                              self.OTHER: FLAG_IN_CALL | FLAG_WITH_AUDIO,
                              self.THIRD: FLAG_IN_CALL})
        with mock.patch.object(human_audio.config, "mix_participants", True), \
                mock.patch.object(human_audio.config, "max_mixed_sources", 1):
            self.assertEqual(audio.targets(), [self.OTHER])

    def test_the_order_is_the_same_twice(self):
        """A retry that picks a different peer than the attempt before
        it is not a retry."""
        with mock.patch.object(human_audio.config, "mix_participants", True):
            self.assertEqual([self.everyone().targets() for _ in range(5)].count(
                self.everyone().targets()), 5)

    def test_nobody_in_the_room_is_nobody_to_carry(self):
        client = client_with_call(roster={})
        self.assertEqual(human_audio.HumanAudio(client).targets(), [])


@needs_media_stack
class SomebodyLeavesTest(unittest.TestCase):
    """One participant hanging up, while the call goes on.

    Three things must happen and a fourth must not: their subscription
    goes, its state machine goes, the others are untouched - and the
    telephone call does not end, because somebody else is still there.
    """

    OTHER = "other-person-session"

    def call_carrying(self, *sessions):
        client = client_with_call()
        entry = client._call_sessions[CALL]
        media = mock.Mock()
        media.subscribers = dict.fromkeys(sessions, None)
        dropped = []
        async def drop(sessionid):
            dropped.append(sessionid)
            media.subscribers.pop(sessionid, None)
        media.drop_subscriber = drop
        entry.media = media
        for sessionid in sessions:
            entry.subscriptions[sessionid] = object()
        return client, entry, dropped

    def leave(self, client, gone):
        asyncio.new_event_loop().run_until_complete(
            human_audio.HumanAudio(client).stop_listening(CALL, gone))

    def test_the_one_who_left_is_dropped(self):
        client, entry, dropped = self.call_carrying(HUMAN, self.OTHER)
        self.leave(client, {HUMAN})
        self.assertEqual(dropped, [HUMAN])

    def test_the_others_are_untouched(self):
        """A call carrying three that dropped all of them because one
        left would be worse than never mixing at all."""
        client, entry, dropped = self.call_carrying(HUMAN, self.OTHER)
        self.leave(client, {HUMAN})
        self.assertEqual(set(entry.media.subscribers), {self.OTHER})
        self.assertEqual(set(entry.subscriptions), {self.OTHER})

    def test_their_state_machine_goes_with_them(self):
        """Left behind, it would be handed to the next subscription for
        that session - one that has already reached FLOWING and asks
        for nothing."""
        client, entry, _ = self.call_carrying(HUMAN)
        self.leave(client, {HUMAN})
        self.assertNotIn(HUMAN, entry.subscriptions)

    def test_somebody_who_was_never_carried_is_ignored(self):
        client, entry, dropped = self.call_carrying(HUMAN)
        self.leave(client, {"a-stranger"})
        self.assertEqual(dropped, [])

    def test_a_call_that_has_ended_is_left_alone(self):
        client, entry, dropped = self.call_carrying(HUMAN)
        client._call_sessions.clear()
        self.leave(client, {HUMAN})
        self.assertEqual(dropped, [])

    def test_they_can_come_back(self):
        """A microphone change is exactly this: leave, then join. The
        re-entry has to find nothing in the way."""
        client, entry, _ = self.call_carrying(HUMAN)
        self.leave(client, {HUMAN})
        entry.media.alive_for = lambda sessionid: False
        entry.media.subscriber_alive = False
        started = []
        audio = human_audio.HumanAudio(client)
        async def start(sip_call_id, media, sessionid):
            started.append(sessionid)
        audio.start = start
        asyncio.new_event_loop().run_until_complete(
            audio.start_for_late_joiner(CALL, {HUMAN}))
        self.assertEqual(started, [HUMAN])

    def test_closing_a_subscription_cannot_end_the_call(self):
        """Only the publisher is watched for the connection dying, and
        that is what ends a call. A subscription that goes has to be
        able to go quietly, or one person hanging up would take the
        telephone call with them."""
        import inspect

        import call_media
        watched = [line.strip() for line
                   in inspect.getsource(call_media.CallMedia).splitlines()
                   if "_watch(" in line and not line.strip().startswith("def ")]
        self.assertEqual(watched, ["self._watch(self.publisher)"],
                         "something other than the publisher is watched for death")
