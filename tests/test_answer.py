"""Answering a call that came in.

Measured against the deployment: everything about an answered call can
look right - 200 OK, a codec both sides speak, audio arriving from the
caller - while the caller hears nothing, because this side is sending its
audio to an address the caller never named. That is what these tests
pin down.
"""
import unittest
from unittest import mock

from tests.support import StubLine, needs_media_stack

try:
    from sip_call import CallManager
    from sip_messages import parse_sip_headers
except ImportError:      # no media stack
    CallManager = None

CALLER_IP = "192.0.2.77"
CALLER_RTP_PORT = 46012


def invite(sdp_port: int = CALLER_RTP_PORT, sdp_host: str = CALLER_IP, codecs: str = "0 8 101") -> str:
    sdp = "\r\n".join([
        "v=0", f"o=- 0 0 IN IP4 {sdp_host}", "s=-", f"c=IN IP4 {sdp_host}", "t=0 0",
        f"m=audio {sdp_port} RTP/AVP {codecs}",
        "a=rtpmap:0 PCMU/8000", "a=rtpmap:8 PCMA/8000",
        "a=rtpmap:101 telephone-event/8000", "a=sendrecv", ""])
    return "\r\n".join([
        "INVITE sip:**900@10.0.0.1:5060 SIP/2.0",
        f"Via: SIP/2.0/UDP {CALLER_IP}:5060;branch=z9hG4bKone",
        f'From: "A Caller" <sip:+4930622@{CALLER_IP}>;tag=callertag',
        "To: <sip:**900@fritz.box>",
        "Call-ID: answer-test@gateway", "CSeq: 1 INVITE",
        f"Contact: <sip:+4930622@{CALLER_IP}:5060>",
        "Content-Type: application/sdp", f"Content-Length: {len(sdp)}", "", sdp])


class FakeTransport:
    def __init__(self):
        self.sent = []

    def send(self, data, addr=None):
        self.sent.append(data.decode())


class FakeRtp:
    """Records what address a session was built with, and what it was
    changed to - the whole point of these tests."""

    instances = []

    def __init__(self, local_ip, local_port, remote_ip, remote_port, **kw):
        self.built_with = (remote_ip, remote_port)
        self.remote_addr = (remote_ip, remote_port)
        self.sample_rate = 8000
        self.samples_per_packet = 160
        self.payload_type = kw.get("payload_type")
        self.dtmf_payload_type = kw.get("dtmf_payload_type")
        FakeRtp.instances.append(self)

    def send_pcm(self, pcm):
        pass

    def close(self):
        pass


@needs_media_stack
class AnswerTest(unittest.TestCase):
    def setUp(self):
        FakeRtp.instances = []
        self.line = StubLine(local_ip="10.0.0.1", local_rtp_port=40000,
                             gateway_host="192.0.2.1", media_relay_enabled=False)
        self.manager = CallManager(self.line)
        self.manager.transport = FakeTransport()

    def answer(self, text=None):
        text = text or invite()
        headers = parse_sip_headers(text)
        with mock.patch("sip_call.RtpSession", FakeRtp), \
                mock.patch("sip_call.threading.Timer", mock.Mock()):
            self.manager.handle_invite(text, headers, headers["call-id"], ("192.0.2.1", 5060))
            answered = self.manager.answer()
        return answered, (FakeRtp.instances[-1] if FakeRtp.instances else None)

    def test_the_caller_gets_their_audio_where_they_asked_for_it(self):
        """The one that was wrong: a caller whose RTP port differs from
        this bridge's own heard silence, while everything else about the
        call - the 200 OK, the codec, their own audio arriving here -
        looked correct."""
        answered, rtp = self.answer()
        self.assertTrue(answered)
        self.assertEqual(rtp.remote_addr, (CALLER_IP, CALLER_RTP_PORT))

    def test_a_relay_stays_the_peer(self):
        """With a relay in between, the caller's own address is not
        reachable from here and the relay's is the only correct target."""
        self.line.media_relay_enabled = True
        self.line.relay_overlay_host = "10.1.1.5"
        self.line.relay_overlay_port = 40001
        answered, rtp = self.answer()
        self.assertTrue(answered)
        self.assertEqual(rtp.remote_addr, ("10.1.1.5", 40001))

    def test_an_offer_without_a_media_address_falls_back(self):
        """Nothing to go on: the gateway's own address on this port is
        the only guess left, and is better than not answering."""
        text = invite().replace(f"c=IN IP4 {CALLER_IP}\r\n", "").replace(
            f"m=audio {CALLER_RTP_PORT}", "m=audio 0")
        answered, rtp = self.answer(text)
        self.assertTrue(answered)
        self.assertEqual(rtp.remote_addr, ("192.0.2.1", 40000))

    def test_a_reinvite_inside_the_call_is_not_a_second_call(self):
        """Measured live: a handset lost power mid-call, came back, and
        the gateway re-offered inside the same dialog. Answering "486
        Busy Here" told it the dialog was gone and the call was torn
        down. Same Call-ID means the call that is already up."""
        self.answer()
        headers = parse_sip_headers(invite())
        with mock.patch("sip_call.RtpSession", FakeRtp):
            self.manager.handle_invite(invite(), headers, headers["call-id"], ("192.0.2.1", 5060))
        response = self.manager.transport.sent[-1]
        self.assertTrue(response.startswith("SIP/2.0 200 OK"), response.split("\r\n")[0])
        self.assertIn("m=audio 40000 RTP/AVP", response)

    def test_a_reinvite_keeps_the_tag_the_call_was_answered_with(self):
        """A different tag is a different dialog to the far end."""
        self.answer()
        first = [line for line in self.manager.transport.sent[-1].split("\r\n") if line.startswith("To:")][0]
        headers = parse_sip_headers(invite())
        self.manager.handle_invite(invite(), headers, headers["call-id"], ("192.0.2.1", 5060))
        second = [line for line in self.manager.transport.sent[-1].split("\r\n") if line.startswith("To:")][0]
        self.assertEqual(first.split("tag=")[1], second.split("tag=")[1])

    def test_a_reinvite_that_moves_the_audio_is_followed(self):
        """What a gateway re-offers for: hold, unhold, or a handset that
        came back on a different port."""
        _, rtp = self.answer()
        moved = invite(sdp_port=47000)
        headers = parse_sip_headers(moved)
        self.manager.handle_invite(moved, headers, headers["call-id"], ("192.0.2.1", 5060))
        self.assertEqual(rtp.remote_addr, (CALLER_IP, 47000))

    def test_a_second_call_is_still_refused(self):
        self.answer()
        other = invite().replace("answer-test@gateway", "another-call@gateway")
        headers = parse_sip_headers(other)
        self.manager.handle_invite(other, headers, headers["call-id"], ("192.0.2.1", 5060))
        self.assertTrue(self.manager.transport.sent[-1].startswith("SIP/2.0 486"))

    def test_a_reinvite_dropping_the_agreed_codec_is_refused(self):
        self.answer()
        narrowed = invite(codecs="8 101").replace("a=rtpmap:0 PCMU/8000\r\n", "")
        headers = parse_sip_headers(narrowed)
        self.manager.handle_invite(narrowed, headers, headers["call-id"], ("192.0.2.1", 5060))
        self.assertTrue(self.manager.transport.sent[-1].startswith("SIP/2.0 488"))

    def test_the_answer_is_a_200_with_a_codec_both_sides_speak(self):
        answered, rtp = self.answer()
        response = self.manager.transport.sent[-1]
        self.assertTrue(response.startswith("SIP/2.0 200 OK"))
        self.assertIn("m=audio 40000 RTP/AVP", response)
        self.assertEqual(rtp.dtmf_payload_type, 101)


@needs_media_stack
class InfoTest(unittest.TestCase):
    """Key presses that arrive as their own request. This gateway sent
    one mid-call and got no answer at all, because INFO was in no
    dispatch table."""

    def setUp(self):
        FakeRtp.instances = []
        self.line = StubLine(local_ip="10.0.0.1", local_rtp_port=40000,
                             gateway_host="192.0.2.1", media_relay_enabled=False)
        self.manager = CallManager(self.line)
        self.manager.transport = FakeTransport()
        with mock.patch("sip_call.RtpSession", FakeRtp), \
                mock.patch("sip_call.threading.Timer", mock.Mock()):
            text = invite()
            headers = parse_sip_headers(text)
            self.manager.handle_invite(text, headers, headers["call-id"], ("192.0.2.1", 5060))
            self.manager.answer()
        self.reported = []
        FakeRtp.instances[-1].report_digit = self.reported.append

    def info(self, body: str, content_type: str = "application/dtmf-relay"):
        text = "\r\n".join([
            "INFO sip:10.0.0.1:5060 SIP/2.0",
            f"Via: SIP/2.0/UDP {CALLER_IP}:5060;branch=z9hG4bKinfo",
            f'From: "A Caller" <sip:+4930622@{CALLER_IP}>;tag=callertag',
            "To: <sip:**900@fritz.box>;tag=whatever",
            "Call-ID: answer-test@gateway", "CSeq: 2 INFO",
            f"Content-Type: {content_type}", f"Content-Length: {len(body)}", "", body])
        headers = parse_sip_headers(text)
        self.manager.handle_info(text, headers, headers["call-id"], ("192.0.2.1", 5060))
        return self.manager.transport.sent[-1]

    def test_every_info_is_acknowledged(self):
        self.assertTrue(self.info("Signal=5\r\nDuration=160").startswith("SIP/2.0 200 OK"))

    def test_one_that_carries_nothing_is_acknowledged_too(self):
        """An unanswered in-dialog request is retransmitted and then
        read as a dead dialog - whatever was in it."""
        self.assertTrue(self.info("").startswith("SIP/2.0 200 OK"))
        self.assertEqual(self.reported, [])

    def test_the_key_is_read_out_of_the_relay_body(self):
        self.info("Signal=5\r\nDuration=160")
        self.assertEqual(self.reported, ["5"])

    def test_the_bare_form_is_read_too(self):
        self.info("7", content_type="application/dtmf")
        self.assertEqual(self.reported, ["7"])

    def test_the_star_and_hash_survive(self):
        self.info("Signal=*\r\nDuration=100")
        self.info("Signal=#\r\nDuration=100")
        self.assertEqual(self.reported, ["*", "#"])

    def test_an_info_for_another_call_reports_nothing(self):
        text = "\r\n".join([
            "INFO sip:10.0.0.1:5060 SIP/2.0",
            f"Via: SIP/2.0/UDP {CALLER_IP}:5060;branch=z9hG4bKelse",
            "From: <sip:x@y>;tag=t", "To: <sip:z@w>;tag=u",
            "Call-ID: some-other-call@gateway", "CSeq: 2 INFO",
            "Content-Type: application/dtmf-relay", "Content-Length: 8", "", "Signal=9"])
        headers = parse_sip_headers(text)
        self.manager.handle_info(text, headers, headers["call-id"], ("192.0.2.1", 5060))
        self.assertTrue(self.manager.transport.sent[-1].startswith("SIP/2.0 200 OK"))
        self.assertEqual(self.reported, [])

if __name__ == "__main__":
    unittest.main()
