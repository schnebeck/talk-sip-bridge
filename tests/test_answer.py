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

    def test_the_answer_is_a_200_with_a_codec_both_sides_speak(self):
        answered, rtp = self.answer()
        response = self.manager.transport.sent[-1]
        self.assertTrue(response.startswith("SIP/2.0 200 OK"))
        self.assertIn("m=audio 40000 RTP/AVP", response)
        self.assertEqual(rtp.dtmf_payload_type, 101)


if __name__ == "__main__":
    unittest.main()
