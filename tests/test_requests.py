"""What goes on the wire: the shape of every message this bridge builds."""
import unittest

import sip_requests
from sip_requests import (build_ack, build_bye, build_cancel, build_invite,
                          build_register, build_response)
from tests.support import (GATEWAY_HOST, LOCAL_IP, StubLine, body_of,
                           header_lines, header_value)

SDP = "v=0\r\nc=IN IP4 198.51.100.2\r\nm=audio 40000 RTP/AVP 9\r\n"
AUTH = ('Authorization: Digest username="sip-phone", realm="fritz.box", '
        'nonce="deadbeef", uri="sip:192.0.2.1", response="cafe", algorithm=MD5')


class WireFormatTest(unittest.TestCase):
    """Rules every message has to satisfy, whatever its method."""

    def all_messages(self):
        line = StubLine()
        common = dict(line=line, call_id="cid@host", branch="z9hG4bKbranch", cseq=1)
        yield "REGISTER", build_register(line, call_id="cid@host", tag="tagX",
                                         branch="z9hG4bKbranch", cseq=1, expires=600)
        yield "INVITE", build_invite(number="621", from_tag="tagX", sdp=SDP, **common)
        yield "ACK", build_ack(number="621", from_tag="tagX",
                               to_header="<sip:621@192.0.2.1>;tag=far", **common)
        yield "CANCEL", build_cancel(number="621", from_tag="tagX", **common)
        yield "BYE", build_bye(request_uri="sip:621@192.0.2.1", from_header="<sip:a@b>;tag=1",
                               to_header="<sip:c@d>;tag=2", **common)

    def test_everything_is_bytes(self):
        for name, message in self.all_messages():
            with self.subTest(method=name):
                self.assertIsInstance(message, bytes)

    def test_lines_end_with_crlf_and_headers_end_with_a_blank_line(self):
        for name, message in self.all_messages():
            with self.subTest(method=name):
                text = message.decode()
                self.assertNotIn("\n", text.replace("\r\n", ""))
                self.assertIn("\r\n\r\n", text)

    def test_content_length_matches_the_body(self):
        for name, message in self.all_messages():
            with self.subTest(method=name):
                self.assertEqual(int(header_value(message, "Content-Length")),
                                 len(body_of(message)))

    def test_mandatory_headers_are_present_once(self):
        for name, message in self.all_messages():
            with self.subTest(method=name):
                lines = header_lines(message)
                for header in ("Via", "Max-Forwards", "From", "To", "Call-ID", "CSeq",
                               "Content-Length"):
                    starting = [l for l in lines if l.startswith(header + ": ")]
                    self.assertEqual(len(starting), 1, f"{header} in {name}")

    def test_request_line_names_the_method_and_ends_with_the_version(self):
        for name, message in self.all_messages():
            with self.subTest(method=name):
                request_line = header_lines(message)[0]
                self.assertTrue(request_line.startswith(name + " "), request_line)
                self.assertTrue(request_line.endswith(" SIP/2.0"), request_line)

    def test_cseq_carries_the_method(self):
        for name, message in self.all_messages():
            with self.subTest(method=name):
                self.assertEqual(header_value(message, "CSeq"), f"1 {name}")

    def test_via_carries_the_branch_with_its_magic_cookie(self):
        for name, message in self.all_messages():
            with self.subTest(method=name):
                self.assertIn(";branch=z9hG4bK", header_value(message, "Via"))


class RegisterTest(unittest.TestCase):
    def test_addresses_the_gateway_and_the_account(self):
        message = build_register(StubLine(), call_id="cid", tag="tagX",
                                 branch="z9hG4bKb", cseq=1, expires=600)
        self.assertEqual(header_lines(message)[0], f"REGISTER sip:{GATEWAY_HOST} SIP/2.0")
        self.assertEqual(header_value(message, "From"),
                         f"<sip:sip-phone@{GATEWAY_HOST}>;tag=tagX")
        self.assertEqual(header_value(message, "To"), f"<sip:sip-phone@{GATEWAY_HOST}>")
        self.assertEqual(header_value(message, "Expires"), "600")

    def test_contact_points_at_this_host_without_a_relay(self):
        message = build_register(StubLine(), call_id="cid", tag="t", branch="z9hG4bKb",
                                 cseq=1, expires=600)
        self.assertEqual(header_value(message, "Contact"),
                         f"<sip:sip-phone@{LOCAL_IP}:5060;transport=udp>")

    def test_contact_points_at_the_relay_when_one_is_configured(self):
        """This is the address the gateway sends calls to, so it has to be
        reachable from the gateway's own network."""
        line = StubLine(contact_host="203.0.113.10", contact_port=5070)
        message = build_register(line, call_id="cid", tag="t", branch="z9hG4bKb",
                                 cseq=1, expires=600)
        self.assertEqual(header_value(message, "Contact"),
                         "<sip:sip-phone@203.0.113.10:5070;transport=udp>")

    def test_deregistering_everything_uses_a_wildcard_contact(self):
        message = build_register(StubLine(), call_id="cid", tag="t", branch="z9hG4bKb",
                                 cseq=1, expires=0, wildcard_contact=True)
        self.assertEqual(header_value(message, "Contact"), "*")
        self.assertEqual(header_value(message, "Expires"), "0")

    def test_authorization_is_added_when_given(self):
        without = build_register(StubLine(), call_id="c", tag="t", branch="z9hG4bKb",
                                 cseq=1, expires=600)
        with_auth = build_register(StubLine(), call_id="c", tag="t", branch="z9hG4bKb",
                                   cseq=2, expires=600, auth_header=AUTH)
        self.assertNotIn("Authorization", without.decode())
        self.assertIn(AUTH, header_lines(with_auth))


class InviteTest(unittest.TestCase):
    def test_carries_the_sdp_as_its_body(self):
        message = build_invite(StubLine(), number="621", call_id="cid", from_tag="t",
                               branch="z9hG4bKb", cseq=1, sdp=SDP)
        self.assertEqual(body_of(message), SDP)
        self.assertEqual(header_value(message, "Content-Type"), "application/sdp")
        self.assertEqual(int(header_value(message, "Content-Length")), len(SDP))

    def test_addresses_the_number_at_the_gateway(self):
        message = build_invite(StubLine(), number="621", call_id="cid", from_tag="t",
                               branch="z9hG4bKb", cseq=1, sdp=SDP)
        self.assertEqual(header_lines(message)[0], f"INVITE sip:621@{GATEWAY_HOST} SIP/2.0")
        self.assertEqual(header_value(message, "To"), f"<sip:621@{GATEWAY_HOST}>")

    def test_the_callee_has_no_tag_yet(self):
        """A dialog's To tag comes from the far end's answer - putting one
        in the INVITE would claim a dialog that does not exist."""
        message = build_invite(StubLine(), number="621", call_id="cid", from_tag="t",
                               branch="z9hG4bKb", cseq=1, sdp=SDP)
        self.assertNotIn("tag=", header_value(message, "To"))


class CancelTest(unittest.TestCase):
    def test_repeats_the_branch_of_the_invite_it_cancels(self):
        """That branch is what identifies the transaction being given up;
        a fresh one would cancel nothing."""
        invite = build_invite(StubLine(), number="621", call_id="cid", from_tag="t",
                              branch="z9hG4bKsame", cseq=1, sdp=SDP)
        cancel = build_cancel(StubLine(), number="621", call_id="cid", from_tag="t",
                              branch="z9hG4bKsame", cseq=1)
        self.assertEqual(header_value(invite, "Via"), header_value(cancel, "Via"))
        self.assertEqual(header_value(invite, "Call-ID"), header_value(cancel, "Call-ID"))
        self.assertEqual(body_of(cancel), "")


class ByeTest(unittest.TestCase):
    def test_goes_to_the_peers_own_contact(self):
        """In-dialog requests go to the URI the peer gave us, which is
        often an opaque per-dialog address, not the number dialled."""
        message = build_bye(StubLine(), request_uri="sip:opaque-42@192.0.2.1:5060",
                            call_id="cid", from_header="<sip:a@b>;tag=1",
                            to_header="<sip:c@d>;tag=2", branch="z9hG4bKb")
        self.assertEqual(header_lines(message)[0], "BYE sip:opaque-42@192.0.2.1:5060 SIP/2.0")
        self.assertEqual(header_value(message, "CSeq"), "2 BYE")

    def test_both_dialog_tags_are_preserved(self):
        message = build_bye(StubLine(), request_uri="sip:x", call_id="cid",
                            from_header="<sip:a@b>;tag=local",
                            to_header="<sip:c@d>;tag=remote", branch="z9hG4bKb")
        self.assertEqual(header_value(message, "From"), "<sip:a@b>;tag=local")
        self.assertEqual(header_value(message, "To"), "<sip:c@d>;tag=remote")


class ResponseTest(unittest.TestCase):
    REQUEST = {
        "via": ["SIP/2.0/UDP 192.0.2.1:5060;branch=z9hG4bKone",
                "SIP/2.0/TCP 203.0.113.10:5070;branch=z9hG4bKtwo"],
        "from": '"Caller" <sip:015112345@fritz.box>;tag=abcd',
        "to": "<sip:sip-phone@192.0.2.1>",
        "call-id": "CALLID@192.0.2.1",
        "cseq": "1 INVITE",
    }

    def test_status_line_and_echoed_headers(self):
        message = build_response("180 Ringing", self.REQUEST)
        self.assertEqual(header_lines(message)[0], "SIP/2.0 180 Ringing")
        self.assertEqual(header_value(message, "From"), self.REQUEST["from"])
        self.assertEqual(header_value(message, "Call-ID"), self.REQUEST["call-id"])
        self.assertEqual(header_value(message, "CSeq"), self.REQUEST["cseq"])

    def test_every_via_is_echoed_in_order(self):
        """Dropping one breaks the path back through the relay."""
        message = build_response("200 OK", self.REQUEST)
        vias = [l[len("Via: "):] for l in header_lines(message) if l.startswith("Via: ")]
        self.assertEqual(vias, self.REQUEST["via"])

    def test_a_single_via_string_is_accepted_too(self):
        request = dict(self.REQUEST, via="SIP/2.0/UDP only;branch=z9hG4bKx")
        message = build_response("200 OK", request)
        self.assertEqual(header_value(message, "Via"), "SIP/2.0/UDP only;branch=z9hG4bKx")

    def test_to_tag_is_added_once(self):
        message = build_response("180 Ringing", self.REQUEST, to_tag="bridge123")
        self.assertEqual(header_value(message, "To"),
                         "<sip:sip-phone@192.0.2.1>;tag=bridge123")

    def test_an_existing_to_tag_is_not_replaced(self):
        request = dict(self.REQUEST, to="<sip:sip-phone@192.0.2.1>;tag=already")
        message = build_response("200 OK", request, to_tag="bridge123")
        self.assertEqual(header_value(message, "To"), "<sip:sip-phone@192.0.2.1>;tag=already")

    def test_body_and_extra_headers(self):
        message = build_response("200 OK", self.REQUEST,
                                 extra_headers=["Contact: <sip:sip-phone@198.51.100.2:5060>",
                                                "Content-Type: application/sdp"],
                                 body=SDP, to_tag="bridge123")
        self.assertEqual(body_of(message), SDP)
        self.assertEqual(int(header_value(message, "Content-Length")), len(SDP))
        self.assertEqual(header_value(message, "Content-Type"), "application/sdp")

    def test_missing_request_headers_do_not_raise(self):
        message = build_response("200 OK", {})
        self.assertEqual(header_lines(message)[0], "SIP/2.0 200 OK")
        self.assertEqual(int(header_value(message, "Content-Length")), 0)


class GeneratedValuesTest(unittest.TestCase):
    def test_branches_carry_the_magic_cookie_and_differ(self):
        first, second = sip_requests.new_branch(), sip_requests.new_branch()
        self.assertTrue(first.startswith("z9hG4bK"))
        self.assertNotEqual(first, second)

    def test_a_branch_suffix_is_appended(self):
        self.assertTrue(sip_requests.new_branch("x2").endswith("x2"))

    def test_tags_differ(self):
        self.assertNotEqual(sip_requests.new_tag(), sip_requests.new_tag())


class ContactHeaderTest(unittest.TestCase):
    def test_transport_can_be_left_out(self):
        """The 200 OK answering an INVITE names the contact without a
        transport parameter."""
        line = StubLine()
        self.assertNotIn("transport=",
                         sip_requests.contact_header(line, with_transport=False))
        self.assertIn("transport=", sip_requests.contact_header(line))


class AckTargetTest(unittest.TestCase):
    """An ACK for a 2xx is a request inside the dialog, and goes where the
    dialog's remote end lives - the Contact from the 200 OK, not the
    number that was dialled. Same lesson as the BYE, learned in the same
    place twice."""

    def test_it_goes_to_the_contact_when_there_is_one(self):
        ack = sip_requests.build_ack(
            StubLine(), number="**611", call_id="c", from_tag="t", branch="b", cseq=1,
            to_header="<sip:x>;tag=theirs",
            request_uri="sip:opaque@192.0.2.1:44528;transport=tcp").decode()
        self.assertTrue(ack.startswith("ACK sip:opaque@192.0.2.1:44528;transport=tcp SIP/2.0"),
                        ack.split("\r\n")[0])

    def test_without_one_it_falls_back_to_the_number(self):
        """A call that never captured a Contact should still be
        acknowledged rather than not at all."""
        ack = sip_requests.build_ack(
            StubLine(), number="**611", call_id="c", from_tag="t", branch="b", cseq=1,
            to_header="<sip:x>;tag=theirs").decode()
        self.assertIn("ACK sip:**611@", ack.split("\r\n")[0])


if __name__ == "__main__":
    unittest.main()
