"""Room-call membership, replayed from recorded signaling traffic.

`fixtures/*.json` are real event streams captured from the signaling server
during real phone calls, anonymised: session ids replaced by the role they
had (`human`, `bridge-internal`, `phone-virtual`), user names removed, chat
payloads reduced to their envelope. They are inputs, not expected output -
what the tests assert is the decision the bridge takes from them.

The recorded successful call is the interesting one: three sessions enter
the call, and only one of them is a person. The other two belong to the
bridge itself.
"""
import json
import pathlib
import unittest

from room_state import RoomCallState, has_in_call_flag, is_room_wide_call_end

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

# What the bridge knows to be its own in these recordings: the internal
# client's session, and the virtual session standing in for the phone.
OURS = {"bridge-internal", "phone-virtual"}


def load(name: str) -> list:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def participant_updates(events: list) -> list:
    return [e["update"] for e in events
            if e.get("target") == "participants" and e.get("type") == "update"]


def replay(events: list, ours: set = OURS):
    """Feeds a recording through the model and collects every decision."""
    state = RoomCallState()
    accepts, departures = [], []
    for update in participant_updates(events):
        entered, left = state.apply(update)
        accepted = state.accepted_by(entered, ours)
        if accepted:
            accepts.append(accepted)
        if left:
            departures.append(left)
    return state, accepts, departures


class RecordedSuccessfulCallTest(unittest.TestCase):
    """A real inbound call that was answered in Talk and carried audio."""

    def setUp(self):
        self.events = load("success")

    def test_exactly_one_session_counts_as_answering(self):
        _, accepts, _ = replay(self.events)
        self.assertEqual(accepts, ["human"])

    def test_the_bridges_own_sessions_entering_the_call_are_not_answers(self):
        """Both of them do enter it in this recording - the virtual phone
        session at flags 9, the bridge's own publisher at 3 - so an
        implementation that forgets to exclude them reports three answers
        for one call."""
        updates = participant_updates(self.events)
        state = RoomCallState()
        all_entered = set()
        for update in updates:
            entered, _ = state.apply(update)
            all_entered |= entered
        self.assertIn("phone-virtual", all_entered)
        self.assertIn("bridge-internal", all_entered)
        self.assertEqual(sorted(all_entered - OURS), ["human"])

    def test_the_first_update_only_seeds_the_model(self):
        """It carries the room's membership, not a change."""
        state = RoomCallState()
        self.assertFalse(state.seeded)
        entered, left = state.apply(participant_updates(self.events)[0])
        self.assertEqual((entered, left), (set(), set()))
        self.assertTrue(state.seeded)

    def test_the_person_stays_in_the_call_when_the_bridge_leaves_it(self):
        """The recording ends with the phone hanging up: the bridge's own
        sessions leave, the person does not. Ending the SIP side off "the
        last one left" has to survive that."""
        state, _, _ = replay(self.events)
        self.assertTrue(state.anyone_in_call_besides(OURS))


class RecordedFailedCallsTest(unittest.TestCase):
    """Two recordings where Talk rang but nobody ever reached the call -
    the Android client joined the room and stopped there. Nothing in them
    may be read as an answer."""

    def test_joining_the_room_is_not_joining_the_call(self):
        for name in ("app_joins_room_only", "ring_then_cancel"):
            with self.subTest(recording=name):
                _, accepts, _ = replay(load(name))
                self.assertEqual(accepts, [])

    def test_a_room_join_alone_produces_no_call_membership(self):
        """These recordings contain room joins for a real user and no
        in-call transition at all."""
        for name in ("app_joins_room_only", "ring_then_cancel"):
            with self.subTest(recording=name):
                events = load(name)
                joins = [e for e in events if e.get("target") == "room" and e.get("type") == "join"]
                self.assertTrue(joins, "recording should contain a room join")
                state, _, _ = replay(events)
                self.assertFalse(state.anyone_in_call_besides(OURS))

    def test_chat_relay_is_not_what_distinguishes_them(self):
        """The session that answers in the successful recording joined the
        room with the same `chat-relay` feature as the ones that never
        answered - so the feature says nothing about it, and only the
        in-call transition does."""
        def features_of_human(events):
            for e in events:
                if e.get("target") == "room" and e.get("type") == "join":
                    for item in e["join"]:
                        if item.get("sessionid") == "human":
                            return item.get("features")
            return None

        self.assertEqual(features_of_human(load("success")), ["chat-relay"])
        self.assertEqual(features_of_human(load("app_joins_room_only")), ["chat-relay"])


class TransitionTest(unittest.TestCase):
    """The model's own rules, independent of any recording."""

    def seeded_state(self, **flags):
        state = RoomCallState()
        state.apply({"users": [{"sessionId": s, "inCall": v} for s, v in flags.items()]})
        return state

    def test_a_session_already_in_call_when_seeding_is_not_an_entry(self):
        """What made inbound calls answer themselves against a dead peer:
        a session the server still lists as in-call looks exactly like
        somebody answering, if state is read instead of change."""
        state = RoomCallState()
        entered, _ = state.apply({"users": [{"sessionId": "stale", "inCall": 7}]})
        self.assertEqual(entered, set())

    def test_entering_and_leaving_are_reported_once(self):
        state = self.seeded_state(a=0)
        self.assertEqual(state.apply({"users": [{"sessionId": "a", "inCall": 7}]}), ({"a"}, set()))
        self.assertEqual(state.apply({"users": [{"sessionId": "a", "inCall": 7}]}), (set(), set()))
        self.assertEqual(state.apply({"users": [{"sessionId": "a", "inCall": 0}]}), (set(), {"a"}))

    def test_a_snapshot_forgets_sessions_it_omits(self):
        """The only way a session that dropped without a "leave" is ever
        forgotten."""
        state = self.seeded_state(a=7, b=7)
        entered, left = state.apply({"users": [{"sessionId": "a", "inCall": 7}]})
        self.assertEqual((entered, left), (set(), {"b"}))

    def test_a_changed_delta_updates_without_replacing(self):
        state = self.seeded_state(a=0, b=0)
        entered, left = state.apply({"changed": [{"sessionId": "b", "inCall": 3}]})
        self.assertEqual((entered, left), ({"b"}, set()))
        self.assertTrue(state.anyone_in_call_besides({"a"}))

    def test_lowercase_session_key_is_accepted(self):
        state = RoomCallState()
        state.apply({"users": [{"sessionid": "a", "inCall": 0}]})
        entered, _ = state.apply({"users": [{"sessionid": "a", "inCall": 1}]})
        self.assertEqual(entered, {"a"})

    def test_several_sessions_of_one_person_only_count_when_they_move(self):
        state = self.seeded_state(browser=7, phone=0)
        entered, _ = state.apply({"users": [{"sessionId": "browser", "inCall": 7},
                                            {"sessionId": "phone", "inCall": 7}]})
        self.assertEqual(entered, {"phone"})

    def test_accepted_by_is_stable_when_several_enter_at_once(self):
        state = RoomCallState()
        self.assertEqual(state.accepted_by({"b", "a", "bridge-internal"}, OURS), "a")

    def test_accepted_by_ignores_a_room_of_only_our_own_sessions(self):
        state = RoomCallState()
        self.assertIsNone(state.accepted_by(set(OURS), OURS))


class FlagTest(unittest.TestCase):
    def test_only_the_lowest_bit_means_in_call(self):
        self.assertTrue(has_in_call_flag(1))
        self.assertTrue(has_in_call_flag(7))
        self.assertTrue(has_in_call_flag(9))
        self.assertFalse(has_in_call_flag(0))
        self.assertFalse(has_in_call_flag(2))

    def test_unusable_values_are_not_in_call(self):
        for raw in (None, "", "abc", [], {}):
            with self.subTest(raw=raw):
                self.assertFalse(has_in_call_flag(raw))

    def test_numeric_strings_are_accepted(self):
        self.assertTrue(has_in_call_flag("3"))


class RoomWideEndTest(unittest.TestCase):
    def test_the_broadcast_that_ends_a_call(self):
        self.assertTrue(is_room_wide_call_end({"all": True, "incall": 0}))
        self.assertTrue(is_room_wide_call_end({"all": True, "inCall": 0}))

    def test_a_call_starting_is_not_a_call_ending(self):
        self.assertFalse(is_room_wide_call_end({"all": True, "incall": 7}))

    def test_a_per_session_update_is_not_room_wide(self):
        self.assertFalse(is_room_wide_call_end({"users": [{"sessionId": "a", "inCall": 0}]}))

    def test_all_without_a_flag_says_nothing(self):
        self.assertFalse(is_room_wide_call_end({"all": True}))


if __name__ == "__main__":
    unittest.main()
