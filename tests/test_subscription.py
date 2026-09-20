"""The negotiation for another participant's audio, transition by
transition.

Every one of these was a live failure before it was a test. The two that
cost whole calls: a refused answer that was never repaired, and repairs
that repaired each other to death - seven rebuilds in five seconds,
because each refusal started its own.

No sockets, no server, no media stack: the machine is told what happened
and says what to do, which is the point of having it.
"""
import unittest

from subscription import Action, State, Subscription


def subscription(**kw):
    settings = {"max_attempts": 3, "request_delay": 5.0, "rebuild_delay": 3.0}
    settings.update(kw)
    return Subscription(**settings)


def connected(sub) -> int:
    """Drives one subscription to the point where audio flows, and
    returns the generation it settled on."""
    sub.start()
    step = sub.offer("sid-1")
    sub.answer_sent(step.generation)
    sub.media_arrived()
    return step.generation


class HappyPathTest(unittest.TestCase):
    def test_it_starts_by_asking(self):
        sub = subscription()
        step = sub.start()
        self.assertEqual(step.action, Action.REQUEST)
        self.assertEqual(sub.state, State.REQUESTED)

    def test_an_offer_is_answered_with_its_own_handle(self):
        sub = subscription()
        sub.start()
        step = sub.offer("sid-1")
        self.assertEqual((step.action, step.sid), (Action.ANSWER, "sid-1"))
        self.assertEqual(sub.state, State.ANSWERING)

    def test_audio_is_the_only_thing_that_counts_as_working(self):
        sub = subscription()
        sub.start()
        step = sub.offer("sid-1")
        sub.answer_sent(step.generation)
        self.assertEqual(sub.state, State.CONNECTING)
        self.assertFalse(sub.working)
        sub.media_arrived()
        self.assertEqual(sub.state, State.FLOWING)
        self.assertTrue(sub.working)

    def test_starting_twice_changes_nothing(self):
        sub = subscription()
        sub.start()
        self.assertFalse(sub.start())


class RepairTest(unittest.TestCase):
    """What happens when the server refuses, and how often."""

    def test_a_refused_answer_is_rebuilt_after_a_wait(self):
        sub = subscription()
        sub.start()
        step = sub.offer("sid-1")
        sub.answer_sent(step.generation)
        repair = sub.refused(step.generation)
        self.assertEqual(repair.action, Action.REBUILD)
        self.assertEqual(repair.delay, 3.0)
        self.assertEqual(sub.state, State.REQUESTED)

    def test_a_missing_publisher_is_asked_again_after_a_longer_wait(self):
        sub = subscription()
        sub.start()
        step = sub.no_publisher()
        self.assertEqual((step.action, step.delay), (Action.REQUEST, 5.0))

    def test_repairs_share_one_budget_and_it_runs_out(self):
        """The caller hears the waiting, not which kind of repair it
        was."""
        sub = subscription(max_attempts=3)
        sub.start()
        self.assertEqual(sub.no_publisher().action, Action.REQUEST)
        self.assertEqual(sub.refused().action, Action.REBUILD)
        self.assertEqual(sub.no_publisher().action, Action.GIVE_UP)
        self.assertEqual(sub.state, State.GIVEN_UP)

    def test_giving_up_is_said_once_and_then_nothing(self):
        sub = subscription(max_attempts=1)
        sub.start()
        self.assertEqual(sub.refused().action, Action.GIVE_UP)
        self.assertFalse(sub.refused())
        self.assertFalse(sub.no_publisher())
        self.assertFalse(sub.offer("sid-2"))


class OneNegotiationAtATimeTest(unittest.TestCase):
    """The property the scattered version could not have. Each offer
    starts a generation; anything arriving about an older one is history
    and must change nothing - otherwise two repairs run at once and tear
    down each other's work."""

    def test_a_refusal_for_a_superseded_negotiation_is_ignored(self):
        sub = subscription()
        sub.start()
        first = sub.offer("sid-1")
        second = sub.offer("sid-2")
        self.assertNotEqual(first.generation, second.generation)
        self.assertFalse(sub.refused(first.generation), "the old refusal started a repair")
        self.assertEqual(sub.state, State.ANSWERING)

    def test_an_answer_for_a_superseded_negotiation_does_not_advance(self):
        sub = subscription()
        sub.start()
        first = sub.offer("sid-1")
        sub.offer("sid-2")
        sub.answer_sent(first.generation)
        self.assertEqual(sub.state, State.ANSWERING, "the stale answer was taken for the live one")

    def test_a_repair_invalidates_what_was_in_flight(self):
        """Otherwise the answer to the offer that was just abandoned
        arrives and looks current."""
        sub = subscription()
        sub.start()
        step = sub.offer("sid-1")
        repair = sub.refused(step.generation)
        self.assertNotEqual(repair.generation, step.generation)
        sub.answer_sent(step.generation)
        self.assertEqual(sub.state, State.REQUESTED)

    def test_the_newest_offer_is_the_one_answered(self):
        sub = subscription()
        sub.start()
        sub.offer("sid-1")
        step = sub.offer("sid-2")
        self.assertEqual(step.sid, "sid-2")
        self.assertEqual(sub.sid, "sid-2")


class WorkingConnectionTest(unittest.TestCase):
    """Audio flowing is what no repair may disturb - the server keeps
    offering after a connection works, and answering those resets it."""

    def test_a_later_offer_is_ignored_once_audio_flows(self):
        sub = subscription()
        connected(sub)
        self.assertFalse(sub.offer("sid-2"))
        self.assertEqual(sub.state, State.FLOWING)

    def test_a_later_refusal_is_ignored_once_audio_flows(self):
        sub = subscription()
        generation = connected(sub)
        self.assertFalse(sub.refused(generation))
        self.assertEqual(sub.state, State.FLOWING)

    def test_a_missing_publisher_is_ignored_once_audio_flows(self):
        sub = subscription()
        connected(sub)
        self.assertFalse(sub.no_publisher())

    def test_nothing_is_attempted_before_it_started(self):
        sub = subscription()
        self.assertFalse(sub.no_publisher())
        self.assertEqual(sub.state, State.IDLE)


class EndOfCallTest(unittest.TestCase):
    def test_a_closed_call_answers_nothing_more(self):
        sub = subscription()
        connected(sub)
        sub.close()
        for attempt in (sub.offer("sid-9"), sub.refused(), sub.no_publisher(), sub.start()):
            self.assertFalse(attempt)
        self.assertEqual(sub.state, State.CLOSED)

    def test_media_after_closing_does_not_revive_it(self):
        sub = subscription()
        sub.start()
        sub.close()
        sub.media_arrived()
        self.assertEqual(sub.state, State.CLOSED)


if __name__ == "__main__":
    unittest.main()
