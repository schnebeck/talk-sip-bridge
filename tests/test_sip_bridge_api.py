# talk-sip-bridge - tests/test_sip_bridge_api.py
# Asking Nextcloud for a conversation as a SIP bridge.
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

"""Asking Nextcloud for a conversation as a SIP bridge.

This is the door Talk's own telephony backend uses, and the only way an
incoming caller becomes a participant Nextcloud knows about rather than a
session that exists in the signaling server alone. Getting the signature
wrong means a 401 and no room; getting the failure handling wrong means a
caller left ringing because a number was not in a table.
"""
import hashlib
import hmac
import io
import json
import unittest
import urllib.error
from unittest import mock

from tests.support import StubLine, env, needs_media_stack  # noqa: F401  (env first: sets the environment)
import talk_sip_bridge

SECRET = "sip-bridge-secret"
DIALLED = "+4930611"
CALLER = "+4930622"


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class RecordingOpener:
    def __init__(self, payload=None, error=None):
        self.requests = []
        self.payload = payload
        self.error = error

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        return FakeResponse(json.dumps(self.payload or {"ocs": {"data": {}}}).encode())


# A default that "no answer at all" can be told apart from.
UNSET = object()


def room(token="abc123", actor_type="guests", actor_id="guest-hash"):
    return {"ocs": {"data": {"token": token, "actorType": actor_type,
                             "actorId": actor_id, "displayName": CALLER}}}


class SignatureTest(unittest.TestCase):
    def test_the_checksum_is_over_the_random_and_the_data(self):
        """Talk computes HMAC-SHA256(secret, random + data) and compares
        it lowercase - see ChecksumVerificationService."""
        headers = talk_sip_bridge.bridge_headers(SECRET, DIALLED)
        expected = hmac.new(SECRET.encode(),
                            (headers["Talk-SIPBridge-Random"] + DIALLED).encode(),
                            hashlib.sha256).hexdigest()
        self.assertEqual(headers["Talk-SIPBridge-Checksum"], expected)
        self.assertEqual(headers["Talk-SIPBridge-Checksum"].lower(),
                         headers["Talk-SIPBridge-Checksum"])

    def test_the_random_is_long_enough_and_fresh(self):
        """Under 32 characters is refused outright; a reused one would let
        a recorded request be replayed."""
        first = talk_sip_bridge.bridge_headers(SECRET, DIALLED)["Talk-SIPBridge-Random"]
        second = talk_sip_bridge.bridge_headers(SECRET, DIALLED)["Talk-SIPBridge-Random"]
        self.assertGreaterEqual(len(first), 32)
        self.assertNotEqual(first, second)

    def test_a_different_number_signs_differently(self):
        """The data is part of the signature, so a checksum for one number
        cannot be replayed for another."""
        headers = talk_sip_bridge.bridge_headers(SECRET, DIALLED)
        other = hmac.new(SECRET.encode(),
                         (headers["Talk-SIPBridge-Random"] + "+4930999").encode(),
                         hashlib.sha256).hexdigest()
        self.assertNotEqual(headers["Talk-SIPBridge-Checksum"], other)


class DirectDialInTest(unittest.TestCase):
    def call(self, opener):
        with env(), mock.patch("urllib.request.urlopen", opener):
            return talk_sip_bridge.direct_dial_in(DIALLED, CALLER, secret=SECRET)

    def test_it_posts_both_numbers_and_signs_the_dialled_one(self):
        opener = RecordingOpener(room())
        self.call(opener)
        request = opener.requests[0]
        self.assertTrue(request.full_url.endswith("/room/direct-dial-in"))
        self.assertEqual(request.get_method(), "POST")
        self.assertIn(f"phoneNumber={DIALLED.replace('+', '%2B')}", request.data.decode())
        self.assertIn("caller=", request.data.decode())
        random = request.get_header("Talk-sipbridge-random")
        self.assertEqual(request.get_header("Talk-sipbridge-checksum"),
                         hmac.new(SECRET.encode(), (random + DIALLED).encode(),
                                  hashlib.sha256).hexdigest())

    def test_the_room_and_the_callers_actor_come_back(self):
        result = self.call(RecordingOpener(room()))
        self.assertEqual(result["token"], "abc123")
        self.assertEqual(result["actorType"], "guests")

    def test_a_number_nobody_owns_is_not_an_error_to_shout_about(self):
        """404 means the dialled number is in no mapping. The call still
        has to go somewhere, so this reports nothing and lets the caller
        fall back to the room its line names."""
        error = urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(b""))
        self.assertIsNone(self.call(RecordingOpener(error=error)))

    def test_a_refused_signature_is_survived(self):
        error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b""))
        self.assertIsNone(self.call(RecordingOpener(error=error)))

    def test_sip_not_configured_is_survived(self):
        error = urllib.error.HTTPError("u", 501, "Not Implemented", {}, io.BytesIO(b""))
        self.assertIsNone(self.call(RecordingOpener(error=error)))

    def test_a_room_without_a_token_counts_as_no_room(self):
        self.assertIsNone(self.call(RecordingOpener({"ocs": {"data": {}}})))

    def test_without_a_secret_nothing_is_asked_at_all(self):
        """An unconfigured deployment must not send unsigned requests at
        a public endpoint on every incoming call."""
        opener = RecordingOpener(room())
        with env(), mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(talk_sip_bridge.direct_dial_in(DIALLED, CALLER, secret=""))
        self.assertEqual(opener.requests, [])


class MappingTest(unittest.TestCase):
    """Which numbers on a line are the bridge's own."""

    def test_a_mapping_is_parsed_per_line(self):
        with env(BRIDGE_DIALIN_NUMBERS="**622=4930622, **623 = 4930623") as configured:
            self.assertEqual(configured.lines[0].dialin_numbers,
                             {"**622": "4930622", "**623": "4930623"})

    def test_no_mapping_means_no_number_is_the_bridges(self):
        with env() as configured:
            self.assertEqual(configured.lines[0].dialin_numbers, {})

    def test_a_malformed_entry_is_refused_rather_than_skipped(self):
        """Skipping it would route calls to that number the other way
        with nothing anywhere saying so."""
        for broken in ("**622", "=4930622", "**622=", "**622=4930622,**623"):
            with self.subTest(entry=broken), self.assertRaises(RuntimeError):
                with env(BRIDGE_DIALIN_NUMBERS=broken):
                    pass


@needs_media_stack
class ConferenceNumberTest(unittest.TestCase):
    """Which calls are asked for a meeting id - and, on a line whose
    number also rings a person's phone, which are not."""

    def is_conference(self, caller, dialled="**621", **line_settings):
        import inbound_call
        line = StubLine(conference_numbers=["**621"], **line_settings)
        return inbound_call.is_conference_call(line, dialled, caller)

    def test_a_conference_number_is_one_without_a_restriction(self):
        self.assertTrue(self.is_conference('"A Caller" <sip:+4930999@gw>'))

    def test_another_number_on_the_same_line_never_is(self):
        self.assertFalse(self.is_conference('<sip:+4930999@gw>', dialled="**622"))

    def test_a_restriction_keeps_outside_callers_out(self):
        """The reason it exists: this gateway delivers one number to
        everything, so a conference number is also the number somebody's
        own phone rings on. An outside call has to keep ringing."""
        self.assertFalse(self.is_conference('"Somebody" <sip:+4930999@gw>',
                                            conference_callers=r"\*\*[0-9]+"))

    def test_and_lets_the_gateways_own_extensions_in(self):
        self.assertTrue(self.is_conference('"FritzFon" <sip:**611@fritz.box>;tag=x',
                                           conference_callers=r"\*\*[0-9]+"))

    def test_the_number_decides_not_the_name_the_gateway_puts_on_it(self):
        """A handset announces itself as "FritzFon", which is nobody's
        number - matching against that would let any caller whose display
        name happens to fit straight in."""
        self.assertFalse(self.is_conference('"**611" <sip:+4930999@gw>',
                                            conference_callers=r"\*\*[0-9]+"))

    def test_a_partial_match_is_not_a_match(self):
        self.assertFalse(self.is_conference('<sip:**611999@gw>',
                                            conference_callers=r"\*\*[0-9]{3}"))


class ConferenceConfigTest(unittest.TestCase):
    def test_a_number_cannot_be_both_kinds_at_once(self):
        with self.assertRaises(RuntimeError):
            with env(BRIDGE_CONFERENCE_NUMBERS="**621",
                     BRIDGE_DIALIN_NUMBERS="**621=4930621"):
                pass

    def test_an_unusable_restriction_is_refused_at_startup(self):
        """Rather than at the first call, where it would look like the
        caller's fault."""
        with self.assertRaises(RuntimeError):
            with env(BRIDGE_CONFERENCE_NUMBERS="**621", BRIDGE_CONFERENCE_CALLERS="[unclosed"):
                pass

    def test_how_long_a_caller_gets_is_configurable(self):
        with env(BRIDGE_IVR_FIRST_DIGIT_TIMEOUT="40",
                 BRIDGE_IVR_NEXT_DIGIT_TIMEOUT="8.5") as configured:
            self.assertEqual(configured.ivr_first_digit_timeout, 40.0)
            self.assertEqual(configured.ivr_next_digit_timeout, 8.5)

    def test_a_caller_gets_long_enough_to_open_a_keypad_by_default(self):
        """Measured against a mobile client: the speaker has to be
        switched on and the keypad opened before a key can be pressed at
        all, and that is most of half a minute."""
        with env() as configured:
            self.assertGreaterEqual(configured.ivr_first_digit_timeout, 20)
            self.assertGreaterEqual(configured.ivr_next_digit_timeout, 5)

    def test_a_duration_that_is_not_one_is_refused_at_startup(self):
        for bad in ("soon", "0", "-5"):
            with self.subTest(value=bad), self.assertRaises(RuntimeError):
                with env(BRIDGE_IVR_FIRST_DIGIT_TIMEOUT=bad):
                    pass

    def test_the_numbers_are_a_plain_list(self):
        with env(BRIDGE_CONFERENCE_NUMBERS=" **900, **901 ") as configured:
            self.assertEqual(configured.lines[0].conference_numbers, ["**900", "**901"])


@needs_media_stack
class InboundRoutingTest(unittest.TestCase):
    """Where an incoming call goes, decided by the number it was placed
    to. The line in this deployment carries a person's own number as well
    as the bridge's, so this decision is the whole safety property: an
    unmapped number must never take the dial-in path, which answers."""

    def route(self, line, dialled, *, secret="sip-secret", answer=UNSET):
        """`answer` is what Nextcloud replies with; UNSET means the usual
        one, None means it declined to place the caller."""
        import asyncio
        import inbound_call
        answer = room() if answer is UNSET else answer
        asked = []

        def fake_dial_in(number, caller, **kw):
            asked.append((number, caller))
            return answer["ocs"]["data"] if answer else None

        # The secret is patched on the configuration inbound_call is
        # holding, not put in the environment: the module bound that object
        # when it was imported, and a reload gives a new one it never sees.
        with mock.patch.object(inbound_call.config, "sip_shared_secret", secret), \
                mock.patch("talk_sip_bridge.direct_dial_in", fake_dial_in):
            result = asyncio.run(inbound_call.InboundCalls.room_for(
                mock.Mock(), line, f"<sip:{CALLER}@gw>", dialled))
        return result, asked

    def test_a_mapped_number_gets_the_conversation_nextcloud_makes(self):
        line = StubLine(dialin_numbers={"**622": "4930622"}, default_room_token="standing")
        (roomid, actor), asked = self.route(line, "**622")
        self.assertEqual(asked, [("4930622", CALLER)])
        self.assertEqual(roomid, "abc123")
        self.assertEqual(actor, {"actorType": "guests", "actorId": "guest-hash"})

    def test_an_unmapped_number_on_the_same_line_rings_as_before(self):
        line = StubLine(dialin_numbers={"**622": "4930622"}, default_room_token="standing")
        (roomid, actor), asked = self.route(line, "**621")
        self.assertEqual(asked, [], "a person's own number must not be dialled in")
        self.assertEqual((roomid, actor), ("standing", None))

    def test_a_call_with_no_number_at_all_rings_as_before(self):
        line = StubLine(dialin_numbers={"**622": "4930622"}, default_room_token="standing")
        self.assertEqual(self.route(line, "")[0], ("standing", None))

    def test_without_the_secret_nothing_is_asked(self):
        line = StubLine(dialin_numbers={"**622": "4930622"}, default_room_token="standing")
        (roomid, actor), asked = self.route(line, "**622", secret="")
        self.assertEqual(asked, [])
        self.assertEqual((roomid, actor), ("standing", None))

    def test_a_number_nextcloud_will_not_place_falls_back_to_the_room(self):
        line = StubLine(dialin_numbers={"**622": "4930622"}, default_room_token="standing")
        self.assertEqual(self.route(line, "**622", answer=None)[0], ("standing", None))


if __name__ == "__main__":
    unittest.main()
