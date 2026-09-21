"""Reading key presses out of audio, because this gateway sends them that
way however the two sides negotiated.

The risk here is not missing a key but inventing one: speech carries
energy at every frequency sooner or later, and a digit hallucinated into
a room number sends a caller somewhere they did not ask for. Half of
these tests are therefore about what must NOT be recognised.
"""
import pathlib
import unittest

from tests.support import needs_media_stack

try:
    import numpy as np
    from dtmf_inband import HIGH_TONES, KEYS, LOW_TONES, InbandDtmf, digit_in_block
except ImportError:      # no media stack
    np = None

RATE = 8000
BLOCK = 160              # 20ms, one RTP packet


def tones_for(digit: str):
    for row, keys in enumerate(KEYS):
        if digit in keys:
            return LOW_TONES[row], HIGH_TONES[keys.index(digit)]
    raise ValueError(digit)


def dtmf(digit: str, seconds: float = 0.1, amplitude: int = 8000, rate: int = RATE):
    low, high = tones_for(digit)
    t = np.arange(int(rate * seconds)) / rate
    wave = np.sin(2 * np.pi * low * t) + np.sin(2 * np.pi * high * t)
    return (wave / 2 * amplitude).astype(np.int16)


def blocks(samples, size: int = BLOCK):
    return [samples[i:i + size] for i in range(0, len(samples) - size + 1, size)]


def press(detector, digit: str, seconds: float = 0.12):
    return [d for d in (detector.feed(b) for b in blocks(dtmf(digit, seconds))) if d]


@needs_media_stack
class RecognitionTest(unittest.TestCase):
    def test_every_key_on_the_pad(self):
        for row in KEYS:
            for digit in row:
                with self.subTest(digit=digit):
                    self.assertEqual(digit_in_block(dtmf(digit)[:BLOCK], RATE), digit)

    def test_a_quiet_press_is_still_a_press(self):
        """A handset held loosely, or a gateway that turns the level down."""
        self.assertEqual(digit_in_block(dtmf("7", amplitude=1500)[:BLOCK], RATE), "7")

    def test_it_works_at_the_other_sample_rate(self):
        """Calls run at 8kHz or 16kHz depending on the codec."""
        self.assertEqual(digit_in_block(dtmf("3", rate=16000)[:320], 16000), "3")


@needs_media_stack
class RejectionTest(unittest.TestCase):
    """What must not be heard as a key."""

    def test_silence_is_not_a_key(self):
        self.assertIsNone(digit_in_block(np.zeros(BLOCK, dtype=np.int16), RATE))

    def test_room_noise_is_not_a_key(self):
        rng = np.random.default_rng(5)
        noise = (rng.normal(0, 300, BLOCK)).astype(np.int16)
        self.assertIsNone(digit_in_block(noise, RATE))

    def test_a_single_tone_is_not_a_key(self):
        """A key is two tones at once; one is a test tone, a ring-back or a
        fax getting started."""
        t = np.arange(BLOCK) / RATE
        for frequency in (697, 1336, 440):
            with self.subTest(frequency=frequency):
                tone = (np.sin(2 * np.pi * frequency * t) * 8000).astype(np.int16)
                self.assertIsNone(digit_in_block(tone, RATE))

    def test_two_tones_of_the_same_group_are_not_a_key(self):
        t = np.arange(BLOCK) / RATE
        wave = np.sin(2 * np.pi * 697 * t) + np.sin(2 * np.pi * 941 * t)
        self.assertIsNone(digit_in_block((wave / 2 * 8000).astype(np.int16), RATE))

    def test_speech_shaped_noise_is_not_a_key(self):
        """The one that matters: a caller talking must never dial."""
        rng = np.random.default_rng(11)
        heard = []
        detector = InbandDtmf(RATE)
        for _ in range(200):
            voice = sum(np.sin(2 * np.pi * f * np.arange(BLOCK) / RATE)
                        for f in rng.uniform(120, 3000, 6))
            voice = (voice / 6 * rng.uniform(2000, 12000)).astype(np.int16)
            digit = detector.feed(voice)
            if digit:
                heard.append(digit)
        self.assertEqual(heard, [], "speech was heard as key presses")


@needs_media_stack
class PressTest(unittest.TestCase):
    def setUp(self):
        self.detector = InbandDtmf(RATE)

    def test_one_press_is_one_digit(self):
        self.assertEqual(press(self.detector, "5"), ["5"])

    def test_a_long_press_is_still_one_digit(self):
        self.assertEqual(press(self.detector, "5", seconds=1.0), ["5"])

    def test_a_blip_too_short_to_be_a_press_is_ignored(self):
        """Below the minimum, a burst of the right frequencies - which
        speech can produce for a moment - does not count."""
        self.assertEqual(press(self.detector, "9", seconds=0.03), [])

    def test_the_same_key_twice_needs_a_gap(self):
        digits = press(self.detector, "1")
        digits += [d for d in (self.detector.feed(np.zeros(BLOCK, dtype=np.int16))
                               for _ in range(3)) if d]
        digits += press(self.detector, "1")
        self.assertEqual(digits, ["1", "1"])

    def test_a_sequence_arrives_in_order(self):
        typed = ""
        for digit in "4711*#":
            typed += "".join(press(self.detector, digit))
            for _ in range(3):
                self.detector.feed(np.zeros(BLOCK, dtype=np.int16))
        self.assertEqual(typed, "4711*#")


@needs_media_stack
class RealGatewayTest(unittest.TestCase):
    """The same detector against what the gateway actually sends.

    fixtures/fritzbox/dtmf-keypad.wav is every key of a FRITZ!Box DECT
    handset, recorded off the wire as the bridge received it: through
    G.722, through the gateway's own re-encoding, with only the stretches
    the detector reads as tones kept, so no speech is in the file.

    This is the recording that corrected the detector. Built against
    synthetic tones of equal amplitude it found one key in twelve: real
    presses arrive with the low tone carrying up to 0.88 of the energy and
    the high one as little as 0.11.
    """

    def read(self, name: str) -> str:
        import wave

        path = pathlib.Path(__file__).resolve().parent / "fixtures" / "fritzbox" / name
        with wave.open(str(path)) as recording:
            rate = recording.getframerate()
            pcm = np.frombuffer(recording.readframes(recording.getnframes()), dtype=np.int16)
        detector = InbandDtmf(rate)
        block = rate // 50
        return "".join(digit for i in range(0, len(pcm) - block, block)
                       if (digit := detector.feed(pcm[i:i + block])))

    def test_every_key_of_a_real_handset(self):
        self.assertEqual(self.read("dtmf-keypad.wav"), "1234567890*#")

    def test_every_key_of_a_mobile_client(self):
        """The same box, the same call path, a different thing pressing
        the keys - and much shorter tones: 60-80ms, arriving as three or
        four 20ms blocks of which one is routinely unreadable. Against
        this recording the detector read six keys of twelve and got two
        of them wrong, which is what a ten-digit meeting id arriving as
        seven digits was.

        dtmf-keypad-app.wav is FRITZ!App Fon's keypad through G.722, off
        the wire, trimmed to the stretches that read as tones - the
        double hash at the end is what was typed."""
        self.assertEqual(self.read("dtmf-keypad-app.wav"), "1234567890*##")


if __name__ == "__main__":
    unittest.main()
