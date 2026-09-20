"""Reading what a real gateway actually sends.

`fixtures/fritzbox/` holds messages the FRITZ!Box this bridge is deployed
against actually sent, captured off the wire, with a handset's name
replaced and the gateway's firmware version dropped. Every other test in
this suite feeds the parsers input that was written to be parsed; these
feed them the real thing, which is longer, carries headers nobody thought
about, and offers eleven codecs where an invented fixture offers two.

Only messages the gateway itself originated belong here. A capture also
holds our own replies and whatever a proxy in the path generated; a
recording of our own output asserts nothing but today's behaviour, bugs
included, and a proxy's output is not the gateway's. What identifies the
sender is the message, not the direction it was captured in: a `Server:`
or `User-Agent:` header, or a tag this bridge itself would have made.

The directory name is the disclaimer: this is one gateway's behaviour, not
the protocol. A second gateway's recordings would sit beside it and the
same tests would run over both.
"""
import pathlib
import unittest

from sip_messages import (caller_display_name, caller_number, content_length_of,
                          dialled_number, extract_contact_uri,
                          parse_auth_challenge, parse_sip_headers,
                          split_messages)
from sip_sdp import (choose_payload_type, extract_sip_body,
                     parse_offered_payload_types, parse_sdp_media_address,
                     parse_telephone_event_type)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "fritzbox"


def raw_message(name: str) -> bytes:
    """Read the bytes, not the text: these are wire recordings, and
    read_text() would quietly translate their CRLF line endings into the
    platform's own - taking the framing with it."""
    return (FIXTURES / f"{name}.txt").read_bytes()


def message(name: str) -> str:
    return raw_message(name).decode()


class InboundInviteTest(unittest.TestCase):
    """The call this bridge exists to answer, as the gateway words it."""

    def setUp(self):
        self.raw = message("inbound_invite")
        self.headers = parse_sip_headers(self.raw)

    def test_the_caller_is_readable_from_the_from_header(self):
        self.assertIn("Handset", self.headers["from"])
        self.assertIn("sip:**611@fritz.box", self.headers["from"])

    def test_the_callers_number_and_the_callers_name_are_not_the_same_string(self):
        """A real gateway sends both, and they differ: the name is what
        the owner typed into the box. Everything that asks for a number -
        Nextcloud's dial-in endpoint, the phone participant's number
        field, the rule saying who may use a conference number - has to
        get **611, and only what is shown to people gets "Handset"."""
        self.assertEqual(caller_number(self.headers["from"]), "**611")
        self.assertEqual(caller_display_name(self.headers["from"]), "Handset")

    def test_a_caller_without_a_display_name_still_has_a_number(self):
        self.assertEqual(caller_number("<sip:+493012345@fritz.box>;tag=x"), "+493012345")
        self.assertEqual(caller_display_name("<sip:+493012345@fritz.box>;tag=x"), "+493012345")

    def test_the_number_dialled_is_in_neither_the_request_uri_nor_to(self):
        """What the bridge is addressed as, twice: this gateway delivers
        a call to the account's own contact, so neither header says
        anything about which of its numbers was called."""
        self.assertIn("sip:sip-phone@", self.raw.split("\r\n", 1)[0])
        self.assertIn("sip:sip-phone@", self.headers["to"])

    def test_the_number_dialled_is_in_p_called_party_id(self):
        """Where RFC 3455 says it belongs, and the only place this
        gateway puts it. **9 is its "ring every handset" extension - a
        call placed to one handset carries that handset's extension."""
        self.assertEqual(dialled_number(self.raw.split("\r\n", 1)[0], self.headers), "**9")

    def test_the_dialog_contact_is_an_opaque_uri_not_the_caller(self):
        """This is why in-dialog requests go to Contact and not to the
        address originally dialled - the gateway hands out a per-dialog
        identifier that has nothing to do with either party."""
        contact = extract_contact_uri(self.headers["contact"])
        self.assertTrue(contact.startswith("sip:"))
        self.assertNotIn("**611", contact)
        self.assertIn("transport=tcp", self.headers["contact"])

    def test_the_offered_codecs_are_read_in_full(self):
        """Eleven of them, where a hand-written fixture offers two."""
        offered = parse_offered_payload_types(extract_sip_body(self.raw))
        self.assertEqual(offered, [9, 8, 0, 2, 102, 100, 99, 101, 97, 120, 121])

    def test_g722_is_chosen_out_of_that_list(self):
        offered = parse_offered_payload_types(extract_sip_body(self.raw))
        self.assertEqual(choose_payload_type(offered), 9)

    def test_the_media_address_is_found(self):
        self.assertEqual(parse_sdp_media_address(extract_sip_body(self.raw)),
                         ("192.168.1.1", 7078))

    def test_the_number_key_presses_will_arrive_under_is_read_from_the_offer(self):
        """101 here, but dynamic by definition - it is read, not assumed,
        and the gateway offers `telephone-event` alongside its codecs."""
        self.assertEqual(parse_telephone_event_type(extract_sip_body(self.raw)), 101)

    def test_a_padded_content_length_is_still_a_number(self):
        """The gateway writes "Content-Length:   415", with spaces."""
        header_block = self.raw.split("\r\n\r\n", 1)[0]
        self.assertEqual(content_length_of(header_block), 415)
        self.assertEqual(len(extract_sip_body(self.raw)), 415)

    def test_headers_nobody_planned_for_do_not_disturb_the_parse(self):
        for name in ("session-expires", "min-se", "p-called-party-id",
                     "allow-events", "accept-encoding"):
            self.assertIn(name, self.headers)

    def test_the_via_says_tcp(self):
        via = self.headers["via"]
        self.assertEqual(len(via), 1)
        self.assertTrue(via[0].startswith("SIP/2.0/TCP"))


class ChallengeTest(unittest.TestCase):
    def test_the_gateway_challenges_in_the_simple_form(self):
        """No qop and no opaque - which is why this bridge's RFC 2069
        style digest is accepted here. A server that sends qop is a
        different matter; see docs/CONCEPT.md's known gaps."""
        headers = parse_sip_headers(message("401_challenge"))
        challenge = parse_auth_challenge(headers["www-authenticate"])
        self.assertEqual(challenge["realm"], "fritz.box")
        self.assertTrue(challenge["nonce"])
        self.assertNotIn("qop", challenge)
        self.assertNotIn("opaque", challenge)


class CancelTest(unittest.TestCase):
    def test_the_cancel_names_the_same_call(self):
        invite = parse_sip_headers(message("inbound_invite"))
        cancel = parse_sip_headers(message("cancel"))
        self.assertEqual(cancel["call-id"], invite["call-id"])
        self.assertEqual(cancel["cseq"].split()[0], invite["cseq"].split()[0])


class StreamTest(unittest.TestCase):
    def test_the_real_messages_split_out_of_one_stream(self):
        """Framing, exercised on bytes that actually came off a TCP
        connection rather than on ones assembled for the purpose."""
        stream = b"".join(raw_message(n) for n in
                          ("inbound_invite", "cancel", "401_challenge"))
        messages, rest = split_messages(stream)
        self.assertEqual(len(messages), 3)
        self.assertEqual(rest, b"")
        self.assertTrue(messages[0].startswith(b"INVITE"))
        self.assertEqual(len(messages[0].split(b"\r\n\r\n", 1)[1]), 415)


if __name__ == "__main__":
    unittest.main()
