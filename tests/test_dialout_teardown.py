# talk-sip-bridge - tests/test_dialout_teardown.py
# A dialout that ends without ever connecting.
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

"""A dialout that ends without ever connecting.

Separate from test_dialout.py because it is a different subject: that
file is about reading a request and answering it, this one about what
has to be taken back afterwards. Refused, unanswered, or hung up while
it rang - all three reach the bridge through the same single callback,
and nothing else will clean up after them.
"""
import asyncio
import unittest

from tests.support import StubLine, needs_media_stack
from tests.test_dialout import FakeCallManager, client_for, request, settle

try:
    import talk_client  # noqa: F401  (the import is what skips this without the media stack)
except ImportError:
    talk_client = None


@needs_media_stack
class CallThatWasNeverAnsweredTest(unittest.TestCase):
    """What has to happen when a dialout ends without ever connecting -
    refused, unanswered, or hung up while it rang.

    Nothing else will do it: on_call_ended belongs to calls that were
    established, so a call that never was reaches only on_call_failed.
    While that did nothing but report the status, every such call left
    its entry behind - and an entry is what says a call is still worth
    working on, so the bridge went on believing in a call that had been
    over for minutes."""

    def failed(self, reason="SIP/2.0 603 Decline"):
        manager = FakeCallManager(StubLine(dialout_strip_prefix="+4930"))
        client = client_for(manager)

        async def scenario():
            # The SIP side reports this from its own worker thread, so
            # the client needs the loop to hand the work to.
            client.loop = asyncio.get_running_loop()
            await client.dialout.requested(request("+4930622"))
            await settle()
            client.ws.sent.clear()
            client.dialout_ws.sent.clear()
            client.on_call_failed(call_id="call-1", reason=reason)
            await settle()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(scenario())
        finally:
            loop.close()
        return client

    def test_the_call_is_forgotten(self):
        """Its entry is what says a call is still worth working on."""
        self.assertEqual(self.failed()._call_sessions, {})

    def test_the_status_is_one_talk_clears_the_ringing_on(self):
        client = self.failed()
        status = [m["internal"]["dialout"] for m in client.dialout_ws.sent
                  if m.get("internal", {}).get("type") == "dialout"][0]
        self.assertEqual(status["status"]["status"], "rejected")
        self.assertEqual(status["status"]["callid"], "call-1")

    def test_the_room_is_left_again(self):
        """Nothing expires the membership, and a room that still holds a
        session is a room Nextcloud counts somebody in - which stops it
        noticing when the conversation's call empties."""
        rooms = [m["room"]["roomid"] for m in self.failed().ws.sent
                 if m.get("type") == "room"]
        self.assertEqual(rooms[-1], "", "the bridge stayed in the room")

    def test_the_dialout_connection_is_kept(self):
        """It is the one thing that must survive every call: only a
        fresh hello makes a connection dialout-eligible, so throwing it
        away costs the next call."""
        client = self.failed()
        self.assertIsNotNone(client.dialout_ws)

    def test_an_unanswered_call_is_torn_down_like_a_refused_one(self):
        """Nobody picking up leaves exactly as much behind."""
        client = self.failed(reason="timeout")
        self.assertEqual(client._call_sessions, {})


if __name__ == "__main__":
    unittest.main()
