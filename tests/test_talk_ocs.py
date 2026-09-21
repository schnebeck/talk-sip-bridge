# talk-sip-bridge - tests/test_talk_ocs.py
# The OCS call sequence, with nothing on the other end.
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

"""The OCS call sequence, with nothing on the other end.

No network: a fake opener records every request the module would send, so
the order and shape of the sequence can be asserted. The order is not
cosmetic - joining the call before establishing a room session is rejected
with 404, which is how this sequence came to look the way it does.
"""
import io
import json
import unittest
import urllib.error
from unittest import mock

from tests.support import env  # noqa: F401  (imported first: sets the environment talk_ocs needs)
import talk_ocs

ROOM = "room-token"
USER = "lobby"
PASSWORD = "app-password"
API = "/ocs/v2.php/apps/spreed/api/v4"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class RecordingOpener:
    """Stands in for urllib's opener: records requests, replies from a
    scripted list, and can be told to fail at a given step."""

    def __init__(self, replies=None, fail_at=None, error=None):
        self.requests = []
        self.replies = list(replies or [])
        self.fail_at = fail_at
        self.error = error or urllib.error.URLError("boom")

    def open(self, req, timeout=None):
        self.requests.append({
            "method": req.get_method(),
            "url": req.full_url,
            "body": json.loads(req.data) if req.data else None,
            "headers": {k.lower(): v for k, v in req.header_items()},
            "timeout": timeout,
        })
        if self.fail_at is not None and len(self.requests) == self.fail_at:
            raise self.error
        payload = self.replies.pop(0) if self.replies else {"ocs": {"data": {}}}
        return FakeResponse(json.dumps(payload).encode())

    @property
    def paths(self):
        return [r["url"].split("/ocs/v2.php/apps/spreed/api/v4")[-1] for r in self.requests]

    @property
    def calls(self):
        return [(r["method"], p) for r, p in zip(self.requests, self.paths, strict=True)]


def join_reply(session_id="session-1"):
    return {"ocs": {"data": {"sessionId": session_id}}}


def participants_reply(*attendees):
    return {"ocs": {"data": list(attendees)}}


def attendee(actor_id, attendee_id, actor_type="users"):
    return {"actorType": actor_type, "actorId": actor_id, "attendeeId": attendee_id}


class StartRingTest(unittest.TestCase):
    def ring(self, opener):
        with env(), mock.patch("urllib.request.build_opener", return_value=opener):
            return talk_ocs.start_ring(ROOM, USER, PASSWORD)

    def test_the_room_session_is_established_before_the_call_is_joined(self):
        """Reversing these two gets a 404 from Talk's RequireParticipant
        check - confirmed live."""
        opener = RecordingOpener([join_reply(), {}, participants_reply()])
        self.ring(opener)
        self.assertEqual(opener.calls[0], ("POST", f"/room/{ROOM}/participants/active"))
        self.assertEqual(opener.calls[1], ("POST", f"/call/{ROOM}"))

    def test_joining_the_call_announces_being_in_it(self):
        opener = RecordingOpener([join_reply(), {}, participants_reply()])
        self.ring(opener)
        self.assertEqual(opener.requests[1]["body"], {"flags": 1})

    def test_the_room_session_id_is_returned_so_it_can_be_excluded_later(self):
        """The signaling server broadcasts this session joining the call to
        everyone including the bridge, which must not mistake its own ring
        for somebody answering."""
        opener = RecordingOpener([join_reply("ring-session"), {}, participants_reply()])
        _, session_id = self.ring(opener)
        self.assertEqual(session_id, "ring-session")

    def test_every_request_carries_the_ocs_header_and_credentials(self):
        opener = RecordingOpener([join_reply(), {}, participants_reply()])
        self.ring(opener)
        for request in opener.requests:
            self.assertEqual(request["headers"].get("Ocs-apirequest".lower()), "true")
            self.assertTrue(request["headers"].get("authorization", "").startswith("Basic "))

    def test_real_users_are_rung_individually_except_the_ringing_account(self):
        """Joining the call alone leaves a stale client painting an
        incoming-call screen that does nothing, so each attendee is rung."""
        opener = RecordingOpener([
            join_reply(), {},
            participants_reply(attendee("user-a", 11), attendee(USER, 12),
                               attendee("guest", 13, actor_type="guests")),
            {},
        ])
        self.ring(opener)
        self.assertIn(("POST", f"/call/{ROOM}/ring/11"), opener.calls)
        self.assertNotIn(("POST", f"/call/{ROOM}/ring/12"), opener.calls)  # the ringing account itself
        self.assertNotIn(("POST", f"/call/{ROOM}/ring/13"), opener.calls)  # not a user

    def test_a_failure_to_ring_one_attendee_does_not_undo_the_call(self):
        """The join-driven ring stays in place regardless."""
        opener = RecordingOpener(
            [join_reply(), {}, participants_reply(attendee("user-a", 11))],
            fail_at=4, error=urllib.error.HTTPError("u", 400, "Bad", {}, io.BytesIO(b"dnd")))
        result, session_id = self.ring(opener)
        self.assertIsNotNone(result)
        self.assertEqual(session_id, "session-1")

    def test_an_unreachable_server_is_reported_rather_than_raised(self):
        """A call that cannot ring must not take down the message loop."""
        opener = RecordingOpener(fail_at=1)
        self.assertEqual(self.ring(opener), (None, None))

    def test_no_credentials_means_no_requests_at_all(self):
        opener = RecordingOpener()
        with env(), mock.patch("urllib.request.build_opener", return_value=opener):
            self.assertEqual(talk_ocs.start_ring(ROOM, "", ""), (None, None))
        self.assertEqual(opener.requests, [])


class StopRingTest(unittest.TestCase):
    def test_the_call_is_left_before_the_room(self):
        opener = RecordingOpener()
        with env():
            talk_ocs.stop_ring(opener, ROOM, USER, PASSWORD)
        self.assertEqual(opener.calls, [
            ("DELETE", f"/call/{ROOM}"),
            ("DELETE", f"/room/{ROOM}/participants/active"),
        ])

    def test_leaving_affects_only_this_participant(self):
        opener = RecordingOpener()
        with env():
            talk_ocs.stop_ring(opener, ROOM, USER, PASSWORD)
        self.assertEqual(opener.requests[0]["body"], {"all": False})


def room_reply(has_call):
    return {"ocs": {"data": {"token": ROOM, "hasCall": has_call}}}


class EndRoomCallTest(unittest.TestCase):
    def end(self, opener):
        with env(), mock.patch("urllib.request.build_opener", return_value=opener):
            talk_ocs.end_room_call(ROOM, USER, PASSWORD)

    def test_the_call_is_ended_for_everyone(self):
        """Left running, the person who answered sits in a call whose phone
        is gone, and Talk plays its waiting tone at them."""
        opener = RecordingOpener(replies=[{}, {}, room_reply(False), {}])
        self.end(opener)
        self.assertEqual(opener.calls, [
            ("POST", f"/room/{ROOM}/participants/active"),
            ("DELETE", f"/call/{ROOM}"),
            ("GET", f"/room/{ROOM}"),
            ("DELETE", f"/room/{ROOM}/participants/active"),
        ])

    def test_all_travels_in_the_body_where_talk_reads_it(self):
        """As a query parameter it is ignored, and ignoring it means only
        this participant leaves - which looks exactly like success."""
        opener = RecordingOpener(replies=[{}, {}, room_reply(False), {}])
        self.end(opener)
        leave = opener.requests[1]
        self.assertEqual(leave["body"], {"all": True})
        self.assertNotIn("all=", leave["url"])

    def test_a_call_that_is_still_running_is_reported_as_such(self):
        """Talk answers 200 whether it ended the call or only left it, so
        the room is asked afterwards. Without moderator rights this is the
        normal outcome, and it has to be visible."""
        opener = RecordingOpener(replies=[{}, {}, room_reply(True), {}])
        with mock.patch("builtins.print") as printed:
            self.end(opener)
        said = " ".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertIn("still running", said)
        self.assertIn("moderator", said)

    def test_the_session_is_left_even_when_the_call_could_not_be_ended(self):
        """Staying joined would leave the bridge account in the room."""
        opener = RecordingOpener(replies=[{}, {}, room_reply(True), {}])
        self.end(opener)
        self.assertEqual(opener.calls[-1], ("DELETE", f"/room/{ROOM}/participants/active"))

    def test_a_room_that_does_not_say_is_not_reported_as_a_failure(self):
        opener = RecordingOpener(replies=[{}, {}, {"ocs": {"data": {}}}, {}])
        with mock.patch("builtins.print") as printed:
            self.end(opener)
        said = " ".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertIn("Ended the call", said)

    def test_missing_moderator_rights_are_survived(self):
        """Older servers refuse with 403 instead of falling through."""
        opener = RecordingOpener(
            fail_at=2, error=urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b"no")))
        with env(), mock.patch("urllib.request.build_opener", return_value=opener):
            talk_ocs.end_room_call(ROOM, USER, PASSWORD)  # must not raise


class RequestTest(unittest.TestCase):
    def test_a_body_makes_it_a_json_request(self):
        opener = RecordingOpener()
        talk_ocs.request(opener, "https://nc.example", "Basic x", "POST", "/p", {"flags": 1})
        self.assertEqual(opener.requests[0]["headers"].get("content-type"), "application/json")
        self.assertEqual(opener.requests[0]["body"], {"flags": 1})

    def test_without_a_body_no_content_type_is_sent(self):
        opener = RecordingOpener()
        talk_ocs.request(opener, "https://nc.example", "Basic x", "DELETE", "/p")
        self.assertIsNone(opener.requests[0]["headers"].get("content-type"))
        self.assertIsNone(opener.requests[0]["body"])

    def test_requests_do_not_wait_forever(self):
        """A caller is waiting on these; an unbounded request would hold the
        SIP side past the point where anyone is still listening."""
        opener = RecordingOpener()
        talk_ocs.request(opener, "https://nc.example", "Basic x", "GET", "/p")
        self.assertIsNotNone(opener.requests[0]["timeout"])


if __name__ == "__main__":
    unittest.main()
