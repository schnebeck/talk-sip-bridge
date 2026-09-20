"""What the message helpers compute."""
import unittest

from sip_messages import (VALID_NUMBER, dialled_number, digest_response,
                          extract_contact_uri, md5hex, parse_auth_challenge,
                          parse_sip_headers)


class ParseHeadersTest(unittest.TestCase):
    def test_headers_are_lowercased_and_stripped(self):
        headers = parse_sip_headers("SIP/2.0 200 OK\r\nCall-ID:  abc \r\nCSeq: 1 REGISTER\r\n\r\n")
        self.assertEqual(headers["call-id"], "abc")
        self.assertEqual(headers["cseq"], "1 REGISTER")

    def test_every_via_is_kept_in_order(self):
        """A request through a relay carries several; a response has to
        repeat all of them or it cannot find its way back."""
        headers = parse_sip_headers(
            "INVITE sip:x SIP/2.0\r\n"
            "Via: SIP/2.0/UDP first;branch=z9hG4bK1\r\n"
            "Via: SIP/2.0/TCP second;branch=z9hG4bK2\r\n"
            "Via: SIP/2.0/TCP third;branch=z9hG4bK3\r\n\r\n")
        self.assertEqual(headers["via"],
                         ["SIP/2.0/UDP first;branch=z9hG4bK1",
                          "SIP/2.0/TCP second;branch=z9hG4bK2",
                          "SIP/2.0/TCP third;branch=z9hG4bK3"])

    def test_short_form_via_counts_as_via(self):
        headers = parse_sip_headers("INVITE sip:x SIP/2.0\r\nv: SIP/2.0/UDP short\r\n\r\n")
        self.assertEqual(headers["via"], ["SIP/2.0/UDP short"])

    def test_parsing_stops_at_the_body(self):
        headers = parse_sip_headers(
            "INVITE sip:x SIP/2.0\r\nCall-ID: abc\r\n\r\nv=0\r\no=- 0 0 IN IP4 192.0.2.1\r\n")
        self.assertNotIn("o=-", headers)
        self.assertNotIn("v", headers)

    def test_a_value_containing_a_colon_survives(self):
        headers = parse_sip_headers("INVITE sip:x SIP/2.0\r\nContact: <sip:a@b:5060>\r\n\r\n")
        self.assertEqual(headers["contact"], "<sip:a@b:5060>")


class DigestTest(unittest.TestCase):
    def test_known_value(self):
        """Pinned, not recomputed: the order of the fields and the colons
        between them are the part that breaks."""
        self.assertEqual(
            digest_response("sip-phone", "fritz.box", "secret", "REGISTER", "sip:192.0.2.1", "deadbeef"),
            "d7d1aec415abacb9171fa74ebe3f73a0")

    def test_composition(self):
        ha1 = md5hex("u:r:p")
        ha2 = md5hex("INVITE:sip:h")
        self.assertEqual(digest_response("u", "r", "p", "INVITE", "sip:h", "n"),
                         md5hex(f"{ha1}:n:{ha2}"))

    def test_a_different_nonce_gives_a_different_response(self):
        first = digest_response("u", "r", "p", "REGISTER", "sip:h", "nonce-1")
        second = digest_response("u", "r", "p", "REGISTER", "sip:h", "nonce-2")
        self.assertNotEqual(first, second)


class AuthChallengeTest(unittest.TestCase):
    def test_realm_and_nonce_are_unquoted(self):
        parsed = parse_auth_challenge('Digest realm="fritz.box", nonce="abc123", algorithm=MD5')
        self.assertEqual(parsed["realm"], "fritz.box")
        self.assertEqual(parsed["nonce"], "abc123")
        self.assertEqual(parsed["algorithm"], "MD5")

    def test_scheme_prefix_is_optional_and_case_insensitive(self):
        self.assertEqual(parse_auth_challenge('digest realm="r"')["realm"], "r")
        self.assertEqual(parse_auth_challenge('realm="r"')["realm"], "r")

    def test_empty_challenge_yields_nothing(self):
        self.assertEqual(parse_auth_challenge(""), {})


class ContactUriTest(unittest.TestCase):
    def test_angle_brackets(self):
        self.assertEqual(extract_contact_uri("<sip:621@192.0.2.1:5060>"), "sip:621@192.0.2.1:5060")

    def test_bare_uri_with_parameters(self):
        self.assertEqual(extract_contact_uri("sip:621@192.0.2.1;transport=tcp"), "sip:621@192.0.2.1")

    def test_display_name_and_parameters_outside_the_brackets(self):
        self.assertEqual(extract_contact_uri('"Phone" <sip:a@b>;expires=600'), "sip:a@b")


class DialledNumberTest(unittest.TestCase):
    """Which number a call came in on - what decides whether it is the
    bridge's own call or a person's, so a wrong answer here either takes
    someone's call away or refuses one meant for the bridge."""

    def test_the_header_meant_for_it_wins(self):
        """A gateway that fills in P-Called-Party-ID means it: this one
        addresses the INVITE to the account's own contact and puts the
        dialled extension only here."""
        self.assertEqual(
            dialled_number("INVITE sip:sip-phone@192.168.1.10:5070;transport=tcp SIP/2.0",
                           {"to": "<sip:sip-phone@192.168.1.10:5070>",
                            "p-called-party-id": "<sip:**621@fritz.box>"}),
            "**621")

    def test_the_request_uri_says_it(self):
        headers = {"to": "<sip:**622@fritz.box>"}
        self.assertEqual(
            dialled_number("INVITE sip:**622@10.1.1.1:5091;transport=tcp SIP/2.0", headers),
            "**622")

    def test_a_full_number_survives_intact(self):
        self.assertEqual(
            dialled_number("INVITE sip:+493012345@provider.example SIP/2.0", {}),
            "+493012345")

    def test_to_answers_when_the_request_uri_names_no_user(self):
        """A relay may rewrite the Request-URI down to the host it is
        forwarding to; what was dialled is then only in To."""
        self.assertEqual(dialled_number("INVITE sip:10.1.1.1:5091 SIP/2.0",
                                        {"to": '"Lobby" <sip:**622@fritz.box>;tag=x'}),
                         "**622")

    def test_nothing_to_go_on_is_empty_not_a_guess(self):
        self.assertEqual(dialled_number("INVITE sip:10.1.1.1 SIP/2.0", {}), "")
        self.assertEqual(dialled_number("", {}), "")


class NumberAllowlistTest(unittest.TestCase):
    """The last line of defence against injecting headers through a
    dialled number."""

    def test_ordinary_numbers_pass(self):
        for number in ("621", "+4930123456", "**621", "*99#", "0711-12345"):
            with self.subTest(number=number):
                self.assertTrue(VALID_NUMBER.fullmatch(number))

    def test_anything_that_could_break_out_of_a_header_is_rejected(self):
        for number in ("621\r\nTo: <sip:evil@host>", "6 21", "621@host", "a" * 33, "",
                       "621\n", "<621>", "621;tag=x"):
            with self.subTest(number=number):
                self.assertIsNone(VALID_NUMBER.fullmatch(number))


if __name__ == "__main__":
    unittest.main()
