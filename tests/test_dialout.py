"""Handling a dialout request from Talk, with nothing on either side.

The outbound call itself is proven elsewhere - `CallManager.dial` places
real INVITEs through the real gateway in tests/hardware. What is left is
the part between Talk and that call: reading the request, mapping the
number to the gateway's dial plan, and answering in the shape the
signaling server accepts. A reply in the wrong shape is *silently
ignored*, which is why its exact form is asserted here rather than
eyeballed.
"""
import asyncio
import json
import unittest

from tests.support import StubLine, needs_media_stack

try:
    import talk_client
    from call import DIALOUT
except ImportError:  # no media stack; every test here is skipped
    talk_client = None


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def request(number: str, roomid: str = "room-token", request_id: str = "req-1") -> dict:
    """A dialout request in the shape the signaling server sends."""
    return {
        "id": request_id,
        "type": "internal",
        "internal": {
            "type": "dialout",
            "dialout": {
                "roomid": roomid,
                "backend": "https://nextcloud.example",
                "request": {"number": number, "options": {}},
            },
        },
    }


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    @property
    def replies(self):
        return [m["internal"]["dialout"] for m in self.sent
                if m.get("type") == "internal" and m.get("internal", {}).get("type") == "dialout"]


class FakeCallManager:
    """Stands in for the SIP side: records what it was asked to dial."""

    def __init__(self, line=None, result=None):
        self.line = line or StubLine()
        self.dialled = []
        self.result = result or {"started": True, "call_id": "call-1"}

    def dial(self, number):
        self.dialled.append(number)
        return self.result


def client_for(manager):
    client = talk_client.TalkClient(manager)
    client.ws = FakeWebSocket()
    client.own_sessionid = "own-session"
    return client


@needs_media_stack
class NumberMappingTest(unittest.TestCase):
    """Nextcloud validates the number with libphonenumber before this
    bridge ever sees it, so a bare extension cannot be entered in Talk. A
    real-looking prefix is stripped back off here to reach the gateway's
    own dial plan."""

    def dial(self, number, **line_options):
        manager = FakeCallManager(StubLine(**line_options))
        run(client_for(manager)._handle_dialout(request(number)))
        return manager.dialled

    def test_the_configured_prefix_is_stripped(self):
        self.assertEqual(self.dial("+4930622", dialout_strip_prefix="+4930"), ["622"])

    def test_a_number_without_that_prefix_is_passed_through(self):
        self.assertEqual(self.dial("622", dialout_strip_prefix="+4930"), ["622"])

    def test_the_prefix_is_only_stripped_from_the_front(self):
        self.assertEqual(self.dial("555+4930", dialout_strip_prefix="+4930"), ["555+4930"])

    def test_without_a_configured_prefix_nothing_is_stripped(self):
        self.assertEqual(self.dial("+4930622"), ["+4930622"])


@needs_media_stack
class ReplyShapeTest(unittest.TestCase):
    """The signaling server ignores a reply that is not wrapped the way the
    request was - no error, no retry, the call simply never happens."""

    def reply_for(self, result=None, number="+4930622"):
        manager = FakeCallManager(StubLine(dialout_strip_prefix="+4930"), result=result)
        client = client_for(manager)
        run(client._handle_dialout(request(number)))
        return client.ws.sent[0], client

    def test_the_reply_echoes_the_request_id(self):
        """Without it the server cannot match the answer to its request."""
        sent, _ = self.reply_for()
        self.assertEqual(sent["id"], "req-1")

    def test_the_reply_keeps_the_internal_wrapper(self):
        sent, _ = self.reply_for()
        self.assertEqual(sent["type"], "internal")
        self.assertEqual(sent["internal"]["type"], "dialout")

    def test_an_accepted_call_reports_its_own_call_id(self):
        sent, _ = self.reply_for()
        dialout = sent["internal"]["dialout"]
        self.assertEqual(dialout["type"], "status")
        self.assertEqual(dialout["roomid"], "room-token")
        self.assertEqual(dialout["status"], {"callid": "call-1", "status": "accepted"})

    def test_a_refused_number_is_reported_as_an_error(self):
        sent, _ = self.reply_for(result={"error": "Number not in allowlist"})
        dialout = sent["internal"]["dialout"]
        self.assertEqual(dialout["type"], "error")
        self.assertEqual(dialout["error"]["code"], "call_failed")
        self.assertIn("allowlist", dialout["error"]["message"])
        self.assertNotIn("status", dialout)

    def test_exactly_one_reply_is_sent(self):
        """One reply, whatever else goes out alongside it - a second one
        for the same request is a protocol error."""
        _, client = self.reply_for()
        self.assertEqual(len(client.ws.replies), 1)

    def test_the_room_is_joined_while_the_call_is_still_ringing(self):
        """Ending a dialout in Talk before anyone answers is announced in
        the room, and a bridge that is not in it hears nothing: measured,
        a phone went on ringing for the rest of the outbound timeout
        because joining only happened once a call connected."""
        _, client = self.reply_for()
        joins = [m for m in client.ws.sent if m.get("type") == "room"]
        self.assertEqual(len(joins), 1, "the room was not joined while it rang")
        self.assertEqual(joins[0]["room"]["roomid"], "room-token")


@needs_media_stack
class CallBookkeepingTest(unittest.TestCase):
    def accepted_call(self, roomid="room-token"):
        manager = FakeCallManager(StubLine(dialout_strip_prefix="+4930"))
        client = client_for(manager)
        run(client._handle_dialout(request("+4930622", roomid=roomid)))
        return client

    def test_the_call_is_remembered_as_a_dialout(self):
        """Its kind decides whether progress is reported back to Talk when
        it ends - an inbound call has nobody to report to."""
        client = self.accepted_call()
        call = client._call_sessions["call-1"]
        self.assertEqual(call.kind, DIALOUT)
        self.assertEqual(call.number, "622")

    def test_the_room_comes_from_the_request(self):
        """There is no other way to learn it, and no default worth falling
        back to - a dialout placed into the wrong room is worse than none."""
        client = self.accepted_call(roomid="another-room")
        self.assertEqual(client._call_sessions["call-1"].roomid, "another-room")

    def test_a_refused_call_is_not_remembered(self):
        manager = FakeCallManager(StubLine(), result={"error": "no"})
        client = client_for(manager)
        run(client._handle_dialout(request("622")))
        self.assertEqual(client._call_sessions, {})


if __name__ == "__main__":
    unittest.main()
