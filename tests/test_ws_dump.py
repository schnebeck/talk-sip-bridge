# talk-sip-bridge - tests/test_ws_dump.py
# Reading signaling messages back out of a packet capture.
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

"""Reading signaling messages back out of a packet capture.

The tool this tests is a diagnostic one, which is exactly why it needs
tests: it is reached for when the logs on both ends disagree, and a
decoder that quietly drops a frame would settle that argument the wrong
way. A message that never appears in its output has to mean the message
was never sent.

Frames are built here rather than captured, so every awkward shape a
real capture contains - masked, split across packets, two in one, the
handshake in front of the first - is present on purpose.
"""
import importlib.util
import pathlib
import struct
import unittest

_SPEC = importlib.util.spec_from_file_location(
    "ws_dump", pathlib.Path(__file__).resolve().parent / "hardware" / "ws_dump.py")
ws_dump = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ws_dump)


def frame(payload: bytes, mask: bytes = None, opcode: int = 0x1) -> bytes:
    """One WebSocket frame, masked the way a client must mask."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    flag = 0x80 if mask else 0
    if length < 126:
        header.append(flag | length)
    elif length < 1 << 16:
        header.append(flag | 126)
        header += struct.pack("!H", length)
    else:
        header.append(flag | 127)
        header += struct.pack("!Q", length)
    if mask:
        header += mask
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + payload


def read(*chunks, seqs=None):
    """What the decoder makes of those packets, in order."""
    stream = ws_dump.Stream()
    out = []
    for index, chunk in enumerate(chunks):
        stream.add(seqs[index] if seqs else index, chunk)
        out.extend(stream.messages())
    return [data.decode() for data in out]


class FrameTest(unittest.TestCase):
    def test_a_server_frame_is_read_as_it_stands(self):
        self.assertEqual(read(frame(b'{"type":"event"}')), ['{"type":"event"}'])

    def test_a_client_frame_is_unmasked(self):
        """The direction that matters most here - what the browser sent -
        is the one RFC 6455 requires to be masked, which is why tcpdump's
        own -A shows nothing readable for it."""
        self.assertEqual(read(frame(b'{"type":"control"}', mask=b"\x01\x02\x03\x04")),
                         ['{"type":"control"}'])

    def test_two_frames_in_one_packet_are_both_read(self):
        packet = frame(b'{"a":1}') + frame(b'{"b":2}', mask=b"\xaa\xbb\xcc\xdd")
        self.assertEqual(read(packet), ['{"a":1}', '{"b":2}'])

    def test_a_frame_split_across_packets_comes_out_whole(self):
        whole = frame(b'{"type":"control","control":{"data":{"type":"hangup"}}}')
        self.assertEqual(read(whole[:9], whole[9:20], whole[20:]),
                         ['{"type":"control","control":{"data":{"type":"hangup"}}}'])

    def test_a_long_message_uses_the_wider_length_field(self):
        """Offers and answers carry SDP and run well past 125 bytes, so
        the two-byte length is the normal case, not an edge one."""
        payload = b'{"sdp":"' + b"x" * 400 + b'"}'
        self.assertEqual(read(frame(payload)), [payload.decode()])

    def test_a_retransmitted_segment_is_not_read_twice(self):
        packet = frame(b'{"a":1}')
        self.assertEqual(read(packet, packet, seqs=[7, 7]), ['{"a":1}'])

    def test_control_frames_carry_no_message(self):
        """Ping, pong and close keep a connection alive and say nothing
        about what the participants did."""
        for opcode in (0x8, 0x9, 0xA):
            with self.subTest(opcode=opcode):
                self.assertEqual(read(frame(b"\x03\xe8", opcode=opcode)), [])


class HandshakeTest(unittest.TestCase):
    """A capture that starts before the connection does begins with the
    HTTP upgrade, and one that starts in the middle does not. Both have
    to be read."""

    def test_the_upgrade_request_is_skipped(self):
        upgrade = (b"GET /spreed HTTP/1.1\r\nHost: localhost\r\n"
                   b"Upgrade: websocket\r\n\r\n")
        self.assertEqual(read(upgrade + frame(b'{"a":1}', mask=b"\x01\x02\x03\x04")),
                         ['{"a":1}'])

    def test_the_upgrade_response_is_skipped(self):
        upgrade = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n"
        self.assertEqual(read(upgrade + frame(b'{"a":1}')), ['{"a":1}'])

    def test_a_capture_started_mid_connection_still_reads_frames(self):
        """No handshake to find, and waiting for one would swallow the
        whole stream."""
        self.assertEqual(read(frame(b'{"a":1}'), frame(b'{"b":2}')),
                         ['{"a":1}', '{"b":2}'])


class DescribeTest(unittest.TestCase):
    """The summary line, which is what makes a busy room's capture
    readable at all."""

    def test_a_hangup_is_named_with_who_it_was_for(self):
        line = ws_dump.describe({
            "type": "control",
            "control": {"recipient": {"type": "session", "sessionid": "phone-abc"},
                        "data": {"type": "hangup"}}})
        self.assertIn("control/hangup", line)
        self.assertIn("phone-abc", line)

    def test_an_internal_message_is_named_by_its_own_type(self):
        self.assertEqual(
            ws_dump.describe({"type": "internal", "internal": {"type": "addsession"}}),
            "internal/addsession")

    def test_a_message_without_data_is_still_described(self):
        self.assertEqual(ws_dump.describe({"type": "control", "control": {}}),
                         "control/? -> ")


if __name__ == "__main__":
    unittest.main()
