"""The dialogue a conference call starts with.

Two things have to hold and neither is obvious from the code. A caller
must be able to get in with the number they were mailed - typed slowly,
corrected with *, ended with # or with nothing at all - and a caller must
NOT be able to get in with anything else, including by keeping a bad
guess going until the bridge tires. Wrong answers therefore cost an
attempt, and the attempts run out.
"""
import unittest

from tests.support import needs_media_stack

try:
    import numpy as np
    import talk_sip_bridge
    from dialin_ivr import DialInIvr
except ImportError:      # no media stack
    np = None


class FakeRtp:
    """Enough of an RtpSession for a dialogue: it knows its clock and
    remembers what was played."""

    sample_rate = 8000
    samples_per_packet = 160

    def __init__(self):
        self.sent = []

    def send_pcm(self, chunk):
        self.sent.append(np.asarray(chunk))

    def played_seconds(self) -> float:
        return sum(len(c) for c in self.sent) / self.sample_rate


def room(token="7052318694", actor_type="guests", actor_id="guest-hash"):
    return {"token": token, "actorType": actor_type, "actorId": actor_id}


class Nextcloud:
    """Stands in for the two endpoints, and records what was asked."""

    def __init__(self, *, meetings=("7052318694",), pins=None):
        self.meetings = set(meetings)
        self.pins = dict(pins or {})
        self.asked = []

    def __call__(self, token, pin=None):
        self.asked.append((token, pin))
        if token in self.pins:
            if pin is None:
                return talk_sip_bridge.NEEDS_PIN, None
            if pin == self.pins[token]:
                return talk_sip_bridge.OK, room(token, "users", "sip-tester")
            return talk_sip_bridge.UNKNOWN, None
        if token in self.meetings:
            if pin is not None:
                return talk_sip_bridge.UNKNOWN, None
            return talk_sip_bridge.OK, room(token)
        return talk_sip_bridge.UNKNOWN, None


def ivr_for(nextcloud, **kw):
    settings = {"attempts": 2, "first_digit_timeout": 0.3, "next_digit_timeout": 0.15}
    settings.update(kw)
    return DialInIvr(FakeRtp(), resolve=nextcloud, **settings)


def keys(ivr, typed: str):
    for digit in typed:
        ivr.press(digit)


@needs_media_stack
class MeetingIdTest(unittest.TestCase):
    def test_a_meeting_id_ending_in_hash_gets_the_caller_in(self):
        nextcloud = Nextcloud()
        ivr = ivr_for(nextcloud)
        keys(ivr, "7052318694#")
        self.assertEqual(ivr.run()["token"], "7052318694")
        self.assertEqual(nextcloud.asked, [("7052318694", None)])

    def test_a_caller_who_does_not_press_hash_is_still_let_in(self):
        """Nobody reads "end with hash" off a beep, and a pause says the
        same thing."""
        nextcloud = Nextcloud()
        ivr = ivr_for(nextcloud)
        keys(ivr, "7052318694")
        self.assertEqual(ivr.run()["token"], "7052318694")

    def test_star_clears_a_mistyped_number(self):
        nextcloud = Nextcloud()
        ivr = ivr_for(nextcloud)
        keys(ivr, "999*7052318694#")
        self.assertEqual(ivr.run()["token"], "7052318694")
        self.assertEqual(nextcloud.asked, [("7052318694", None)])

    def test_a_hash_with_nothing_in_front_of_it_is_not_an_answer(self):
        """It is what arrives when a caller finishes one attempt just as
        the next prompt starts - spending an attempt on it would cut the
        call short for a caller who is still typing."""
        nextcloud = Nextcloud()
        ivr = ivr_for(nextcloud, attempts=1)
        keys(ivr, "#7052318694#")
        self.assertEqual(ivr.run()["token"], "7052318694")
        self.assertEqual(nextcloud.asked, [("7052318694", None)])

    def test_pressing_nothing_at_all_ends_the_call(self):
        nextcloud = Nextcloud()
        self.assertIsNone(ivr_for(nextcloud).run())
        self.assertEqual(nextcloud.asked, [], "nothing should have been asked")


@needs_media_stack
class PinTest(unittest.TestCase):
    def test_a_conversation_that_wants_a_pin_asks_for_one(self):
        nextcloud = Nextcloud(meetings=(), pins={"7052318694": "1234567"})
        ivr = ivr_for(nextcloud)
        keys(ivr, "7052318694#")
        ivr.press("1")  # queued behind the first prompt; the rest follows below

        # The PIN is keyed in after the meeting id has been answered, so
        # it cannot be queued up front - the dialogue is fed as it runs.
        original = ivr.resolve

        def resolve(token, pin=None):
            outcome = original(token, pin)
            if outcome[0] == talk_sip_bridge.NEEDS_PIN:
                keys(ivr, "234567#")
            return outcome

        ivr.resolve = resolve
        result = ivr.run()
        self.assertIsNotNone(result)
        self.assertEqual(result["actorType"], "users")
        self.assertEqual(nextcloud.asked, [("7052318694", None), ("7052318694", "1234567")])

    def test_a_wrong_pin_does_not_get_in(self):
        nextcloud = Nextcloud(meetings=(), pins={"7052318694": "1234567"})
        ivr = ivr_for(nextcloud, attempts=1)
        keys(ivr, "7052318694#")
        original = ivr.resolve

        def resolve(token, pin=None):
            outcome = original(token, pin)
            if outcome[0] == talk_sip_bridge.NEEDS_PIN:
                keys(ivr, "7654321#")
            return outcome

        ivr.resolve = resolve
        self.assertIsNone(ivr.run())


@needs_media_stack
class RefusalTest(unittest.TestCase):
    """What must not happen."""

    def test_a_meeting_that_does_not_exist_gets_nowhere(self):
        nextcloud = Nextcloud(meetings=("7052318694",))
        ivr = ivr_for(nextcloud, attempts=1)
        keys(ivr, "9999999999#")
        self.assertIsNone(ivr.run())

    def test_guessing_runs_out_of_attempts(self):
        """Each wrong answer costs one, and the call ends - otherwise a
        caller can sit on the line trying numbers."""
        nextcloud = Nextcloud(meetings=("7052318694",))
        ivr = ivr_for(nextcloud, attempts=3)
        for guess in ("1111111111#", "2222222222#", "3333333333#", "7052318694#"):
            keys(ivr, guess)
        self.assertIsNone(ivr.run())
        self.assertEqual([t for t, _ in nextcloud.asked],
                         ["1111111111", "2222222222", "3333333333"])

    def test_a_bridge_that_may_not_ask_stops_after_the_first_try(self):
        """REFUSED is this bridge's own problem - repeating it only keeps
        a caller listening to beeps."""
        asked = []

        def refuse(token, pin=None):
            asked.append(token)
            return talk_sip_bridge.REFUSED, None

        ivr = ivr_for(refuse, attempts=3)
        keys(ivr, "7052318694#")
        self.assertIsNone(ivr.run())
        self.assertEqual(len(asked), 1)

    def test_a_caller_who_hangs_up_ends_the_dialogue(self):
        nextcloud = Nextcloud()
        ivr = ivr_for(nextcloud, first_digit_timeout=30)
        ivr.stop()
        self.assertIsNone(ivr.run())
        self.assertEqual(nextcloud.asked, [])


@needs_media_stack
class AudioTest(unittest.TestCase):
    def test_the_caller_hears_something_to_answer(self):
        ivr = ivr_for(Nextcloud(), attempts=1)
        ivr.run()
        self.assertGreater(ivr.rtp.played_seconds(), 0.2)

    def test_a_key_pressed_early_cuts_the_prompt_short(self):
        """A caller who knows the number should not have to sit through
        the announcement."""
        nextcloud = Nextcloud()
        ivr = ivr_for(nextcloud)
        ivr.prompts = [np.zeros(8000 * 10, dtype=np.int16)]   # ten seconds
        keys(ivr, "7052318694#")
        self.assertIsNotNone(ivr.run())
        self.assertLess(ivr.rtp.played_seconds(), 2.0)

    def test_the_second_language_is_played_when_the_first_gets_no_answer(self):
        german = np.full(800, 1000, dtype=np.int16)
        english = np.full(800, 2000, dtype=np.int16)
        ivr = ivr_for(Nextcloud(), attempts=1, prompt_gap=0.2)
        ivr.prompts = [german, english]
        ivr.run()
        played = np.concatenate(ivr.rtp.sent)
        self.assertIn(2000, played, "the second prompt was never played")

    def test_the_second_language_is_skipped_once_the_caller_answers(self):
        """A caller who understood the first one is already keying in;
        playing the translation over that is noise on the line."""
        german = np.full(800, 1000, dtype=np.int16)
        english = np.full(800, 2000, dtype=np.int16)
        nextcloud = Nextcloud()
        ivr = ivr_for(nextcloud, prompt_gap=5)
        ivr.prompts = [german, english]
        keys(ivr, "7052318694#")
        self.assertIsNotNone(ivr.run())
        played = np.concatenate(ivr.rtp.sent)
        self.assertNotIn(2000, played)

    def test_the_tones_are_all_different(self):
        """Ask, ask-for-PIN, accepted and rejected have to be told apart
        by ear, or the dialogue cannot be followed without sight of a
        screen."""
        ivr = ivr_for(Nextcloud())
        signatures = {bytes(np.asarray(t).tobytes()[:400])
                      for t in (ivr.prompts[0], ivr.pin_prompts[0], ivr.accepted, ivr.rejected)}
        self.assertEqual(len(signatures), 4)


if __name__ == "__main__":
    unittest.main()
