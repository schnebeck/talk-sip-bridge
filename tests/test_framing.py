# talk-sip-bridge - tests/test_framing.py
# Finding message boundaries in a TCP stream.
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

"""Finding message boundaries in a TCP stream.

A datagram is one message and needs no framing. A stream is not: the only
thing that says where a message ends is its own Content-Length. Get this
wrong and a message is truncated or two are glued together - in both cases
without an error, which is why it is worth its own tests.
"""
import unittest

from sip_messages import content_length_of, split_messages


def message(start_line: str, body: str = "", **headers) -> bytes:
    lines = [start_line]
    lines += [f"{name.replace('_', '-')}: {value}" for name, value in headers.items()]
    lines.append(f"Content-Length: {len(body)}")
    lines.append("")
    lines.append(body)
    return "\r\n".join(lines).encode()


OK = message("SIP/2.0 200 OK", Call_ID="a")
INVITE = message("INVITE sip:x SIP/2.0", "v=0\r\nm=audio 40000 RTP/AVP 0\r\n", Call_ID="b")


class ContentLengthTest(unittest.TestCase):
    def test_reads_the_announced_length(self):
        self.assertEqual(content_length_of("SIP/2.0 200 OK\r\nContent-Length: 42"), 42)

    def test_the_compact_form_counts_too(self):
        """"l" is the compact header name, and gateways do use it."""
        self.assertEqual(content_length_of("SIP/2.0 200 OK\r\nl: 7"), 7)

    def test_the_name_is_case_insensitive(self):
        self.assertEqual(content_length_of("SIP/2.0 200 OK\r\ncontent-length: 3"), 3)

    def test_a_missing_header_means_no_body(self):
        self.assertEqual(content_length_of("SIP/2.0 200 OK\r\nCall-ID: a"), 0)

    def test_nonsense_is_treated_as_no_body(self):
        """Better a message without its body than a reader that stops."""
        self.assertEqual(content_length_of("SIP/2.0 200 OK\r\nContent-Length: x"), 0)
        self.assertEqual(content_length_of("SIP/2.0 200 OK\r\nContent-Length: -5"), 0)

    def test_the_start_line_is_not_searched(self):
        self.assertEqual(content_length_of("Content-Length: 9 SIP/2.0"), 0)


class SplitTest(unittest.TestCase):
    def test_one_whole_message(self):
        messages, rest = split_messages(OK)
        self.assertEqual(messages, [OK])
        self.assertEqual(rest, b"")

    def test_two_messages_in_one_read(self):
        """TCP coalesces writes; a reader that assumes one message per read
        loses the second."""
        messages, rest = split_messages(OK + INVITE)
        self.assertEqual(messages, [OK, INVITE])
        self.assertEqual(rest, b"")

    def test_a_message_split_across_reads(self):
        """And it splits them too - the body may arrive separately."""
        head, tail = INVITE[:40], INVITE[40:]
        messages, rest = split_messages(head)
        self.assertEqual(messages, [])
        messages, rest = split_messages(rest + tail)
        self.assertEqual(messages, [INVITE])
        self.assertEqual(rest, b"")

    def test_headers_split_mid_line(self):
        head, tail = OK[:12], OK[12:]
        messages, rest = split_messages(head)
        self.assertEqual((messages, rest), ([], head))
        messages, _ = split_messages(rest + tail)
        self.assertEqual(messages, [OK])

    def test_a_body_that_has_not_fully_arrived_is_not_delivered(self):
        """Delivering it early truncates the SDP, and the call comes up
        without media."""
        messages, rest = split_messages(INVITE[:-5])
        self.assertEqual(messages, [])
        self.assertEqual(rest, INVITE[:-5])

    def test_the_remainder_of_a_second_message_is_kept(self):
        messages, rest = split_messages(OK + INVITE[:30])
        self.assertEqual(messages, [OK])
        self.assertEqual(rest, INVITE[:30])

    def test_a_body_is_taken_by_length_not_by_a_blank_line(self):
        """An SDP contains blank lines of its own; splitting on those would
        cut a message in half."""
        body = "v=0\r\n\r\ns=-\r\n"
        with_blank_line = message("INVITE sip:x SIP/2.0", body, Call_ID="c")
        messages, rest = split_messages(with_blank_line)
        self.assertEqual(messages, [with_blank_line])
        self.assertEqual(rest, b"")
        self.assertTrue(messages[0].endswith(body.encode()))

    def test_nothing_in_nothing_out(self):
        self.assertEqual(split_messages(b""), ([], b""))

    def test_a_stream_of_many(self):
        stream = b"".join([OK, INVITE, OK, INVITE])
        messages, rest = split_messages(stream)
        self.assertEqual(messages, [OK, INVITE, OK, INVITE])
        self.assertEqual(rest, b"")

    def test_byte_by_byte_delivers_each_message_exactly_once(self):
        """The worst case a stream can produce, and the one that catches an
        off-by-one in the boundary."""
        stream = OK + INVITE
        buffer, collected = b"", []
        for i in range(len(stream)):
            buffer += stream[i:i + 1]
            messages, buffer = split_messages(buffer)
            collected += messages
        self.assertEqual(collected, [OK, INVITE])
        self.assertEqual(buffer, b"")


class KeepaliveTest(unittest.TestCase):
    """A connection nobody writes to gets closed by the far end, which for
    an inbound call means the BYE has nowhere to go - so both ends ping it
    with bare line breaks. They are not messages."""

    def test_a_ping_before_a_message_is_skipped(self):
        messages, rest = split_messages(b"\r\n\r\n" + INVITE)
        self.assertEqual(messages, [INVITE])
        self.assertEqual(rest, b"")

    def test_pings_between_messages_are_skipped(self):
        stream = b"\r\n\r\n".join([INVITE, INVITE]) + b"\r\n\r\n"
        messages, rest = split_messages(stream)
        self.assertEqual(messages, [INVITE, INVITE])
        self.assertEqual(rest, b"")

    def test_a_ping_on_its_own_is_not_a_message(self):
        messages, rest = split_messages(b"\r\n\r\n")
        self.assertEqual(messages, [])
        self.assertEqual(rest, b"")


if __name__ == "__main__":
    unittest.main()
