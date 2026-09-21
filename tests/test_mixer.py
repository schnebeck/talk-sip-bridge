# talk-sip-bridge - tests/test_mixer.py
# Summing a room into the one stream a phone can carry.
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
"""Summing a room into the one stream a phone can carry.

A ring buffer and a limiter are both easy to write and easy to get
subtly wrong, and both fail quietly: a wrap bug is a click once a
second, a limiter bug is a call that sounds fine until three people
speak at once. So the wrap, the underrun, the overrun and the gain are
each pinned here rather than listened to.

The determinism the module promises is a property, not a comment, and
is checked as one: the same writes and reads twice, compared sample for
sample.
"""
import unittest

from tests.support import needs_media_stack

try:
    import numpy as np
    from mixer import BLOCK, Limiter, Mixer, NoiseGate, SourceBuffer
except ImportError:      # no media stack
    np = None


def tone(samples: int, frequency: float = 440.0, amplitude: int = 8000,
         rate: int = 48000, phase: int = 0):
    t = (np.arange(samples) + phase) / rate
    return (np.sin(2 * np.pi * frequency * t) * amplitude).astype(np.int16)


@needs_media_stack
class SourceBufferTest(unittest.TestCase):
    """Chunks in, blocks out, in the right order and without loss."""

    def test_what_goes_in_comes_out(self):
        buffer = SourceBuffer(capacity=1000)
        buffer.write(np.arange(300, dtype=np.int16))
        self.assertTrue(np.array_equal(buffer.read(300), np.arange(300, dtype=np.int16)))

    def test_writes_of_any_size_read_back_as_one_stream(self):
        """A WebRTC track hands over whatever a frame happened to hold;
        the phone wants a fixed block. That is the whole job."""
        buffer = SourceBuffer(capacity=4000)
        written = []
        for size in (17, 480, 3, 960, 200):
            chunk = np.arange(len(written) * 0, size, dtype=np.int16) + len(written)
            written.append(chunk)
            buffer.write(chunk)
        expected = np.concatenate(written)
        self.assertTrue(np.array_equal(buffer.read(len(expected)), expected))

    def test_reading_past_the_end_is_silence_and_is_counted(self):
        buffer = SourceBuffer(capacity=1000)
        buffer.write(np.full(100, 7, dtype=np.int16))
        out = buffer.read(300)
        self.assertEqual(list(out[:100]), [7] * 100)
        self.assertEqual(list(out[100:]), [0] * 200)
        self.assertEqual(buffer.underruns, 1)

    def test_it_wraps_without_losing_or_reordering_a_sample(self):
        """The bug this exists for: a write that straddles the end of
        the ring, read back across the same seam."""
        buffer = SourceBuffer(capacity=100)
        buffer.write(np.arange(80, dtype=np.int16))
        self.assertTrue(np.array_equal(buffer.read(80), np.arange(80, dtype=np.int16)))
        crossing = np.arange(1000, 1060, dtype=np.int16)      # starts at 80, wraps
        buffer.write(crossing)
        self.assertTrue(np.array_equal(buffer.read(60), crossing))

    def test_a_source_that_runs_ahead_loses_its_oldest_audio(self):
        """Not its newest. A source further ahead than the buffer is
        late, and keeping it would add that delay to everyone."""
        buffer = SourceBuffer(capacity=100)
        buffer.write(np.arange(80, dtype=np.int16))
        buffer.write(np.arange(1000, 1040, dtype=np.int16))   # 20 too many
        self.assertEqual(buffer.dropped, 20)
        out = buffer.read(100)
        self.assertEqual(out[0], 20)                          # the first 20 are gone
        self.assertEqual(out[-1], 1039)                       # the newest survived

    def test_a_single_write_larger_than_the_buffer_keeps_its_tail(self):
        buffer = SourceBuffer(capacity=100)
        buffer.write(np.arange(250, dtype=np.int16))
        self.assertTrue(np.array_equal(buffer.read(100),
                                       np.arange(150, 250, dtype=np.int16)))


@needs_media_stack
class NoiseGateTest(unittest.TestCase):
    def test_it_starts_closed(self):
        self.assertFalse(NoiseGate().passes(np.zeros(BLOCK, dtype=np.int16)))

    def test_one_loud_block_opens_it(self):
        self.assertTrue(NoiseGate().passes(np.full(BLOCK, 5000, dtype=np.int16)))

    def test_it_stays_open_across_a_gap_between_words(self):
        """Closing in the pause would chop the start off the next word."""
        gate = NoiseGate(threshold=300, hold=25)
        gate.passes(np.full(BLOCK, 5000, dtype=np.int16))
        for _ in range(25):
            self.assertTrue(gate.passes(np.zeros(BLOCK, dtype=np.int16)))

    def test_it_closes_once_the_silence_is_real(self):
        gate = NoiseGate(threshold=300, hold=25)
        gate.passes(np.full(BLOCK, 5000, dtype=np.int16))
        for _ in range(26):
            gate.passes(np.zeros(BLOCK, dtype=np.int16))
        self.assertFalse(gate.open)

    def test_a_noise_floor_never_opens_it(self):
        gate = NoiseGate(threshold=300)
        rng = np.random.default_rng(3)
        for _ in range(50):
            noise = rng.normal(0, 60, BLOCK).astype(np.int16)
            self.assertFalse(gate.passes(noise))


@needs_media_stack
class LimiterTest(unittest.TestCase):
    def test_it_leaves_a_quiet_signal_alone(self):
        quiet = tone(BLOCK, amplitude=4000).astype(np.int32)
        limiter = Limiter()
        self.assertEqual(int(np.abs(limiter.apply(quiet)).max()), 0)   # the empty first block
        out = limiter.apply(np.zeros(BLOCK, dtype=np.int32))
        self.assertLess(int(np.abs(out - quiet).max()), 2)    # rounding only

    def test_it_holds_a_loud_sum_to_the_ceiling(self):
        """Including the very block that caused it - which is what the
        look-ahead is for, and what the first version got wrong: a
        fourfold overshoot came out at 32767."""
        loud = (tone(BLOCK, amplitude=20000).astype(np.int32) * 4)
        limiter = Limiter(ceiling=30000)
        limiter.apply(loud)
        out = limiter.apply(loud)
        self.assertLessEqual(int(np.abs(out).max()), 30001)

    def test_nothing_ever_wraps_around(self):
        """int16 overflow is not a quiet fault: it turns a loud moment
        into a full-scale square wave, which is a bang."""
        limiter = Limiter()
        rng = np.random.default_rng(9)
        for _ in range(100):
            block = rng.integers(-200000, 200000, BLOCK, dtype=np.int64).astype(np.int32)
            out = limiter.apply(block)
            self.assertEqual(out.dtype, np.int16)
            self.assertLessEqual(int(np.abs(out).max()), 32767)

    def test_the_gain_comes_back_after_a_loud_moment(self):
        """Otherwise one shout leaves the whole call quiet."""
        limiter = Limiter(ceiling=30000)
        limiter.apply(tone(BLOCK, amplitude=30000).astype(np.int32) * 8)
        pulled_down = limiter.gain
        for _ in range(200):
            limiter.apply(tone(BLOCK, amplitude=1000).astype(np.int32))
        self.assertGreater(limiter.gain, pulled_down * 2)
        self.assertLessEqual(limiter.gain, 1.0)

    def test_the_gain_moves_across_a_block_not_at_its_edge(self):
        """A step in gain between two blocks is a click every 20ms. With
        the look-ahead the duck starts during the block *before* the
        loud one, which is exactly where it should."""
        limiter = Limiter(ceiling=1000)
        quiet = np.full(BLOCK, 900, dtype=np.int32)
        loud = np.full(BLOCK, 20000, dtype=np.int32)
        limiter.apply(quiet)                                  # held, nothing out
        out = limiter.apply(loud)                             # emits `quiet`, ramped
        self.assertEqual(int(out[0]), 900)                    # still at full gain
        self.assertLess(int(out[-1]), 100)                    # already ducked


@needs_media_stack
class MixerTest(unittest.TestCase):
    def test_an_empty_mixer_still_produces_its_block(self):
        """The phone is owed a steady stream whatever the room does."""
        out = Mixer().read()
        self.assertEqual(len(out), BLOCK)
        self.assertEqual(int(np.abs(out).max()), 0)

    def test_one_source_arrives_unchanged(self):
        """One block later than it went in - the limiter's look-ahead
        is the module's whole delay, and it is constant."""
        mixer = Mixer(gate=False)
        signal = tone(BLOCK, 440, 8000)
        mixer.write("a", signal)
        self.assertEqual(int(np.abs(mixer.read()).max()), 0)
        self.assertLess(int(np.abs(mixer.read().astype(np.int32) - signal).max()), 2)

    def test_two_sources_are_both_audible(self):
        """The point of the whole module: 440 Hz and 1150 Hz in, both
        out - which is what a caller cannot have today."""
        mixer = Mixer(gate=False)
        for i in range(4):
            mixer.write("a", tone(BLOCK, 440, 6000, phase=i * BLOCK))
            mixer.write("b", tone(BLOCK, 1150, 6000, phase=i * BLOCK))
        out = np.concatenate([mixer.read() for _ in range(4)])[BLOCK:]
        spectrum = np.abs(np.fft.rfft(out.astype(np.float64) * np.hanning(len(out))))
        freqs = np.fft.rfftfreq(len(out), 1 / 48000)
        loudest = spectrum.max()
        for wanted in (440, 1150):
            band = np.abs(freqs - wanted) < 25
            level = 20 * np.log10(spectrum[band].max() / loudest)
            self.assertGreater(level, -6, f"{wanted} Hz is not in the mix")

    def test_a_source_nobody_added_is_still_carried(self):
        """Audio can arrive before the roster says who is there."""
        mixer = Mixer(gate=False)
        mixer.write("surprise", tone(BLOCK * 2, amplitude=5000))
        mixer.read()
        self.assertGreater(int(np.abs(mixer.read()).max()), 1000)

    def test_removing_a_source_removes_its_audio(self):
        """Once the one block still inside the limiter has come out."""
        mixer = Mixer(gate=False)
        mixer.write("a", tone(BLOCK * 2, amplitude=5000))
        mixer.read()
        mixer.read()
        mixer.remove("a")
        mixer.read()                                          # the last held block
        self.assertEqual(int(np.abs(mixer.read()).max()), 0)

    def test_a_silent_source_is_gated_out(self):
        """Four open microphones in a quiet room are four noise floors."""
        mixer = Mixer()
        rng = np.random.default_rng(11)
        for _ in range(40):
            mixer.write("quiet", rng.normal(0, 50, BLOCK).astype(np.int16))
            mixer.read()
        self.assertEqual(mixer.stats()["open"], 0)

    def test_a_missing_source_does_not_stall_the_others(self):
        """One participant's stream stopping must not take the call
        with it - that is what the zero-fill is for."""
        mixer = Mixer(gate=False)
        mixer.write("a", tone(BLOCK * 3, amplitude=6000))
        mixer.write("b", tone(BLOCK, 1150, 6000))            # one block only
        for _ in range(3):
            self.assertEqual(len(mixer.read()), BLOCK)
        self.assertGreaterEqual(mixer.stats()["underruns"], 2)

    def test_the_same_input_gives_the_same_output_twice(self):
        """The promise the module makes. Two runs, sample for sample."""
        def run():
            mixer = Mixer()
            out = []
            for i in range(20):
                mixer.write("a", tone(BLOCK, 440, 9000, phase=i * BLOCK))
                mixer.write("b", tone(BLOCK, 1150, 9000, phase=i * BLOCK))
                if i == 10:
                    mixer.remove("a")
                out.append(mixer.read())
            return np.concatenate(out)
        self.assertTrue(np.array_equal(run(), run()))

    def test_the_order_sources_join_in_changes_nothing(self):
        """int32 addition is exact, so the sum cannot depend on it -
        and the gates are stepped in a fixed order so they cannot
        either."""
        def run(keys):
            mixer = Mixer()
            for i in range(10):
                for key, frequency in keys:
                    mixer.write(key, tone(BLOCK, frequency, 7000, phase=i * BLOCK))
                mixer.read()
            return mixer.read()
        forwards = run([("a", 440), ("b", 1150), ("c", 700)])
        backwards = run([("c", 700), ("b", 1150), ("a", 440)])
        self.assertTrue(np.array_equal(forwards, backwards))

    def test_writing_and_reading_race_without_corrupting_anything(self):
        """The GIL does not make this safe on its own: numpy releases it
        inside array work, and `self.available -= n` is three bytecodes.
        A read caught mid-overrun would take samples from indices that
        had moved. So writers and a reader are run at each other here,
        and what must hold is that every block is whole and the total
        number of samples adds up."""
        import threading

        mixer = Mixer(gate=False)
        blocks, faults, stop = [], [], threading.Event()

        def writer(name, frequency):
            try:
                for i in range(300):
                    mixer.write(name, tone(BLOCK, frequency, 6000, phase=i * BLOCK))
            except Exception as e:                            # noqa: BLE001
                faults.append(e)

        def reader():
            # Bounded: an unbounded spin would race just as well and
            # take a quarter of a minute doing it.
            try:
                for _ in range(1500):
                    blocks.append(mixer.read())
                    if stop.is_set():
                        return
            except Exception as e:                            # noqa: BLE001
                faults.append(e)

        threads = [threading.Thread(target=writer, args=(f"s{i}", 300 + i * 190))
                   for i in range(4)]
        consumer = threading.Thread(target=reader)
        consumer.start()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        stop.set()
        consumer.join()

        self.assertEqual(faults, [], f"threads raised: {faults[:3]}")
        self.assertTrue(blocks)
        for block in blocks:
            self.assertEqual(len(block), BLOCK)               # never a short read
            self.assertEqual(block.dtype, np.int16)
        stats = mixer.stats()
        self.assertEqual(stats["sources"], 4)

    def test_adding_and_removing_while_reading_is_safe(self):
        """A participant joining or leaving mid-call is exactly this."""
        import threading

        mixer = Mixer(gate=False)
        faults, stop = [], threading.Event()

        signal = tone(BLOCK, 440, 5000)

        def churn():
            try:
                for i in range(1500):
                    mixer.write(f"s{i % 5}", signal)
                    mixer.remove(f"s{(i + 2) % 5}")
            except Exception as e:                            # noqa: BLE001
                faults.append(e)

        def reader():
            try:
                for _ in range(1500):
                    self.assertEqual(len(mixer.read()), BLOCK)
                    if stop.is_set():
                        return
            except Exception as e:                            # noqa: BLE001
                faults.append(e)

        consumer = threading.Thread(target=reader)
        consumer.start()
        churner = threading.Thread(target=churn)
        churner.start()
        churner.join()
        stop.set()
        consumer.join()
        self.assertEqual(faults, [], f"threads raised: {faults[:3]}")

    def test_it_never_reads_a_clock(self):
        """Determinism is only true while that stays true."""
        import pathlib

        import mixer as module
        source = pathlib.Path(module.__file__).read_text()
        for forbidden in ("time.", "monotonic", "datetime", "random"):
            self.assertNotIn(forbidden, source, f"mixer.py uses {forbidden}")


if __name__ == "__main__":
    unittest.main()
