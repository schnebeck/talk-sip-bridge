# talk-sip-bridge - tests/test_call_media.py
# Routing, the duplicate-offer guard, and teardown of a call's media.
#
#   Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
#   Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
#   Written by Anthropic Claude Opus 5 - AI generated content.
#
#   Free software under the GNU General Public License, version 3 or later.
#   There is no warranty, to the extent permitted by law. The full text is
#   in LICENSES/GPL-3.0-or-later.txt.
#
# SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
# SPDX-FileContributor: Anthropic Claude Opus 5 (AI generated content)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Routing, the duplicate-offer guard, and teardown of a call's media.

No real peer connections: what is checked here is which connection a
message belongs to and what happens to both when the call ends. That a
real offer is answerable at all is checked against a live signaling server
by tests/hardware/test_publish_and_verify.py.
"""
import asyncio
import unittest

from tests.support import needs_media_stack

try:
    from call_media import CallMedia, parse_ice_candidate
except ImportError:  # no media stack; every test here is skipped
    CallMedia = None

CANDIDATE = "candidate:2781844737 1 udp 2122194687 192.0.2.5 33388 typ host generation 0"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakePeerConnection:
    def __init__(self, *args, answer_sdp="v=0 answer", **kwargs):
        self.closed = False
        self.remote = None
        self.localDescription = type("D", (), {"sdp": answer_sdp})()
        self.candidates = []

    def on(self, event):
        """aiortc's event decorator; the handlers are not exercised here."""
        return lambda handler: handler

    async def setRemoteDescription(self, description):
        self.remote = description

    async def createAnswer(self):
        return "answer"

    async def setLocalDescription(self, description):
        pass

    async def addIceCandidate(self, candidate):
        self.candidates.append(candidate)

    async def close(self):
        self.closed = True


class FakeTask:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def media_with(publisher=None, subscriber=None, peer=None, human=None):
    media = CallMedia("call-1", rtp_session=None)
    media.publisher, media.publisher_peer_sessionid = publisher, peer
    media.subscriber, media.human_sessionid = subscriber, human
    return media


@needs_media_stack
class RoutingTest(unittest.TestCase):
    """Which connection an incoming message belongs to. Getting this wrong
    feeds an answer meant for one direction into the other."""

    def test_the_publishers_own_session_routes_to_the_publisher(self):
        publisher = FakePeerConnection()
        media = media_with(publisher=publisher, peer="own-session")
        self.assertEqual(media.peer_for("own-session"), (publisher, False))

    def test_the_subscribed_session_routes_to_the_subscriber(self):
        subscriber = FakePeerConnection()
        media = media_with(subscriber=subscriber, human="human-session")
        self.assertEqual(media.peer_for("human-session"), (subscriber, True))

    def test_an_unrelated_sender_matches_nothing(self):
        media = media_with(publisher=FakePeerConnection(), peer="own-session")
        self.assertIsNone(media.peer_for("somebody-else"))

    def test_a_session_id_matches_nothing_before_its_connection_exists(self):
        """Both ids are set alongside their connection; neither may match on
        its own, or a message arrives before there is anything to hand it to."""
        media = media_with(peer="own-session", human="human-session")
        self.assertIsNone(media.peer_for("own-session"))
        self.assertIsNone(media.peer_for("human-session"))

    def test_nothing_matches_a_missing_sender(self):
        media = media_with(publisher=FakePeerConnection(), peer=None)
        self.assertIsNone(media.peer_for(None))


@needs_media_stack
class SubscriberOfferTest(unittest.TestCase):
    def media(self):
        media = CallMedia("call-1", rtp_session=None)
        media.subscriber = FakePeerConnection()
        media.offer_arrived = asyncio.Event()
        return media

    def test_the_first_offer_is_answered(self):
        media = self.media()
        self.assertEqual(run(media.answer_subscriber_offer("v=0 offer")), "v=0 answer")
        self.assertTrue(media.offer_arrived.is_set())

    def test_a_later_offer_is_ignored_once_audio_is_flowing(self):
        """Answering one resets a connection that already works - observed
        as audio dropping mid-call. "Works" means frames have arrived, not
        that a track object exists: aiortc hands one over as soon as the
        offer is applied, and a connection that never completes has one
        too."""
        media = self.media()
        run(media.answer_subscriber_offer("v=0 offer"))
        media.subscriber_receiving = True          # a track arrived
        self.assertIsNone(run(media.answer_subscriber_offer("v=0 offer again")))

    def test_a_later_offer_rebuilds_the_subscription_while_nothing_flows(self):
        """The opposite case, and the one that cost a call its return
        direction: the server throws the subscription away and builds a
        new one when the publisher is not sending yet. An answer carrying
        the old one's id is refused - "answer message sid does not match
        subscriber sid" - so the second offer is the live one."""
        from unittest import mock

        media = self.media()
        media.human_sessionid = "person"
        run(media.answer_subscriber_offer("v=0 offer"))
        first = media.subscriber
        with mock.patch("call_media.RTCPeerConnection", FakePeerConnection):
            answer = run(media.answer_subscriber_offer("v=0 second offer"))
        self.assertEqual(answer, "v=0 answer")
        self.assertIsNot(media.subscriber, first, "the dead connection was kept")
        self.assertTrue(first.closed, "the dead connection was left open")
        self.assertEqual(media.subscriber.remote.sdp, "v=0 second offer")

    def test_the_arrival_is_what_stops_the_retries(self):
        media = self.media()
        self.assertFalse(media.offer_arrived.is_set())
        run(media.answer_subscriber_offer("v=0 offer"))
        self.assertTrue(media.offer_arrived.is_set())


@needs_media_stack
class CandidateTest(unittest.TestCase):
    def test_a_candidate_reaches_the_connection(self):
        pc = FakePeerConnection()
        media = CallMedia("call-1", rtp_session=None)
        run(media.add_candidate(pc, {"candidate": {"candidate": CANDIDATE, "sdpMid": "0",
                                                   "sdpMLineIndex": 0}}))
        self.assertEqual(len(pc.candidates), 1)

    def test_an_empty_candidate_is_not_an_error(self):
        """End-of-candidates arrives as an empty string."""
        pc = FakePeerConnection()
        media = CallMedia("call-1", rtp_session=None)
        run(media.add_candidate(pc, {"candidate": {"candidate": ""}}))
        run(media.add_candidate(pc, {}))
        run(media.add_candidate(pc, None))
        self.assertEqual(pc.candidates, [])

    def test_an_unparsable_candidate_does_not_propagate(self):
        """A malformed candidate must not take down the message loop."""
        pc = FakePeerConnection()
        media = CallMedia("call-1", rtp_session=None)
        run(media.add_candidate(pc, {"candidate": {"candidate": "nonsense"}}))

    def test_parsing_keeps_the_fields_webrtc_needs(self):
        parsed = parse_ice_candidate(CANDIDATE, sdp_mid="0", sdp_mline_index=0)
        self.assertEqual(parsed.ip, "192.0.2.5")
        self.assertEqual(parsed.port, 33388)
        self.assertEqual(parsed.protocol, "udp")
        self.assertEqual(parsed.type, "host")
        self.assertEqual(parsed.sdpMid, "0")


@needs_media_stack
class TeardownTest(unittest.TestCase):
    def test_closing_stops_both_directions_and_the_relay(self):
        media = media_with(publisher=FakePeerConnection(), subscriber=FakePeerConnection())
        relay = FakeTask()
        media.relay_task = relay
        publisher, subscriber = media.publisher, media.subscriber
        run(media.close())
        self.assertTrue(publisher.closed)
        self.assertTrue(subscriber.closed)
        self.assertTrue(relay.cancelled)

    def test_closing_twice_is_harmless(self):
        """Teardown can be reached from a BYE and from the connection dying
        at almost the same moment."""
        media = media_with(publisher=FakePeerConnection(), subscriber=FakePeerConnection())
        run(media.close())
        run(media.close())

    def test_closing_a_call_that_never_published(self):
        run(CallMedia("call-1", rtp_session=None).close())

    def test_a_closed_call_is_no_longer_publishing(self):
        media = media_with(publisher=FakePeerConnection(), peer="own-session")
        self.assertTrue(media.is_publishing)
        run(media.close())
        self.assertFalse(media.is_publishing)

    def test_a_closed_call_routes_nothing(self):
        media = media_with(publisher=FakePeerConnection(), peer="own-session")
        run(media.close())
        self.assertIsNone(media.peer_for("own-session"))


if __name__ == "__main__":
    unittest.main()
