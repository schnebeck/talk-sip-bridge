# talk-sip-bridge - tests/test_dtmf.py
# Key presses arriving as RTP events (RFC 4733).
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

"""Key presses arriving as RTP events (RFC 4733).

One press is dozens of packets, all carrying the same RTP timestamp and a
duration that grows, and the last few repeat with an end marker. Counting
them naively gives forty digits for one key; waiting for the end marker
gives none at all when it is lost. What decides is the timestamp, and that
is what these pin.
"""
import struct
import unittest

from dtmf import DigitGuard, DtmfEvents, parse_event


def event(digit: int, duration: int = 160, end: bool = False, volume: int = 10) -> bytes:
    return struct.pack("!BBH", digit, (0x80 if end else 0) | volume, duration)


class ParseTest(unittest.TestCase):
    def test_the_digits_are_where_the_standard_puts_them(self):
        self.assertEqual(parse_event(event(0))[0], "0")
        self.assertEqual(parse_event(event(9))[0], "9")
        self.assertEqual(parse_event(event(10))[0], "*")
        self.assertEqual(parse_event(event(11))[0], "#")
        self.assertEqual(parse_event(event(15))[0], "D")

    def test_the_end_marker_and_duration_are_read(self):
        digit, end, duration = parse_event(event(7, duration=1280, end=True))
        self.assertEqual((digit, end, duration), ("7", True, 1280))

    def test_something_that_is_not_an_event_is_not_guessed_at(self):
        self.assertIsNone(parse_event(b"\x00\x00"))          # too short
        self.assertIsNone(parse_event(event(16)))            # no such event


class OnePressTest(unittest.TestCase):
    """A real press, as a gateway sends it."""

    def setUp(self):
        self.events = DtmfEvents()

    def press(self, digit: int, timestamp: int, packets: int = 20):
        """Every 20ms while the key is down, then three end packets - all
        under the one timestamp the press started at."""
        seen = []
        for i in range(packets):
            seen.append(self.events.feed(timestamp, event(digit, duration=160 * (i + 1))))
        for _ in range(3):
            seen.append(self.events.feed(timestamp, event(digit, duration=160 * packets, end=True)))
        return [d for d in seen if d is not None]

    def test_one_press_is_one_digit(self):
        self.assertEqual(self.press(5, timestamp=1000), ["5"])

    def test_the_same_key_twice_counts_twice(self):
        """Different presses carry different timestamps - that is the only
        thing telling them apart, since everything else is identical."""
        self.assertEqual(self.press(5, timestamp=1000) + self.press(5, timestamp=2600), ["5", "5"])

    def test_a_digit_is_known_while_the_key_is_still_down(self):
        """Reported on the first packet, not on the end marker: waiting for
        a marker that can be lost would drop the press."""
        self.assertEqual(self.events.feed(1000, event(3)), "3")

    def test_a_press_whose_end_never_arrives_still_counts_once(self):
        digits = [self.events.feed(1000, event(8, duration=160 * i)) for i in range(1, 30)]
        self.assertEqual([d for d in digits if d], ["8"])

    def test_a_sequence_arrives_in_order(self):
        pressed = "4711*"
        digits = []
        for index, key in enumerate(pressed):
            digits += self.press("0123456789*#ABCD".index(key), timestamp=1000 + index * 1600)
        self.assertEqual("".join(digits), pressed)


class GuardTest(unittest.TestCase):
    """One press can arrive as an event, as the tone the gateway also
    plays, and as a SIP INFO. That is one press - but only when it is the
    same key."""

    def setUp(self):
        self.guard = DigitGuard(window=0.4)

    def test_the_same_press_arriving_twice_is_one_press(self):
        self.assertTrue(self.guard.accepts("5", 10.0))
        self.assertFalse(self.guard.accepts("5", 10.05))

    def test_a_different_key_is_never_collapsed(self):
        """The one that cost a caller their meeting id: ten digits were
        keyed in and seven arrived, because anything within the window
        was taken for a repeat whatever key it was."""
        typed = "7052318694"
        heard = "".join(d for i, d in enumerate(typed)
                        if self.guard.accepts(d, 10.0 + i * 0.05))
        self.assertEqual(heard, typed)

    def test_the_same_key_twice_with_a_gap_counts_twice(self):
        self.assertTrue(self.guard.accepts("1", 10.0))
        self.assertTrue(self.guard.accepts("1", 10.5))

    def test_a_repeat_after_another_key_counts(self):
        """1-1 is two presses; 1-2-1 is three, and the last one is not a
        repeat of anything inside the window it can see."""
        self.assertTrue(self.guard.accepts("1", 10.0))
        self.assertTrue(self.guard.accepts("2", 10.1))
        self.assertTrue(self.guard.accepts("1", 10.2))

    def test_the_first_press_of_a_call_is_never_a_repeat(self):
        self.assertTrue(DigitGuard().accepts("0", 0.0))


if __name__ == "__main__":
    unittest.main()
