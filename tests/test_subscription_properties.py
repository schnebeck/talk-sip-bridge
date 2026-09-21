"""The subscription machine against sequences nobody wrote down.

Every other test here walks a path somebody thought of, which is why
they tend to be written after a call has already gone wrong. This one
goes the other way: it throws thousands of random event sequences at
the machine and checks that the promises it makes hold in all of them.

The promises, in the order they matter:

1. A working connection is never disturbed. Once audio flows, no event
   may produce an action - that is what tore down a live call.
2. Repairs are bounded. The attempts never exceed the budget, whatever
   order the failures arrive in.
3. One negotiation at a time. A step decided in an older generation is
   never still current, so nothing acted on late can reach into what
   replaced it.
4. A closed call stays closed, and answers nothing.

Seeded, so a failure is reproducible: the seed is in the message.
"""
import random
import unittest

from subscription import Action, State, Subscription

EVENTS = ("start", "offer", "answer_sent", "refused", "no_publisher",
          "media_arrived", "close")
SEQUENCES = 2000
LENGTH = 12


def apply(sub, event, rng):
    """One event, with the arguments that event takes."""
    if event == "offer":
        return sub.offer(f"sid-{rng.randint(1, 3)}")
    if event == "answer_sent":
        # Sometimes the generation the machine is on, sometimes an older
        # one - a late answer is exactly what has to be ignored.
        return sub.answer_sent(rng.choice([sub.generation, max(0, sub.generation - 1)]))
    if event == "refused":
        return sub.refused(rng.choice([None, sub.generation, max(0, sub.generation - 1)]))
    return getattr(sub, event)()


class PropertyTest(unittest.TestCase):
    def sequences(self):
        for seed in range(SEQUENCES):
            rng = random.Random(seed)
            yield seed, rng, [rng.choice(EVENTS) for _ in range(LENGTH)]

    def test_a_working_connection_is_never_disturbed(self):
        for seed, rng, events in self.sequences():
            sub = Subscription(max_attempts=3)
            for event in events:
                was_flowing = sub.state is State.FLOWING
                step = apply(sub, event, rng)
                if was_flowing and event != "close":
                    self.assertEqual(step.action, Action.NOTHING,
                                     f"seed {seed}: {event} acted on a working connection")
                    self.assertEqual(sub.state, State.FLOWING,
                                     f"seed {seed}: {event} left FLOWING")

    def test_repairs_stay_within_the_budget(self):
        for seed, rng, events in self.sequences():
            sub = Subscription(max_attempts=3)
            for event in events:
                apply(sub, event, rng)
                self.assertLessEqual(sub.attempts, 3, f"seed {seed}: {sub!r}")

    def test_an_older_generation_is_never_current(self):
        for seed, rng, events in self.sequences():
            sub = Subscription(max_attempts=3)
            steps = []
            for event in events:
                step = apply(sub, event, rng)
                if step:
                    steps.append(step)
                for earlier in steps[:-1]:
                    if earlier.generation != sub.generation:
                        self.assertFalse(sub.still_current(earlier),
                                         f"seed {seed}: a step from generation "
                                         f"{earlier.generation} is current at {sub.generation}")

    def test_the_generation_only_ever_moves_forward(self):
        for seed, rng, events in self.sequences():
            sub = Subscription(max_attempts=3)
            generation = sub.generation
            for event in events:
                apply(sub, event, rng)
                self.assertGreaterEqual(sub.generation, generation, f"seed {seed}")
                generation = sub.generation

    def test_a_closed_call_answers_nothing(self):
        for seed, rng, events in self.sequences():
            sub = Subscription(max_attempts=3)
            for event in events:
                apply(sub, event, rng)
            sub.close()
            for event in EVENTS:
                step = apply(sub, event, rng)
                self.assertEqual(step.action, Action.NOTHING,
                                 f"seed {seed}: {event} acted on a closed call")
                self.assertEqual(sub.state, State.CLOSED, f"seed {seed}: {event} reopened it")

    def test_an_answer_always_belongs_to_the_newest_offer(self):
        for seed, rng, events in self.sequences():
            sub = Subscription(max_attempts=3)
            for event in events:
                step = apply(sub, event, rng)
                if step.action is Action.ANSWER:
                    self.assertEqual(step.sid, sub.sid, f"seed {seed}")
                    self.assertEqual(step.generation, sub.generation, f"seed {seed}")


if __name__ == "__main__":
    unittest.main()
