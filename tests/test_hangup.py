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


if __name__ == "__main__":
    unittest.main()
