# talk-sip-bridge - bridge/dtmf_inband.py
# Hearing key presses that arrive as sound rather than as events.
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

"""Hearing key presses that arrive as sound rather than as events.

A gateway is supposed to pass RFC 4733 events through once both sides
negotiate them. This deployment's gateway does not: it agrees to events
and then plays the tones into the audio instead - measured twice, once
from a synthetic caller and once from its own DECT handset. Anything a
caller types is therefore only in the audio, and the only way to read it
is to listen.

A key press is two sine tones at once, one from a low group and one from
a high group, and which pair says which key. Goertzel evaluates a single
frequency far more cheaply than a whole spectrum, which is what makes
checking eight of them per 20ms packet affordable in the receive path.

What makes this hard is not detecting a tone but not detecting one:
speech contains every frequency eventually, and a false digit in a room
number is worse than a missed one. Hence the three conditions below, all
of which must hold together, and the requirement that a press persist.
"""
import numpy as np

LOW_TONES = (697, 770, 852, 941)
HIGH_TONES = (1209, 1336, 1477, 1633)
KEYS = (("1", "2", "3", "A"),
        ("4", "5", "6", "B"),
        ("7", "8", "9", "C"),
        ("*", "0", "#", "D"))

# What distinguishes a key press from speech is not that two frequencies
# are loud but that the other six are not: measured through a real
# gateway, the other six carry at most 4% between them while the pair
# carries the rest.
#
# The two tones are emphatically NOT equally loud. Measured on this
# gateway's own handset, the low one carries up to 0.88 and the high one
# as little as 0.11 - "twist", which the standard allows and a detector
# built against synthetic tones of equal amplitude rejects. So the pair is
# judged together, each tone only has to be clearly present, and each has
# to dominate its own group.
MIN_PAIR_SHARE = 0.60      # the two tones together, as a share of all eight
MIN_TONE_SHARE = 0.05      # each of them on its own - a single tone fails here
MIN_GROUP_MARGIN = 4.0     # the winner of a group, against its runner-up
MAX_OTHER_SHARE = 0.10     # the loudest of the other six
MIN_LEVEL = 500.0          # peak amplitude; below this it is room noise

# How long a press has to hold, and how much of it may be missing.
#
# Two blocks is 40ms, which is the shortest press a receiver has to
# accept (ITU-T Q.24). Measured against a mobile client's keypad, its
# tones last 60-80ms and arrive as three or four blocks of which one is
# routinely unreadable - the onset block often reads as the key one row
# below, and a block straddling the end of the tone reads as nothing. A
# detector that demanded three *consecutive* blocks therefore read six
# keys out of twelve, and read two of them wrong.
#
# So a single unreadable block no longer ends the press. What ends it is
# a different key, or silence lasting longer than that.
MIN_BLOCKS = 2
MAX_GAP_BLOCKS = 1


def goertzel_power(samples: np.ndarray, frequency: float, sample_rate: int) -> float:
    """Energy at one frequency. The whole filter is two multiplies and two
    adds per sample, which is why eight of these cost less than one FFT."""
    k = 2.0 * np.cos(2.0 * np.pi * frequency / sample_rate)
    s1 = s2 = 0.0
    for sample in samples:
        s0 = sample + k * s1 - s2
        s2, s1 = s1, s0
    return s1 * s1 + s2 * s2 - k * s1 * s2


def _goertzel_block(samples: np.ndarray, frequencies, sample_rate: int) -> np.ndarray:
    """The same filter for several frequencies, vectorised over samples -
    a Python loop over 320 samples per packet per frequency is too slow for
    the receive path, this is not."""
    n = len(samples)
    indices = np.arange(n)
    powers = []
    for frequency in frequencies:
        angle = 2.0 * np.pi * frequency * indices / sample_rate
        real = float(np.dot(samples, np.cos(angle)))
        imaginary = float(np.dot(samples, np.sin(angle)))
        powers.append(real * real + imaginary * imaginary)
    return np.array(powers)


def digit_in_block(samples: np.ndarray, sample_rate: int):
    """The key this block of audio carries, or None.

    None for silence, for speech, for a single tone, and for two tones of
    the same group - all of which happen and none of which is a key."""
    samples = np.asarray(samples, dtype=np.float64)
    if samples.size == 0 or np.abs(samples).max() < MIN_LEVEL:
        return None
    powers = _goertzel_block(samples, LOW_TONES + HIGH_TONES, sample_rate)
    total = powers.sum()
    if total <= 0:
        return None
    shares = powers / total

    low = int(np.argmax(shares[:4]))
    high = int(np.argmax(shares[4:]))
    low_share, high_share = shares[low], shares[4 + high]
    if low_share < MIN_TONE_SHARE or high_share < MIN_TONE_SHARE:
        return None
    if low_share + high_share < MIN_PAIR_SHARE:
        return None
    if not _dominates(shares[:4], low) or not _dominates(shares[4:], high):
        return None
    others = np.delete(shares, [low, 4 + high])
    if others.max() > MAX_OTHER_SHARE:
        return None
    return KEYS[low][high]


def _dominates(group: np.ndarray, winner: int) -> bool:
    """One tone per group, not two. Two tones of the same group are a
    chord, a ring-back or speech - never a key."""
    runner_up = float(np.delete(group, winner).max())
    return runner_up <= 0 or group[winner] >= MIN_GROUP_MARGIN * runner_up


class InbandDtmf:
    """One digit per key press, from audio.

    A press has to hold for several blocks before it counts, and the same
    key is not reported again until something else has been heard - a tone
    lasting a second is one press, not fifty.
    """

    def __init__(self, sample_rate: int, min_blocks: int = MIN_BLOCKS,
                 max_gap_blocks: int = MAX_GAP_BLOCKS):
        self.sample_rate = sample_rate
        self.min_blocks = min_blocks
        self.max_gap_blocks = max_gap_blocks
        self._candidate = None
        self._blocks = 0
        self._gap = 0
        self._reported = None

    def feed(self, samples: np.ndarray):
        """The digit if this block completes a new press, else None."""
        digit = digit_in_block(samples, self.sample_rate)
        if digit is None:
            self._gap += 1
            if self._gap > self.max_gap_blocks:
                # Long enough to be the end of the press rather than one
                # unreadable block inside it.
                self._candidate = None
                self._blocks = 0
                self._reported = None
            return None
        self._gap = 0
        if digit != self._candidate:
            self._candidate = digit
            self._blocks = 0
            self._reported = None          # a different key is a new press
        self._blocks += 1
        if digit == self._reported or self._blocks < self.min_blocks:
            return None
        self._reported = digit
        return digit
