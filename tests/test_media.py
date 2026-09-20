"""Resampling between the phone call's rate and Talk's 48kHz.

Both directions of a call pass through here, so an error shows up as
speech that is too fast, too slow, or distorted - none of which a
signaling test would notice.
"""
import unittest

from tests.support import needs_media_stack

try:
    import numpy as np
    from media import (AUDIO_SAMPLE_RATE, StreamResampler, frame_to_mono_pcm,
                       resample_linear)
except ImportError:  # no media stack; every test here is skipped
    np = None

PACKET_RATE = 50          # 20ms packets, the rate a call arrives at


def tone(freq: int, rate: int, seconds: float = 0.25, amplitude: int = 8000):
    t = np.arange(int(rate * seconds))
    return (np.sin(2 * np.pi * freq * t / rate) * amplitude).astype(np.int16)


def dominant_frequency(pcm, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(pcm.astype(np.float64) * np.hanning(len(pcm))))
    return float(np.fft.rfftfreq(len(pcm), 1 / rate)[spectrum.argmax()])


def sideband_level(pcm, rate: int, offset: float) -> float:
    """Level at (dominant frequency + offset), in dB relative to the
    dominant frequency itself. Amplitude modulation shows up here as a
    matched pair at +-the modulating frequency."""
    spectrum = np.abs(np.fft.rfft(pcm.astype(np.float64) * np.hanning(len(pcm))))
    freqs = np.fft.rfftfreq(len(pcm), 1 / rate)
    f0 = freqs[spectrum.argmax()]
    band = np.abs(freqs - (f0 + offset)) < 8
    return float(20 * np.log10(spectrum[band].max() / spectrum.max()))


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
class StreamResamplerTest(unittest.TestCase):
    """A call does not arrive as one array but as a packet every 20ms, and
    resampling each packet on its own is not the same operation as
    resampling the stream."""

    def packets(self, pcm, rate):
        size = rate // PACKET_RATE
        return [pcm[i:i + size] for i in range(0, len(pcm) - size + 1, size)]

    def resampled_packet_by_packet(self, pcm, in_rate, out_rate):
        resampler = StreamResampler(in_rate, out_rate)
        return np.concatenate([resampler.process(p) for p in self.packets(pcm, in_rate)])

    def test_a_steady_tone_comes_out_without_sidebands(self):
        """The failure this exists for: resampling each packet on its own
        restarts the interpolation 50 times a second, which lands on the
        tone as sidebands at +-50Hz - about -35dB, and audible as a low
        ringing. Anything below -60dB is inaudible."""
        out = self.resampled_packet_by_packet(tone(440, 16000, seconds=1.0), 16000, 48000)
        for offset in (-PACKET_RATE, PACKET_RATE, -2 * PACKET_RATE, 2 * PACKET_RATE):
            with self.subTest(offset=offset):
                self.assertLess(sideband_level(out, 48000, offset), -60)

    def test_the_same_holds_on_the_way_back_down(self):
        """Talk's 48kHz to the call's rate, packet by packet - the
        direction that carries the person's voice to the phone."""
        out = self.resampled_packet_by_packet(tone(440, 48000, seconds=1.0), 48000, 8000)
        for offset in (-PACKET_RATE, PACKET_RATE):
            with self.subTest(offset=offset):
                self.assertLess(sideband_level(out, 8000, offset), -60)

    def test_the_tone_itself_is_unchanged(self):
        out = self.resampled_packet_by_packet(tone(440, 16000, seconds=0.5), 16000, 48000)
        self.assertAlmostEqual(dominant_frequency(out, 48000), 440, delta=5)

    def test_the_stream_keeps_its_length(self):
        """Time has to pass at the same speed on both sides: a resampler
        that hands out a sample too few per packet slowly drifts, which is
        heard as clicks once the far end's buffer runs dry."""
        pcm = tone(440, 16000, seconds=2.0)
        out = self.resampled_packet_by_packet(pcm, 16000, 48000)
        expected = len(self.packets(pcm, 16000)) * 320 * 3
        self.assertLess(abs(len(out) - expected), 5)

    def test_the_same_rate_passes_through_untouched(self):
        resampler = StreamResampler(8000, 8000)
        pcm = tone(440, 8000, seconds=0.02)
        np.testing.assert_array_equal(resampler.process(pcm), pcm)

    def test_it_picks_up_where_the_last_packet_ended(self):
        """What makes the difference: the piece before is what the first
        samples of this one are interpolated against."""
        first, second = tone(440, 16000, seconds=0.02), tone(880, 16000, seconds=0.02)
        resampler = StreamResampler(16000, 48000)
        resampler.process(first)
        self.assertEqual(resampler.previous, first[-1])
        self.assertGreater(resampler.position, 0)


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
