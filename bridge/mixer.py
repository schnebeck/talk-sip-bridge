# talk-sip-bridge - bridge/mixer.py
# Several participants' audio summed into the one stream a phone can carry.
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
"""Several participants' audio summed into the one stream a phone can carry.

A SIP call is one RTP stream with one channel, so a caller who is to
hear a room rather than one person needs the room summed for them. Talk
gives no such sum: its signaling server is an SFU, and every publisher
arrives separately. So it is built here.

**Mix-minus, without the minus.** The rule is that nobody hears
themselves. Since each call builds its own sum from separate sources,
that is done by never adding the call's own stream - not by subtracting
it afterwards. The difference matters: a sum that has been through a
limiter or a codec is no longer linear, and subtracting from it leaves
an audible, distorted remnant of one's own voice.

**Deterministic.** Nothing here reads a clock. Time is counted in
blocks, handed in by whatever drives the output, so the same sequence of
writes and reads always produces the same samples - which is what makes
it testable at all. Sources are summed as int32, where addition is exact
and order therefore cannot change the result.

That guarantee is per *sequence of calls*. When the writes and the
reads race in real time, which of them lands first is not ours to
decide; what is ours is that each one happens whole.

**Thread-safe, and the GIL is not what makes it so.** A call writes
from whichever thread aiortc delivers frames on and reads from the one
pacing RTP. The GIL would not protect this: numpy releases it inside
array operations, and even `self.available -= n` is three bytecodes
with room between them. A read interrupted by an overrunning write
would take its samples from indices that moved under it. So one lock
covers every entry point, held for the few dozen microseconds the work
takes - measured at 1.5% of a block's budget with eight sources, so
contention is not a concern at these rates.

**Fast.** One vectorised pass per block: a ring buffer copy per source,
one accumulate, one gain ramp. No allocation per sample and no Python
loop over samples.

What this does NOT do: acoustic echo cancellation. Mix-minus removes a
participant's own *digital* contribution. What their loudspeaker emits
and their microphone picks up again comes back as a new signal, and
nothing here can tell it from speech - see docs/CONCEPT.md.
"""
import threading

import numpy as np

# One RTP packet at Talk's rate. Everything is counted in blocks of this,
# including the times below, so that no clock is needed anywhere.
SAMPLE_RATE = 48000
BLOCK = 960                     # 20ms at 48kHz

# How much audio a source may hold before the oldest is dropped. Beyond
# this a source is not jittery, it is behind, and keeping it only adds
# delay to everyone in the room.
CAPACITY_BLOCKS = 10            # 200ms

# Room for the sum to exceed one speaker before the limiter works, and
# the level it is held to. Below full scale, because the resampler and
# the codec after it both overshoot slightly on steep edges.
CEILING = 30000

# A source quieter than this for HOLD_BLOCKS in a row stops being added.
# Four open microphones in a quiet room are four noise floors; this is
# what keeps a sum from hissing more than any of its parts.
GATE_THRESHOLD = 300
HOLD_BLOCKS = 25                # 500ms - long enough to bridge a syllable


class SourceBuffer:
    """One participant's audio, turned from chunks as they arrive into
    blocks as they are wanted.

    A ring buffer rather than a queue of chunks: reading a block is then
    one or two array copies whatever the writes looked like. Underrun is
    silence and is counted, because a source that underruns constantly
    is a source with a problem worth naming. Overrun drops the oldest,
    which bounds the delay this source can add to the room.
    """

    def __init__(self, capacity: int = CAPACITY_BLOCKS * BLOCK):
        self.data = np.zeros(capacity, dtype=np.int16)
        self.capacity = capacity
        self.read_at = 0
        self.available = 0
        self.underruns = 0
        self.dropped = 0

    def write(self, pcm: np.ndarray):
        pcm = np.asarray(pcm, dtype=np.int16).reshape(-1)
        if len(pcm) >= self.capacity:
            # A single write larger than the whole buffer: keep its tail,
            # which is the most recent audio in it.
            self.dropped += self.available + len(pcm) - self.capacity
            pcm = pcm[-self.capacity:]
            self.data[:] = pcm
            self.read_at = 0
            self.available = self.capacity
            return
        free = self.capacity - self.available
        if len(pcm) > free:
            behind = len(pcm) - free
            self.read_at = (self.read_at + behind) % self.capacity
            self.available -= behind
            self.dropped += behind
        at = (self.read_at + self.available) % self.capacity
        first = min(len(pcm), self.capacity - at)
        self.data[at:at + first] = pcm[:first]
        if first < len(pcm):
            self.data[:len(pcm) - first] = pcm[first:]
        self.available += len(pcm)

    def read(self, count: int) -> np.ndarray:
        """Exactly `count` samples, zero-filled where there were none."""
        out = np.zeros(count, dtype=np.int16)
        have = min(count, self.available)
        if have:
            first = min(have, self.capacity - self.read_at)
            out[:first] = self.data[self.read_at:self.read_at + first]
            if first < have:
                out[first:have] = self.data[:have - first]
            self.read_at = (self.read_at + have) % self.capacity
            self.available -= have
        if have < count:
            self.underruns += 1
        return out


class NoiseGate:
    """Holds a source out of the sum while it has nothing to contribute.

    Opens on the first block above the threshold and closes only after
    the level has stayed below it for a while, so that the gap between
    two words does not chop the second one off. The decision is per
    block and counted in blocks: no clock, and the same input always
    gates the same way.
    """

    def __init__(self, threshold: int = GATE_THRESHOLD, hold: int = HOLD_BLOCKS):
        self.threshold = threshold
        self.hold = hold
        self.quiet_blocks = hold + 1        # starts closed
        self.open = False

    def passes(self, block: np.ndarray) -> bool:
        if int(np.abs(block).max(initial=0)) >= self.threshold:
            self.quiet_blocks = 0
            self.open = True
        else:
            self.quiet_blocks += 1
            if self.quiet_blocks > self.hold:
                self.open = False
        return self.open


class Limiter:
    """Keeps the sum inside what a 16-bit sample can hold.

    Dividing by the number of sources is the obvious answer and the
    wrong one: it makes a single speaker quiet in proportion to how many
    other people are in the room and silent. So the sources are summed
    at their own level, and the result is held down only when it
    actually exceeds the ceiling, then released slowly so the level does
    not pump between syllables.

    **One block of look-ahead**, which is where the delay in this module
    comes from. Without it the gain for a loud block can only be applied
    from the *next* block onwards, and the peak that triggered it goes
    out at full level - measured, not feared: a fourfold overshoot came
    through at 32767 and clipped. So a block is held back, the gain is
    computed from the block behind it, and the held one is emitted
    ramped to exactly the gain its successor needs. The cost is 20ms on
    a path that already carries a hundred or more, and the gain never
    steps at a block edge, which would be a click.
    """

    def __init__(self, ceiling: int = CEILING, release: float = 0.9995):
        self.ceiling = ceiling
        self.release = release
        self.gain = 1.0
        self.held = None

    def apply(self, mix: np.ndarray) -> np.ndarray:
        peak = int(np.abs(mix).max(initial=0))
        needed = 1.0 if peak <= self.ceiling else self.ceiling / peak
        previous = self.gain
        if needed < self.gain:
            self.gain = needed                       # attack: at once
        else:
            # Release: towards 1.0, never past what this block allows.
            self.gain = min(needed, 1.0 - (1.0 - self.gain) * self.release ** len(mix))
        held, self.held = self.held, mix.copy()
        if held is None:
            return np.zeros(len(mix), dtype=np.int16)
        ramp = np.linspace(previous, self.gain, len(held), dtype=np.float32)
        return np.clip(held * ramp, -32768, 32767).astype(np.int16)


class Mixer:
    """The sum one call sends to its phone.

    One instance per call. Its own published audio is simply never a
    source, which is the whole of mix-minus here.
    """

    def __init__(self, block: int = BLOCK, gate: bool = True,
                 threshold: int = GATE_THRESHOLD, ceiling: int = CEILING):
        self.block = block
        self.gate_enabled = gate
        self.threshold = threshold
        self.sources = {}
        self.gates = {}
        self.limiter = Limiter(ceiling=ceiling)
        self._accumulator = np.zeros(block, dtype=np.int32)
        self.blocks_read = 0
        self._lock = threading.Lock()

    def add(self, key: str):
        with self._lock:
            self._add(key)

    def _add(self, key: str):
        if key not in self.sources:
            self.sources[key] = SourceBuffer()
            self.gates[key] = NoiseGate(threshold=self.threshold)

    def remove(self, key: str):
        with self._lock:
            self.sources.pop(key, None)
            self.gates.pop(key, None)

    def write(self, key: str, pcm: np.ndarray):
        """Audio from one participant. Unknown keys are added, so a
        stream that starts before anybody says it exists is not lost."""
        with self._lock:
            self._add(key)
            self.sources[key].write(pcm)

    def read(self) -> np.ndarray:
        """One block of the sum. Always exactly `block` samples, even
        with no sources at all - the phone is owed a steady stream, and
        silence is what a room with nobody talking sounds like."""
        with self._lock:
            self._accumulator[:] = 0
            # Sorted, so the result cannot depend on the order
            # participants happened to join in. The sum is int32 and
            # therefore exact, so this is about reproducibility of the
            # gates, not of the sum.
            for key in sorted(self.sources):
                block = self.sources[key].read(self.block)
                if self.gate_enabled and not self.gates[key].passes(block):
                    continue
                self._accumulator += block
            self.blocks_read += 1
            return self.limiter.apply(self._accumulator)

    def stats(self) -> dict:
        with self._lock:
            return self._stats()

    def _stats(self) -> dict:
        return {
            "sources": len(self.sources),
            "blocks": self.blocks_read,
            "open": sum(1 for g in self.gates.values() if g.open),
            "underruns": sum(s.underruns for s in self.sources.values()),
            "dropped": sum(s.dropped for s in self.sources.values()),
            "gain": round(self.limiter.gain, 3),
        }
