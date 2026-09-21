# talk-sip-bridge - tests/test_resilience.py
# What has to survive a fault in the middle of a call.
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

"""What has to survive a fault in the middle of a call.

Three loops carry this bridge, each in a thread of its own: the media
receiver, the registration keepalive, and the two signaling connections
(those are in test_signaling_loop.py). A thread that dies takes its job
with it and nothing else notices - the line stays registered, the call
stays up, the journal stays quiet, and only the person on the phone can
tell.

Each of them therefore has to cost a fault the fault and nothing more.
These tests inject one where the real world would: in a packet off the
wire, and in a registration refresh.

No sockets here either: the media session is built without its
constructor, which is the only part of it that binds anything.
"""
import threading
import unittest
from unittest import mock

from tests.support import StubLine, needs_media_stack

import sip_registrar
from sip_registrar import SipRegistrar

try:
    import rtp
except ImportError:  # no media stack; those tests skip themselves
    rtp = None


def media_session(packets=()):
    """An RtpSession with only the fields the receive loop touches, and
    a socket that hands out the given packets and then blocks."""
    session = rtp.RtpSession.__new__(rtp.RtpSession)
    session.stop_event = threading.Event()
    session._failed_packets = 0
    session._last_packet_error = 0.0
    remaining = list(packets)

    class Socket:
        def recvfrom(self, size):
            if remaining:
                return remaining.pop(0), ("127.0.0.1", 40002)
            session.stop_event.set()
            raise OSError("closed")

    session.sock = Socket()
    return session


@needs_media_stack
class MediaPacketTest(unittest.TestCase):
    """The receive thread is the only thing feeding the caller's audio
    into the bridge. If it dies the call is still up, still negotiated,
    still connected - and silent in that direction until somebody hangs
    up. What arrives here comes off the wire, so it is also where a
    malformed packet lands."""

    def test_a_packet_that_cannot_be_handled_does_not_end_the_loop(self):
        session = media_session([b"one", b"two", b"three"])
        seen = []

        def explode(self, data, addr):
            seen.append(data)
            raise ValueError("something in the decoder")

        with mock.patch.object(type(session), "_handle_packet", explode), \
                mock.patch("traceback.print_exc", lambda *a, **kw: None), \
                mock.patch("builtins.print"):
            session._recv_loop()
        self.assertEqual(seen, [b"one", b"two", b"three"],
                         "the loop stopped at the first bad packet")

    def test_a_working_packet_is_handled_as_before(self):
        session = media_session([b"one", b"two"])
        seen = []
        with mock.patch.object(type(session), "_handle_packet",
                               lambda self, data, addr: seen.append(data)):
            session._recv_loop()
        self.assertEqual(seen, [b"one", b"two"])

    def test_a_recurring_fault_is_reported_once_and_then_counted(self):
        """Fifty packets a second means one fault would otherwise be
        fifty journal lines a second, and the journal is where the next
        problem has to stay visible."""
        session = media_session()
        printed = []
        with mock.patch("builtins.print", lambda *a, **kw: printed.append(" ".join(map(str, a)))), \
                mock.patch("traceback.print_exc", lambda *a, **kw: None):
            for _ in range(200):
                session._packet_failed(ValueError("the same fault again"))
        self.assertEqual(len(printed), 1, f"reported {len(printed)} times")
        self.assertEqual(session._failed_packets, 200)

    def test_it_speaks_again_once_the_quiet_period_is_over(self):
        session = media_session()
        printed = []
        with mock.patch("builtins.print", lambda *a, **kw: printed.append(" ".join(map(str, a)))), \
                mock.patch("traceback.print_exc", lambda *a, **kw: None):
            session._packet_failed(ValueError("one"))
            session._last_packet_error -= rtp.PACKET_ERROR_QUIET + 1
            session._packet_failed(ValueError("two"))
        self.assertEqual(len(printed), 2)
        self.assertIn("2 so far this call", printed[1])


class RegistrationKeepaliveTest(unittest.TestCase):
    """A refresh that fails is normal and handled. A refresh that
    *raises* used to end this thread, and then nothing refreshed the
    registration again: the line stayed reachable until it expired and
    was silently gone after that, with a bridge that still looked
    healthy in every other way."""

    def run_loop(self, register, rounds=3):
        """Runs the keepalive until it has tried to refresh `rounds`
        times, with the refresh interval taken out of the way."""
        attempts = []
        done = threading.Event()

        def counted(expires):
            attempts.append(expires)
            if len(attempts) >= rounds:
                done.set()
            return register(expires)

        registrar = SipRegistrar(lambda: mock.Mock(), StubLine())
        registrar.wanted = True
        registrar.registered = True
        registrar._do_register = counted
        # Patched on the object sip_registrar is holding: the module bound
        # it at import, and reloading config gives a new one it never sees.
        with mock.patch.object(sip_registrar.config, "register_expires", 0), \
                mock.patch("traceback.print_exc", lambda *a, **kw: None):
            thread = threading.Thread(target=registrar._keepalive_loop, daemon=True)
            thread.start()
            finished = done.wait(timeout=5)
            registrar.stop_event.set()
            thread.join(timeout=2)
        self.assertTrue(finished, f"the loop stopped after {len(attempts)} refresh(es)")
        return registrar, attempts

    def raising(self, expires):
        raise RuntimeError("the socket went away mid-refresh")

    def test_a_refresh_that_raises_does_not_end_the_loop(self):
        _, attempts = self.run_loop(self.raising)
        self.assertGreaterEqual(len(attempts), 3)

    def test_the_line_is_marked_unregistered_when_a_refresh_raises(self):
        """Reporting "registered" while nothing refreshes it is the one
        state that must never be shown - it is the state in which the
        line disappears without warning."""
        registrar, _ = self.run_loop(self.raising)
        self.assertFalse(registrar.registered)
        self.assertIn("went away", registrar.last_error)

    def test_it_is_still_meant_to_be_registered_afterwards(self):
        """A fault must not read as "switched off", or recovery never
        happens."""
        registrar, _ = self.run_loop(self.raising)
        self.assertTrue(registrar.wanted)

    def test_a_refresh_that_only_fails_is_left_to_the_normal_path(self):
        registrar, attempts = self.run_loop(lambda expires: False)
        self.assertGreaterEqual(len(attempts), 3)
        self.assertFalse(registrar.registered)


if __name__ == "__main__":
    unittest.main()
