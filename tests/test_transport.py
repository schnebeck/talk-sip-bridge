"""Where a SIP message goes when it is sent over TCP.

A datagram is sent to an address and that is the end of it. A stream is
sent over a connection, and a connection can be gone by the time there is
something to send on it - which is why this has its own tests: the gateway
closes the connection an INVITE arrived on while a person is still
deciding whether to answer, and the 200 OK that follows has to reach the
gateway anyway.

No sockets: what is under test is the choice of where to write, so the
connections here are objects that record what they were handed.
"""
import threading
import time
import unittest

from tests.support import StubLine   # sets the environment config needs, before it is imported
import sip_transport
from sip_transport import KEEPALIVE, TcpSipTransport


class FakeConnection:
    """A socket as far as the transport is concerned."""

    def __init__(self, fails=False):
        self.fails = fails
        self.sent = []

    def sendall(self, data):
        if self.fails:
            raise OSError(9, "Bad file descriptor")
        self.sent.append(data)

    def close(self):
        pass


class Transport(TcpSipTransport):
    """The transport without its constructor, which would bind sockets.
    Only what send() reads is set up; the outgoing connection is handed
    over instead of dialled."""

    def __init__(self, line, outgoing):
        self.line = line
        self._stopping = threading.Event()
        self.call_manager = None
        self._out = None
        self._out_lock = threading.Lock()
        self._closed = False
        self._outgoing = outgoing
        self.connects = 0

    def _connect(self):
        self.connects += 1
        return self._outgoing

    def close(self):
        self._closed = True
        self._stopping.set()


class KeepaliveTest(unittest.TestCase):
    """Why a connection nobody writes to must not be allowed to die.

    A peer's Contact over TCP is the source port of its own connection to
    us. Once that connection is gone the address exists nowhere, and a BYE
    for that dialog can never be delivered - measured on a real gateway:
    it closed the connection 32 seconds into a call, the hangup went
    nowhere, and the caller's handset stayed in a call for five more
    minutes."""

    def setUp(self):
        self.transport = Transport(StubLine(sip_transport="tcp"), FakeConnection())
        self.interval = sip_transport.KEEPALIVE_INTERVAL
        sip_transport.KEEPALIVE_INTERVAL = 0.02

    def tearDown(self):
        sip_transport.KEEPALIVE_INTERVAL = self.interval
        self.transport.close()

    def test_a_connection_is_pinged(self):
        connection = FakeConnection()
        self.transport._start_keepalive(connection)
        deadline = time.time() + 2
        while time.time() < deadline and not connection.sent:
            time.sleep(0.01)
        self.assertEqual(connection.sent[0], KEEPALIVE)

    def test_pinging_stops_when_the_transport_closes(self):
        connection = FakeConnection()
        self.transport._start_keepalive(connection)
        deadline = time.time() + 2
        while time.time() < deadline and not connection.sent:
            time.sleep(0.01)
        self.transport.close()
        time.sleep(0.1)
        before = len(connection.sent)
        time.sleep(0.1)
        self.assertEqual(len(connection.sent), before)

    def test_a_dead_connection_ends_its_own_pinging(self):
        """Nothing else tells the ping thread the socket is gone."""
        connection = FakeConnection(fails=True)
        self.transport._start_keepalive(connection)
        time.sleep(0.1)
        self.assertEqual(connection.sent, [])


class AnsweringTest(unittest.TestCase):
    def setUp(self):
        self.line = StubLine(sip_transport="tcp")
        self.outgoing = FakeConnection()
        self.transport = Transport(self.line, self.outgoing)

    def test_a_reply_goes_back_on_the_connection_it_came_from(self):
        arrived_on = FakeConnection()
        self.transport.send(b"SIP/2.0 200 OK\r\n\r\n", arrived_on)
        self.assertEqual(arrived_on.sent, [b"SIP/2.0 200 OK\r\n\r\n"])
        self.assertEqual(self.outgoing.sent, [])
        self.assertEqual(self.transport.connects, 0)

    def test_a_reply_still_goes_out_when_that_connection_is_gone(self):
        """The failure this is here for: a gateway closes an idle
        connection while a call rings, and answering it must not depend on
        that connection still being open - the alternative is a call
        accepted by a person and never connected."""
        dead = FakeConnection(fails=True)
        self.transport.send(b"SIP/2.0 200 OK\r\n\r\n", dead)
        self.assertEqual(self.outgoing.sent, [b"SIP/2.0 200 OK\r\n\r\n"])
        self.assertEqual(self.transport.connects, 1)

    def test_a_request_of_our_own_uses_this_line_s_own_connection(self):
        self.transport.send(b"REGISTER sip:gateway SIP/2.0\r\n\r\n")
        self.assertEqual(self.outgoing.sent, [b"REGISTER sip:gateway SIP/2.0\r\n\r\n"])

    def test_that_connection_is_opened_once_and_then_reused(self):
        self.transport.send(b"one")
        self.transport.send(b"two")
        self.assertEqual(self.transport.connects, 1)
        self.assertEqual(self.outgoing.sent, [b"one", b"two"])


class ReconnectTest(unittest.TestCase):
    def test_a_connection_the_far_end_closed_is_replaced_and_the_message_still_sent(self):
        """Idle TCP connections are closed by the far end as a matter of
        course, and nothing says so until something is written."""
        line = StubLine(sip_transport="tcp")
        fresh = FakeConnection()
        transport = Transport(line, fresh)
        stale = FakeConnection(fails=True)
        transport._out = stale
        transport.send(b"REGISTER sip:gateway SIP/2.0\r\n\r\n")
        self.assertEqual(fresh.sent, [b"REGISTER sip:gateway SIP/2.0\r\n\r\n"])
        self.assertEqual(transport.connects, 1)

    def test_a_connection_that_cannot_be_replaced_raises_rather_than_dropping_the_message(self):
        """Silently swallowing it would leave a registration or a reply
        that never happened and nothing to see anywhere."""
        line = StubLine(sip_transport="tcp")
        transport = Transport(line, FakeConnection(fails=True))
        with self.assertRaises(OSError):
            transport.send(b"REGISTER sip:gateway SIP/2.0\r\n\r\n")


if __name__ == "__main__":
    unittest.main()
