"""The shapes this bridge sends to the signaling server.

The server does not complain about a message it does not recognise - it
ignores it. A wrong wrapper therefore looks like nothing happening, which
is why these shapes are asserted field by field rather than eyeballed
against the documentation they came from (docs/SIGNALING-API.md).
"""
import hashlib
import hmac
import unittest

import talk_messages as m

SECRET = "internal-secret"
BACKEND = "https://nextcloud.example"


class HelloTest(unittest.TestCase):
    def setUp(self):
        self.hello = m.hello(SECRET, BACKEND)

    def test_it_authenticates_as_an_internal_client(self):
        self.assertEqual(self.hello["type"], "hello")
        self.assertEqual(self.hello["hello"]["auth"]["type"], "internal")

    def test_the_token_is_the_random_value_keyed_with_the_secret(self):
        params = self.hello["hello"]["auth"]["params"]
        expected = hmac.new(SECRET.encode(), params["random"].encode(), hashlib.sha256).hexdigest()
        self.assertEqual(params["token"], expected)

    def test_the_random_value_is_long_enough_and_fresh_each_time(self):
        """At least 32 bytes, says the protocol; reusing one would let a
        recorded hello be replayed."""
        params = self.hello["hello"]["auth"]["params"]
        self.assertGreaterEqual(len(params["random"]), 64)  # hex of 32 bytes
        self.assertNotEqual(params["random"], m.hello(SECRET, BACKEND)["hello"]["auth"]["params"]["random"])

    def test_the_backend_is_sent(self):
        """Required, and documented nowhere but the server's own source -
        without it the hello is rejected."""
        self.assertEqual(self.hello["hello"]["auth"]["params"]["backend"], BACKEND)

    def test_both_features_are_declared(self):
        """start-dialout to receive dialout requests at all, and
        internal-incall so audio is announced when the publisher exists
        rather than the moment the connection opens."""
        self.assertEqual(set(self.hello["hello"]["features"]), {"start-dialout", "internal-incall"})

    def test_the_declared_features_cannot_be_mutated_through_the_message(self):
        self.hello["hello"]["features"].append("nonsense")
        self.assertNotIn("nonsense", m.FEATURES)


class SessionTest(unittest.TestCase):
    def test_a_phone_participant_is_announced_without_audio(self):
        """It can never carry any: publishers exist only under a real
        client's session. Claiming audio leaves Talk's clients retrying
        against a tile that stays silent forever."""
        message = m.add_session("phone-1", "room-token", call_id="call-1",
                                number="+4930622", displayname="Anna")
        incall = message["internal"]["addsession"]["incall"]
        self.assertTrue(incall & m.FLAG_IN_CALL)
        self.assertTrue(incall & m.FLAG_WITH_PHONE)
        self.assertFalse(incall & m.FLAG_WITH_AUDIO)

    def test_it_can_be_announced_with_audio_instead(self):
        """The other side of the trade: without the flag Talk's clients
        build no peer for the phone and never stop their "waiting for
        someone" sound; with it they look for a stream a virtual session
        cannot have. Which is less wrong is measured per deployment - see
        config.phone_participant."""
        message = m.add_session("phone-1", "room-token", call_id="call-1",
                                number="+4930622", displayname="Anna", with_audio=True)
        incall = message["internal"]["addsession"]["incall"]
        self.assertTrue(incall & m.FLAG_WITH_AUDIO)
        self.assertTrue(incall & m.FLAG_WITH_PHONE)
        self.assertTrue(incall & m.FLAG_IN_CALL)

    def test_the_caller_is_named_by_displayname(self):
        """The field Talk renders participants by; without it the caller
        shows up as a guest."""
        message = m.add_session("phone-1", "room-token", call_id="call-1",
                                number="+4930622", displayname="Anna")
        user = message["internal"]["addsession"]["user"]
        self.assertEqual(user["displayname"], "Anna")
        self.assertEqual(user["type"], "phone")

    def test_the_call_id_round_trips_so_the_session_can_be_recognised(self):
        """The server assigns its own room session id; this is what maps it
        back to the call."""
        message = m.add_session("phone-1", "room-token", call_id="call-42",
                                number="622", displayname="622")
        self.assertEqual(message["internal"]["addsession"]["user"]["callid"], "call-42")

    def test_no_actor_is_claimed(self):
        """Nextcloud rejects an actor that is not already invited to the
        room, and fails the whole addsession with it."""
        message = m.add_session("phone-1", "room-token", call_id="c", number="1", displayname="1")
        self.assertNotIn("options", message["internal"]["addsession"])

    def test_removing_names_the_same_session_and_room(self):
        message = m.remove_session("phone-1", "room-token")
        self.assertEqual(message["internal"]["type"], "removesession")
        self.assertEqual(message["internal"]["removesession"],
                         {"sessionid": "phone-1", "roomid": "room-token"})


class RoomAndFlagsTest(unittest.TestCase):
    def test_joining_a_room_is_identifiable_in_the_reply(self):
        """The confirmation comes back carrying this id, and so does the
        already_joined error that means the same thing."""
        self.assertEqual(m.join_room("room-token")["id"], m.ROOM_REQUEST_ID)
        self.assertEqual(m.join_room("room-token")["room"], {"roomid": "room-token"})

    def test_in_call_flags_are_sent_as_given(self):
        message = m.set_incall(m.FLAG_IN_CALL | m.FLAG_WITH_AUDIO)
        self.assertEqual(message["internal"]["incall"]["incall"], 3)

    def test_clearing_them_is_a_flag_value_not_a_different_message(self):
        self.assertEqual(m.set_incall(0)["internal"]["incall"]["incall"], 0)


class MediaTest(unittest.TestCase):
    def test_the_publish_offer_is_addressed_to_our_own_session(self):
        """A virtual session has no client that could answer one."""
        message = m.publish_offer("own-session", "call-1", "v=0", "Anna")
        self.assertEqual(message["message"]["recipient"]["sessionid"], "own-session")
        self.assertEqual(message["message"]["data"]["to"], "own-session")

    def test_the_offer_names_the_tile_that_carries_the_audio(self):
        message = m.publish_offer("own-session", "call-1", "v=0", "Anna")
        self.assertEqual(message["message"]["data"]["payload"]["nick"], "Anna")
        self.assertEqual(message["message"]["data"]["payload"]["sdp"], "v=0")

    def test_audio_only_is_still_roomtype_video(self):
        for message in (m.publish_offer("s", "c", "v=0", "n"),
                        m.request_offer("c", "human"),
                        m.subscribe_answer("human", "sid", "v=0")):
            self.assertEqual(message["message"]["data"]["roomType"], "video")

    def test_requesting_a_stream_names_who_it_is_wanted_from(self):
        message = m.request_offer("call-1", "human-session")
        self.assertEqual(message["message"]["recipient"]["sessionid"], "human-session")
        self.assertEqual(message["message"]["data"]["type"], "requestoffer")

    def test_the_answer_goes_back_to_the_sender_with_their_sid(self):
        message = m.subscribe_answer("human-session", "sid-7", "v=0 answer")
        self.assertEqual(message["message"]["recipient"]["sessionid"], "human-session")
        self.assertEqual(message["message"]["data"]["sid"], "sid-7")
        self.assertEqual(message["message"]["data"]["payload"]["sdp"], "v=0 answer")


class DialoutReplyTest(unittest.TestCase):
    def test_a_status_keeps_the_wrapper_and_echoes_the_request(self):
        message = m.dialout_status("room-token", "call-1", "accepted", "req-1")
        self.assertEqual(message["id"], "req-1")
        self.assertEqual(message["type"], "internal")
        self.assertEqual(message["internal"]["type"], "dialout")
        self.assertEqual(message["internal"]["dialout"],
                         {"roomid": "room-token", "type": "status",
                          "status": {"callid": "call-1", "status": "accepted"}})

    def test_an_unsolicited_update_carries_no_request_id(self):
        """Progress reports answer nothing, so there is nothing to echo."""
        message = m.dialout_status("room-token", "call-1", "connected")
        self.assertNotIn("id", message)
        self.assertEqual(message["internal"]["dialout"]["status"]["status"], "connected")

    def test_an_error_replaces_the_status_rather_than_joining_it(self):
        message = m.dialout_error("room-token", "no such number", "req-1")
        dialout = message["internal"]["dialout"]
        self.assertEqual(dialout["type"], "error")
        self.assertEqual(dialout["error"], {"code": "call_failed", "message": "no such number"})
        self.assertNotIn("status", dialout)


if __name__ == "__main__":
    unittest.main()
