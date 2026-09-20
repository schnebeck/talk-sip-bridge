"""Resampling between the phone call's rate and Talk's 48kHz.

Both directions of a call pass through here, so an error shows up as
speech that is too fast, too slow, or distorted - none of which a
signaling test would notice.
"""
import unittest

from tests.support import needs_media_stack

try:
    import numpy as np
    from media import AUDIO_SAMPLE_RATE, frame_to_mono_pcm, resample_linear
except ImportError:  # no media stack; every test here is skipped
    np = None


def tone(freq: int, rate: int, seconds: float = 0.25, amplitude: int = 8000):
    t = np.arange(int(rate * seconds))
    return (np.sin(2 * np.pi * freq * t / rate) * amplitude).astype(np.int16)


def dominant_frequency(pcm, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(pcm.astype(np.float64) * np.hanning(len(pcm))))
    return float(np.fft.rfftfreq(len(pcm), 1 / rate)[spectrum.argmax()])


@needs_media_stack
class ResampleTest(unittest.TestCase):
    def test_the_same_rate_changes_nothing(self):
        pcm = tone(440, 8000)
        np.testing.assert_array_equal(resample_linear(pcm, 8000, 8000), pcm)

    def test_upsampling_lengthens_proportionally(self):
        pcm = tone(440, 8000, seconds=0.1)
        out = resample_linear(pcm, 8000, 48000)
        self.assertAlmostEqual(len(out) / len(pcm), 6, delta=0.01)

    def test_downsampling_shortens_proportionally(self):
        pcm = tone(440, 48000, seconds=0.1)
        out = resample_linear(pcm, 48000, 8000)
        self.assertAlmostEqual(len(out) / len(pcm), 1 / 6, delta=0.01)

    def test_a_tone_keeps_its_pitch_on_the_way_up(self):
        """The audible failure mode: speech an octave off."""
        out = resample_linear(tone(440, 8000), 8000, AUDIO_SAMPLE_RATE)
        self.assertAlmostEqual(dominant_frequency(out, AUDIO_SAMPLE_RATE), 440, delta=10)

    def test_a_tone_keeps_its_pitch_on_the_way_down(self):
        out = resample_linear(tone(440, AUDIO_SAMPLE_RATE), AUDIO_SAMPLE_RATE, 8000)
        self.assertAlmostEqual(dominant_frequency(out, 8000), 440, delta=10)

    def test_g722_rate_round_trips(self):
        """16kHz is what G.722 negotiates, and the rate the phone side runs
        at for an HD call."""
        original = tone(1000, 16000)
        up = resample_linear(original, 16000, AUDIO_SAMPLE_RATE)
        back = resample_linear(up, AUDIO_SAMPLE_RATE, 16000)
        self.assertAlmostEqual(dominant_frequency(back, 16000), 1000, delta=15)

    def test_the_result_stays_in_sixteen_bit_range(self):
        out = resample_linear(tone(440, 8000, amplitude=32000), 8000, AUDIO_SAMPLE_RATE)
        self.assertEqual(out.dtype, np.int16)
        self.assertLessEqual(int(np.abs(out).max()), 32767)

    def test_empty_input_does_not_raise(self):
        out = resample_linear(np.zeros(0, dtype=np.int16), 8000, 48000)
        self.assertEqual(len(out), 0)


@needs_media_stack
class FrameConversionTest(unittest.TestCase):
    def make_frame(self, samples, layout, rate=None):
        # Resolved here, not as a default: defaults are evaluated when the
        # class is defined, which happens even where the media stack is
        # absent and every test in it is skipped.
        from av import AudioFrame
        frame = AudioFrame.from_ndarray(samples, format="s16", layout=layout)
        frame.sample_rate = rate or AUDIO_SAMPLE_RATE
        return frame

    def test_mono_passes_through(self):
        samples = tone(440, AUDIO_SAMPLE_RATE, seconds=0.02).reshape(1, -1)
        pcm = frame_to_mono_pcm(self.make_frame(samples, "mono"))
        self.assertEqual(pcm.dtype, np.int16)
        self.assertEqual(len(pcm), samples.shape[1])

    def test_stereo_is_reduced_to_one_channel(self):
        """Talk's clients can send stereo; the phone line cannot carry it."""
        mono = tone(440, AUDIO_SAMPLE_RATE, seconds=0.02)
        interleaved = np.repeat(mono, 2).reshape(1, -1)
        pcm = frame_to_mono_pcm(self.make_frame(interleaved, "stereo"))
        self.assertEqual(pcm.dtype, np.int16)
        self.assertEqual(len(pcm), len(mono))


if __name__ == "__main__":
    unittest.main()
