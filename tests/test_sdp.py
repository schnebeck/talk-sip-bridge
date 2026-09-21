"""What the SDP layer offers, answers and reads back."""
import unittest

from payload_types import PT_G722, PT_PCMA, PT_PCMU, PT_TELEPHONE_EVENT
from sip_sdp import (answer_sdp, choose_payload_type, extract_sip_body,
                     offer_sdp, parse_offered_payload_types,
                     parse_sdp_media_address, parse_telephone_event_type)
from tests.support import RELAY_LAN_HOST, StubLine


class OfferTest(unittest.TestCase):
    def test_offers_every_codec_with_g722_first(self):
        """Both halves of G.711 are offered, not just one: which of them a
        registrar speaks is regional, and some speak only one."""
        sdp, host, port = offer_sdp(StubLine(), 40000)
        media = [line for line in sdp.split("\r\n") if line.startswith("m=audio")][0]
        self.assertEqual(media,
                         f"m=audio 40000 RTP/AVP {PT_G722} {PT_PCMA} {PT_PCMU} {PT_TELEPHONE_EVENT}")
        self.assertIn(f"a=rtpmap:{PT_G722} G722/8000", sdp)
        self.assertIn(f"a=rtpmap:{PT_PCMA} PCMA/8000", sdp)
        self.assertIn(f"a=rtpmap:{PT_PCMU} PCMU/8000", sdp)

    def test_offers_key_presses_as_events(self):
        """Without telephone-event in the offer a gateway sends key presses
        as audio tones, if at all - and then nothing can read them."""
        sdp, _, _ = offer_sdp(StubLine(), 40000)
        self.assertIn(f"a=rtpmap:{PT_TELEPHONE_EVENT} telephone-event/8000", sdp)
        self.assertIn(f"a=fmtp:{PT_TELEPHONE_EVENT} 0-15", sdp)

    def test_advertises_this_host_without_a_relay(self):
        line = StubLine()
        sdp, host, port = offer_sdp(line, 40000)
        self.assertEqual(host, line.local_ip)
        self.assertEqual(port, 40000)
        self.assertIn(f"c=IN IP4 {line.local_ip}", sdp)

    def test_advertises_the_relay_when_one_is_configured(self):
        """The gateway can only deliver RTP to an address on its own LAN,
        so with a relay the SDP has to name the relay, not this host."""
        line = StubLine(media_relay_enabled=True, relay_lan_port=40010)
        sdp, host, port = offer_sdp(line, 40000)
        self.assertEqual((host, port), (RELAY_LAN_HOST, 40010))
        self.assertIn(f"c=IN IP4 {RELAY_LAN_HOST}", sdp)
        self.assertIn("m=audio 40010 ", sdp)


class AnswerTest(unittest.TestCase):
    def test_answer_names_exactly_one_codec(self):
        sdp, _, _ = answer_sdp(StubLine(), 40000, PT_G722)
        media = [line for line in sdp.split("\r\n") if line.startswith("m=audio")][0]
        self.assertEqual(media, f"m=audio 40000 RTP/AVP {PT_G722}")
        self.assertIn(f"a=rtpmap:{PT_G722} G722/8000", sdp)
        self.assertNotIn("PCMU", sdp)

    def test_pcmu_answer(self):
        sdp, _, _ = answer_sdp(StubLine(), 40000, PT_PCMU)
        self.assertIn(f"a=rtpmap:{PT_PCMU} PCMU/8000", sdp)
        self.assertNotIn("G722", sdp)

    def test_pcma_answer(self):
        sdp, _, _ = answer_sdp(StubLine(), 40000, PT_PCMA)
        self.assertIn(f"a=rtpmap:{PT_PCMA} PCMA/8000", sdp)
        self.assertNotIn("PCMU", sdp)


class TelephoneEventTest(unittest.TestCase):
    def test_the_answer_echoes_the_number_the_caller_chose(self):
        """Their number, not ours: it is the only one they will send
        events under."""
        sdp, _, _ = answer_sdp(StubLine(), 40000, PT_G722, dtmf_payload_type=96)
        media = [line for line in sdp.split("\r\n") if line.startswith("m=audio")][0]
        self.assertEqual(media, f"m=audio 40000 RTP/AVP {PT_G722} 96")
        self.assertIn("a=rtpmap:96 telephone-event/8000", sdp)

    def test_an_answer_claims_no_events_that_were_not_offered(self):
        sdp, _, _ = answer_sdp(StubLine(), 40000, PT_G722)
        self.assertNotIn("telephone-event", sdp)
        self.assertEqual([line for line in sdp.split("\r\n") if line.startswith("m=audio")][0],
                         f"m=audio 40000 RTP/AVP {PT_G722}")

    def test_the_number_is_read_from_the_offer(self):
        for offered, expected in (("a=rtpmap:101 telephone-event/8000", 101),
                                  ("a=rtpmap:96 telephone-event/8000", 96),
                                  ("a=rtpmap:101 TELEPHONE-EVENT/8000", 101),
                                  ("a=rtpmap:9 G722/8000", None)):
            with self.subTest(offered=offered):
                self.assertEqual(parse_telephone_event_type(f"v=0\r\n{offered}\r\n"), expected)


class CodecChoiceTest(unittest.TestCase):
    def test_g722_wins_when_offered(self):
        self.assertEqual(choose_payload_type([PT_PCMU, PT_G722]), PT_G722)
        self.assertEqual(choose_payload_type([PT_G722]), PT_G722)

    def test_falls_back_to_pcmu(self):
        self.assertEqual(choose_payload_type([PT_PCMU]), PT_PCMU)

    def test_a_law_is_chosen_when_it_is_what_is_offered(self):
        """The European half of G.711, and what some registrars offer
        instead of mu-law rather than alongside it."""
        self.assertEqual(choose_payload_type([PT_PCMA]), PT_PCMA)
        self.assertEqual(choose_payload_type([PT_PCMA, PT_PCMU]), PT_PCMA)

    def test_between_equals_the_callers_own_order_decides(self):
        """Their list is their preference; there is no reason to overrule
        it where both are the same quality."""
        self.assertEqual(choose_payload_type([PT_PCMU, PT_PCMA]), PT_PCMU)
        self.assertEqual(choose_payload_type([PT_PCMA, PT_PCMU]), PT_PCMA)

    def test_codecs_we_cannot_speak_are_not_echoed_back(self):
        """G.726, iLBC and telephone-event are in the offer this bridge's
        own gateway sends; answering with one would produce a call that
        connects and carries nothing."""
        self.assertIsNone(choose_payload_type([2, 97, 101, 120]))

    def test_nothing_in_common_is_said_rather_than_guessed(self):
        """None is what makes the caller get a 488 instead of a silent
        call - see CallManager.answer."""
        self.assertIsNone(choose_payload_type([]))


class ParseTest(unittest.TestCase):
    def test_payload_types_come_from_the_audio_line(self):
        self.assertEqual(
            parse_offered_payload_types("v=0\r\nm=audio 7078 RTP/AVP 9 0 101\r\na=rtpmap:9 G722/8000"),
            [9, 0, 101])

    def test_no_audio_line_yields_nothing(self):
        self.assertEqual(parse_offered_payload_types("v=0\r\nm=video 7078 RTP/AVP 96"), [])

    def test_media_address(self):
        self.assertEqual(
            parse_sdp_media_address("v=0\r\nc=IN IP4 192.0.2.5\r\nm=audio 7078 RTP/AVP 9\r\n"),
            ("192.0.2.5", 7078))

    def test_media_address_ignores_a_ttl_suffix(self):
        self.assertEqual(
            parse_sdp_media_address("c=IN IP4 192.0.2.5/127\r\nm=audio 7078 RTP/AVP 0\r\n"),
            ("192.0.2.5", 7078))

    def test_incomplete_media_description_yields_none(self):
        self.assertIsNone(parse_sdp_media_address("c=IN IP4 192.0.2.5\r\n"))
        self.assertIsNone(parse_sdp_media_address("m=audio 7078 RTP/AVP 0\r\n"))
        self.assertIsNone(parse_sdp_media_address(""))


class BodyTest(unittest.TestCase):
    def test_body_is_what_follows_the_blank_line(self):
        self.assertEqual(
            extract_sip_body("SIP/2.0 200 OK\r\nCall-ID: x\r\n\r\nv=0\r\nm=audio 1 RTP/AVP 0\r\n"),
            "v=0\r\nm=audio 1 RTP/AVP 0\r\n")

    def test_no_body(self):
        self.assertEqual(extract_sip_body("SIP/2.0 200 OK\r\nCall-ID: x\r\n"), "")


if __name__ == "__main__":
    unittest.main()
