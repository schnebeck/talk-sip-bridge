"""Ending a call from this side.

The direction that had never been exercised: in every test until now the
caller hung up, so the BYE arrived here. A hangup that starts in Talk has
to go the other way, and it has to name the dialog correctly or the far
end answers "481 Call/Transaction Does Not Exist" and keeps the call
running - measured on a real gateway, with a handset left in a call for
five minutes after Talk was done with it.
"""
import unittest

from tests.support import StubLine, needs_media_stack

try:
    from sip_call import CallManager, _with_tag
except ImportError:      # no media stack
    CallManager = None

CALLER = '"FritzFon" <sip:**611@fritz.box>;tag=CALLERTAG'
US = "<sip:sip-phone@192.0.2.1:5070>"
OUR_TAG = "bridge12ab34"
CONTACT = "<sip:C5892C7CB51F2F5F3832DAEAB3B8BF9@192.0.2.1:44528;transport=tcp>"


class FakeTransport:
    def __init__(self):
        self.sent = []

    def send(self, data, addr=None):
        self.sent.append(data.decode())

    def open_waiter(self, call_id):
        import queue
        return queue.Queue()

    def close_waiter(self, call_id):
        pass


@needs_media_stack
class TagTest(unittest.TestCase):
    def test_a_tag_is_added_once(self):
        self.assertEqual(_with_tag("<sip:a@b>", "x"), "<sip:a@b>;tag=x")
        self.assertEqual(_with_tag("<sip:a@b>;tag=y", "x"), "<sip:a@b>;tag=y")

    def test_nothing_to_add_to_is_left_alone(self):
        self.assertEqual(_with_tag("", "x"), "")
        self.assertEqual(_with_tag("<sip:a@b>", ""), "<sip:a@b>")


@needs_media_stack
class InboundHangupTest(unittest.TestCase):
    """What the bridge sends when Talk ends a call that came in."""

    def setUp(self):
        self.manager = CallManager(StubLine())
        self.manager.transport = FakeTransport()
        self.manager.call = {
            "call_id": "abc@gateway", "direction": "inbound", "status": "connected",
            "to_tag": OUR_TAG, "bye_timer": None, "rtp": None,
            "remote_addr": ("192.0.2.1", 5060),
            "headers": {"from": CALLER, "to": US, "contact": CONTACT,
                        "call-id": "abc@gateway", "cseq": "1 INVITE", "via": []},
        }
        self.manager.hangup()
        self.bye = self.manager.transport.sent[0]

    def test_it_is_a_bye_for_this_call(self):
        self.assertTrue(self.bye.startswith("BYE "))
        self.assertIn("Call-ID: abc@gateway", self.bye)

    def test_it_goes_to_the_dialog_contact(self):
        """Not to the registrar and not to the number originally dialled:
        the peer's Contact is the only address that identifies this
        dialog's remote end."""
        self.assertTrue(self.bye.startswith(
            "BYE sip:C5892C7CB51F2F5F3832DAEAB3B8BF9@192.0.2.1:44528"), self.bye.split("\r\n")[0])

    def test_our_own_tag_is_in_the_from_header(self):
        """The one that was missing. Our tag was invented when the call
        was answered and appeared only in the responses; a BYE without it
        names no dialog the far end knows."""
        from_line = next(l for l in self.bye.split("\r\n") if l.startswith("From:"))
        self.assertIn(f";tag={OUR_TAG}", from_line)

    def test_their_tag_is_in_the_to_header(self):
        to_line = next(l for l in self.bye.split("\r\n") if l.startswith("To:"))
        self.assertIn(";tag=CALLERTAG", to_line)

    def test_the_two_are_not_swapped(self):
        """From is us, To is them - the other way round names a dialog
        that exists nowhere."""
        from_line = next(l for l in self.bye.split("\r\n") if l.startswith("From:"))
        to_line = next(l for l in self.bye.split("\r\n") if l.startswith("To:"))
        self.assertIn("sip-phone", from_line)
        self.assertIn("**611", to_line)


@needs_media_stack
class OutboundHangupTest(unittest.TestCase):
    """Ending a call this bridge placed, before and after it is
    answered. The two are different requests, and sending the wrong one
    leaves the far end ringing: measured, a phone rang for another half
    minute after the call was hung up in Talk, because a BYE for a
    dialog that does not exist yet ends nothing."""

    def setUp(self):
        from sip_call import _OutboundAttempt

        self.manager = CallManager(StubLine())
        self.manager.transport = FakeTransport()
        self.attempt = _OutboundAttempt("**611", "out@bridge", "fromtag", "v=0")
        self.manager.call = {
            "call_id": "out@bridge", "direction": "outbound", "status": "dialing",
            "number": "**611", "from_tag": "fromtag", "to_tag": None,
            "bye_timer": None, "rtp": None, "attempt": self.attempt,
        }

    def sent(self):
        return self.manager.transport.sent[-1] if self.manager.transport.sent else ""

    def test_a_ringing_call_is_cancelled(self):
        self.manager.hangup()
        self.assertTrue(self.sent().startswith("CANCEL "), self.sent().split("\r\n")[0])

    def test_the_cancel_repeats_the_invites_branch(self):
        """A CANCEL that names another branch cancels nothing."""
        self.manager.hangup()
        via = next(l for l in self.sent().split("\r\n") if l.startswith("Via:"))
        self.assertIn(self.attempt.branch, via)
        self.assertIn(f"CSeq: {self.attempt.cseq} CANCEL", self.sent())

    def test_an_answered_call_is_ended_with_bye(self):
        self.manager.call["status"] = "connected"
        self.manager.call["to_tag"] = "theirtag"
        self.manager.call["remote_contact"] = "sip:opaque@192.0.2.1:5060"
        self.manager.hangup()
        self.assertTrue(self.sent().startswith("BYE "), self.sent().split("\r\n")[0])

    def test_a_ringing_call_without_an_attempt_sends_nothing_wrong(self):
        """Nothing to cancel is better than a BYE that ends nothing."""
        self.manager.call.pop("attempt")
        self.manager.hangup()
        self.assertFalse(self.sent().startswith("BYE "))


@needs_media_stack
class HangupMatrixTest(unittest.TestCase):
    """Every state a call can be in, and the request that ends it.

    Written as a table rather than as one case per incident: the states
    are known in advance, and asking "what does this send here?" for
    each of them is the kind of question that does not need a broken
    call to be asked. SIP answers it differently in each row, and
    getting a row wrong leaves the far end hanging - measured twice.
    """

    def manager_for(self, direction: str, status: str):
        from sip_call import _OutboundAttempt

        manager = CallManager(StubLine())
        manager.transport = FakeTransport()
        if direction == "inbound":
            manager.call = {
                "call_id": "in@gateway", "direction": "inbound", "status": status,
                "to_tag": OUR_TAG, "bye_timer": None, "rtp": None,
                "remote_addr": ("192.0.2.1", 5060),
                "headers": {"from": CALLER, "to": US, "contact": CONTACT,
                            "call-id": "in@gateway", "cseq": "1 INVITE", "via": []},
            }
        else:
            manager.call = {
                "call_id": "out@bridge", "direction": "outbound", "status": status,
                "number": "**611", "from_tag": "fromtag", "to_tag": "theirtag",
                "bye_timer": None, "rtp": None, "remote_contact": "sip:opaque@192.0.2.1",
                "attempt": _OutboundAttempt("**611", "out@bridge", "fromtag", "v=0"),
            }
        return manager

    def first_line(self, manager):
        sent = manager.transport.sent
        return sent[-1].split("\r\n")[0] if sent else ""

    def test_what_each_state_sends(self):
        """A dialog that exists is ended (BYE). One that does not is
        withdrawn (CANCEL) or refused (a final response) - never
        ended, because there is nothing to end and the far end goes on
        ringing."""
        expected = {
            ("outbound", "dialing"): "CANCEL",      # no dialog yet: withdraw the INVITE
            ("outbound", "connected"): "BYE",       # a dialog: end it
            ("inbound", "connected"): "BYE",
        }
        for (direction, status), request in expected.items():
            with self.subTest(direction=direction, status=status):
                manager = self.manager_for(direction, status)
                manager.hangup()
                self.assertTrue(self.first_line(manager).startswith(request),
                                f"{direction}/{status} sent {self.first_line(manager)!r}")

    def test_a_ringing_inbound_call_is_refused_not_ended(self):
        """The row that has no incident behind it. A call this bridge
        has only answered "180 Ringing" to has no dialog either: BYE is
        answered "481 Call/Transaction Does Not Exist" and the caller
        keeps hearing ringback until the gateway gives up."""
        manager = self.manager_for("inbound", "ringing")
        manager.hangup()
        self.assertFalse(self.first_line(manager).startswith("BYE"),
                         "a call nobody answered was ended with BYE")
        self.assertTrue(self.first_line(manager).startswith("SIP/2.0 6"),
                        f"expected a final response, got {self.first_line(manager)!r}")

    def test_hanging_up_twice_sends_nothing_the_second_time(self):
        manager = self.manager_for("outbound", "connected")
        manager.hangup()
        before = len(manager.transport.sent)
        manager.hangup()
        self.assertEqual(len(manager.transport.sent), before)


if __name__ == "__main__":
    unittest.main()
