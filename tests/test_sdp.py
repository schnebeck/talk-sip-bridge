"""What the SDP layer offers, answers and reads back."""
import unittest

from payload_types import PT_G722, PT_PCMU
from sip_sdp import (answer_sdp, choose_payload_type, extract_sip_body,
                     offer_sdp, parse_offered_payload_types,
                     parse_sdp_media_address)
from tests.support import RELAY_LAN_HOST, StubLine


class OfferTest(unittest.TestCase):
    def test_offers_both_codecs_with_g722_first(self):
        sdp, host, port = offer_sdp(StubLine(), 40000)
        media = [l for l in sdp.split("\r\n") if l.startswith("m=audio")][0]
        self.assertEqual(media, f"m=audio 40000 RTP/AVP {PT_G722} {PT_PCMU}")
        self.assertIn(f"a=rtpmap:{PT_G722} G722/8000", sdp)
        self.assertIn(f"a=rtpmap:{PT_PCMU} PCMU/8000", sdp)

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
        media = [l for l in sdp.split("\r\n") if l.startswith("m=audio")][0]
        self.assertEqual(media, f"m=audio 40000 RTP/AVP {PT_G722}")
        self.assertIn(f"a=rtpmap:{PT_G722} G722/8000", sdp)
        self.assertNotIn("PCMU", sdp)

    def test_pcmu_answer(self):
        sdp, _, _ = answer_sdp(StubLine(), 40000, PT_PCMU)
        self.assertIn(f"a=rtpmap:{PT_PCMU} PCMU/8000", sdp)
        self.assertNotIn("G722", sdp)


class CodecChoiceTest(unittest.TestCase):
    def test_g722_wins_when_offered(self):
        self.assertEqual(choose_payload_type([PT_PCMU, PT_G722]), PT_G722)
        self.assertEqual(choose_payload_type([PT_G722]), PT_G722)

    def test_falls_back_to_pcmu(self):
        self.assertEqual(choose_payload_type([PT_PCMU]), PT_PCMU)

    def test_unknown_codecs_do_not_win(self):
        """Only two codecs are implemented; anything else offered has to
        end up as PCMU rather than being echoed back."""
        self.assertEqual(choose_payload_type([8, 97, 101]), PT_PCMU)
        self.assertEqual(choose_payload_type([]), PT_PCMU)


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
