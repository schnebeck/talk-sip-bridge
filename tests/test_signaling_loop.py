"""Keeping the two signaling connections alive.

These are the loops everything else hangs off. A connection that
quietly stops is this bridge's worst failure mode, because nothing about
it is visible from the phone side: the line stays registered, calls are
answered, audio is negotiated, and none of it reaches Talk.

So the promises are small and absolute. A message that cannot be handled
costs only itself. A loop that ends is started again. Neither connection
can take the other down.

No sockets: the loops are handed something that behaves like a
websocket, and asked what they do with it.
"""
import asyncio
import json
import unittest

from tests.support import needs_media_stack

try:
    import talk_client
except ImportError:  # no media stack; every test here is skipped
    talk_client = None


class FakeSocket:
    """Yields the given messages, then ends the way a closed connection
    does."""

    def __init__(self, *messages):
        self.messages = [json.dumps(m) if isinstance(m, dict) else m for m in messages]

    def __aiter__(self):
        async def messages():
            for message in self.messages:
                yield message
        return messages()


def client():
    return talk_client.TalkClient(call_manager=None)


def read(socket, handle):
    return asyncio.new_event_loop().run_until_complete(
        client()._read("room", socket, handle))


@needs_media_stack
class OneBadMessageTest(unittest.TestCase):
    """A message that cannot be handled must not reach the connection.
    Letting it through reconnects, and a reconnect during a call takes
    that call's signaling with it."""

    def test_a_handler_that_raises_does_not_stop_the_reading(self):
        seen = []

        async def handle(message):
            seen.append(message["n"])
            if message["n"] == 2:
                raise RuntimeError("something in a handler")

        read(FakeSocket({"n": 1}, {"n": 2}, {"n": 3}), handle)
        self.assertEqual(seen, [1, 2, 3], "the messages after the bad one were lost")

    def test_a_message_that_is_not_json_costs_only_itself(self):
        seen = []

        async def handle(message):
            seen.append(message)

        read(FakeSocket('{"n": 1}', "not json at all", '{"n": 3}'), handle)
        self.assertEqual(seen, [{"n": 1}, {"n": 3}])

    def test_every_message_is_offered_to_the_handler(self):
        seen = []

        async def handle(message):
            seen.append(message)

        read(FakeSocket({"a": 1}, {"b": 2}), handle)
        self.assertEqual(len(seen), 2)


@needs_media_stack
class SupervisorTest(unittest.TestCase):
    """`_serve` loops forever, so nothing should ever reach the
    supervisor - which is exactly why it has to hold when something
    does."""

    def run_supervisor(self, serve, rounds=3):
        """Runs the supervisor until `serve` has been called `rounds`
        times, with the reconnect wait taken out of the way."""
        c = client()
        calls = []
        done = asyncio.Event()

        async def counted():
            calls.append(len(calls))
            if len(calls) >= rounds:
                done.set()
            return await serve()

        async def scenario():
            task = asyncio.ensure_future(c._supervise("room", counted))
            try:
                await asyncio.wait_for(done.wait(), timeout=5)
            finally:
                task.cancel()

        loop = asyncio.new_event_loop()
        original = talk_client.config.sip_response_timeout
        talk_client.config.sip_response_timeout = 0
        try:
            loop.run_until_complete(scenario())
        finally:
            talk_client.config.sip_response_timeout = original
            loop.close()
        return calls

    def test_a_loop_that_raises_is_started_again(self):
        async def falls_over():
            raise RuntimeError("the connection loop fell over")

        self.assertEqual(len(self.run_supervisor(falls_over)), 3)

    def test_a_loop_that_simply_returns_is_started_again(self):
        """Returning is as bad as raising: the connection is gone either
        way, and the phone side carries on regardless."""
        async def returns():
            return None

        self.assertEqual(len(self.run_supervisor(returns)), 3)


if __name__ == "__main__":
    unittest.main()
