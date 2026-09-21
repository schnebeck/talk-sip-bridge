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


async def settle():
    """Waits for everything the handler started alongside itself.

    The name plate for a ringing call goes up in a task of its own, so
    that the "accepted" reply the server waits for is never held up
    behind it. A test that stopped when the handler returned would not
    see it at all.

    Work handed over from a worker thread does not even become a task
    until the loop has turned once, so this waits for quiet rather than
    for the tasks that happen to exist right now."""
    quiet = 0
    while quiet < 3:
        await asyncio.sleep(0)
        pending = [task for task in asyncio.all_tasks()
                   if task is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
            quiet = 0
        else:
            quiet += 1


def run(coro):
    async def scenario():
        result = await coro
        await settle()
        return result

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        loop.close()


# What Talk puts in a dialout request's options: the phone attendee it
# made for the number before asking for the call
# (BackendNotifier::dialOutToAttendee), plus options this bridge has no
# use for.
OPTIONS = {"attendeeId": 42, "actorType": "phones", "actorId": "abc123token"}


def request(number: str, roomid: str = "room-token", request_id: str = "req-1",
            options: dict = None) -> dict:
    """A dialout request in the shape the signaling server sends."""
    return {
        "id": request_id,
        "type": "internal",
        "internal": {
            "type": "dialout",
            "dialout": {
                "roomid": roomid,
                "backend": "https://nextcloud.example",
                "request": {"number": number,
                            "options": OPTIONS if options is None else options},
            },
        },
    }


class FakeWebSocket:
    """Records what was sent, and confirms a room join the way the
    server does - without that the bridge waits out its five-second
    join timeout on every call."""

    def __init__(self, client=None):
        self.sent = []
        self.client = client

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message.get("type") == "room" and self.client is not None:
            self.client._room_joined_event.set()

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
    """A client with both its connections faked.

    They are separate on purpose: the dialout connection carries the
    requests and their answers and never enters a room, the room
    connection does everything else. A message on the wrong one is the
    failure this split exists to prevent - the server matches a dialout
    reply against the session it asked."""
    client = talk_client.TalkClient(manager)
    client.ws = FakeWebSocket(client)     # the room connection
    client.dialout_ws = FakeWebSocket()   # the dialout connection
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
        run(client_for(manager).dialout.requested(request(number)))
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
        run(client.dialout.requested(request(number)))
        return client.dialout_ws.sent[0], client

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
        self.assertEqual(len(client.dialout_ws.replies), 1)

    def test_the_dialout_connection_never_enters_a_room(self):
        """An internal client that is in a room is no longer eligible
        for dialout requests, and only a fresh hello puts it back - so
        one room join on this connection costs every later dialout.
        Measured before the two connections existed: Nextcloud refused
        every attempt after the first with "the phone number could not
        be called"."""
        _, client = self.reply_for()
        joins = [m for m in client.dialout_ws.sent if m.get("type") == "room"]
        self.assertEqual(joins, [], "joining a room here disables dialout")

    def test_the_room_connection_goes_in_while_the_call_rings(self):
        """Ending the call in Talk reaches the room's sessions and
        nothing else, so being there before anyone answers is the only
        way a caller who gives up is heard."""
        _, client = self.reply_for()
        joins = [m["room"]["roomid"] for m in client.ws.sent if m.get("type") == "room"]
        self.assertEqual(joins, ["room-token"])

    def test_the_answer_goes_out_before_the_room_is_joined(self):
        """The server times the "accepted" out; waiting for a join
        confirmation first would miss it."""
        _, client = self.reply_for()
        self.assertTrue(client.dialout_ws.sent, "nothing answered the request")


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


@needs_media_stack
class CallBookkeepingTest(unittest.TestCase):
    def accepted_call(self, roomid="room-token"):
        manager = FakeCallManager(StubLine(dialout_strip_prefix="+4930"))
        client = client_for(manager)
        run(client.dialout.requested(request("+4930622", roomid=roomid)))
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
        run(client.dialout.requested(request("622")))
        self.assertEqual(client._call_sessions, {})


if __name__ == "__main__":
    unittest.main()
