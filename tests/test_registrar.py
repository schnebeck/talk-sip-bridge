"""Whether a line is registered, and whether it is meant to be.

These were one field once, and a failed refresh set it to false - which
the keepalive read as "switched off" and stopped. The line then stayed
unreachable until somebody noticed. Keeping the two apart is what makes
recovery possible at all, so it is worth pinning here; that it actually
recovers against a gateway that goes away is
tests/hardware/test_registration_recovery.py.
"""
import unittest

from tests.support import StubLine, env
import sip_registrar
from sip_registrar import SipRegistrar


class FakeTransport:
    """Answers, or does not, without a socket in sight."""

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.sent = []

    def send(self, data, addr=None):
        self.sent.append(data)

    def wait_response(self, call_id, timeout=None):
        return self.answers.pop(0) if self.answers else None

    def open_waiter(self, call_id):
        pass

    def close_waiter(self, call_id):
        pass


def ok_response():
    return "SIP/2.0 200 OK\r\nCall-ID: x\r\n\r\n"


def registrar_with(answers):
    transport = FakeTransport(answers)
    registrar = SipRegistrar(lambda: transport, StubLine())
    return registrar, transport


class TurnOnTest(unittest.TestCase):
    def tearDown(self):
        for registrar in getattr(self, "_started", []):
            registrar.turn_off(persist=False)

    def start(self, answers):
        registrar, transport = registrar_with(answers)
        self._started = getattr(self, "_started", []) + [registrar]
        with env():
            result = registrar.turn_on()
        return registrar, transport, result

    def test_a_gateway_that_answers_leaves_the_line_registered(self):
        registrar, _, result = self.start([ok_response()])
        self.assertTrue(result)
        self.assertTrue(registrar.registered)
        self.assertTrue(registrar.wanted)

    def test_a_gateway_that_does_not_answer_is_reported_but_not_given_up_on(self):
        """The caller learns the attempt failed; the line stays switched
        on, and the keepalive goes on trying."""
        registrar, _, result = self.start([])
        self.assertFalse(result)
        self.assertFalse(registrar.registered)
        self.assertTrue(registrar.wanted, "the line must stay wanted")
        self.assertTrue(registrar.keepalive_thread.is_alive(), "nothing would retry")

    def test_switching_on_twice_starts_one_keepalive(self):
        registrar, _, _ = self.start([ok_response()])
        first = registrar.keepalive_thread
        with env():
            registrar.turn_on()
        self.assertIs(registrar.keepalive_thread, first)


class TurnOffTest(unittest.TestCase):
    def test_switching_off_clears_both(self):
        registrar, _ = registrar_with([ok_response(), ok_response()])
        with env():
            registrar.turn_on()
            registrar.turn_off()
        self.assertFalse(registrar.registered)
        self.assertFalse(registrar.wanted)

    def test_switching_off_an_unreachable_line_still_stops_it(self):
        """Off has to work even while the gateway is unreachable - that is
        exactly when somebody reaches for the switch."""
        registrar, _ = registrar_with([])
        with env():
            registrar.turn_on()
            self.assertTrue(registrar.wanted)
            registrar.turn_off()
        self.assertFalse(registrar.wanted)
        self.assertTrue(registrar.stop_event.is_set())


class StatusTest(unittest.TestCase):
    def test_status_reports_what_is_true_now(self):
        registrar, _ = registrar_with([])
        with env():
            registrar.turn_on()
        status = registrar.status()
        self.assertFalse(status["registered"])
        self.assertIsNotNone(status["last_error"])
        registrar.turn_off(persist=False)

    def test_the_retry_interval_is_bounded(self):
        """Short enough that a line comes back promptly, long enough not to
        hammer a gateway that is down."""
        self.assertGreaterEqual(sip_registrar.RETRY_INTERVAL, 5)
        self.assertLessEqual(sip_registrar.RETRY_INTERVAL, 60)


if __name__ == "__main__":
    unittest.main()
