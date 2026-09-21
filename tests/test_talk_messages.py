"""The shapes this bridge sends to the signaling server.

The server does not complain about a message it does not recognise - it
ignores it. A wrong wrapper therefore looks like nothing happening, which
is why these shapes are asserted field by field rather than eyeballed
against the documentation they came from (docs/SIGNALING-API.md).
"""
import hashlib
import hmac
import json
import threading
import unittest

from tests.support import needs_media_stack
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

    def test_each_connection_declares_only_its_own_role(self):
        """The two cannot be combined: a "start-dialout" session that
        joins a room is dropped from the server's dialout candidates for
        good, and only a fresh hello puts it back. So one connection
        takes dialout requests and never enters a room, and the other
        owns its in-call flags because it publishes the call's audio."""
        dialout = m.hello(SECRET, BACKEND, m.DIALOUT_FEATURES)
        room = m.hello(SECRET, BACKEND, m.ROOM_FEATURES)
        self.assertEqual(dialout["hello"]["features"], ["start-dialout"])
        self.assertEqual(room["hello"]["features"], ["internal-incall"])
        self.assertNotIn("start-dialout", room["hello"]["features"],
                         "the room connection would lose this on its first join anyway")

    def test_the_declared_features_cannot_be_mutated_through_the_message(self):
        m.hello(SECRET, BACKEND, m.ROOM_FEATURES)["hello"]["features"].append("nonsense")
        self.assertNotIn("nonsense", m.ROOM_FEATURES)


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

    def test_a_caller_nextcloud_knows_is_not_flagged_as_a_phone(self):
        """The phone flag says "a telephone, not a client that sent
        nothing". With an actor, Nextcloud already knows who this is, and
        the flag only adds the state clients render for a call still
        being placed - an hourglass that never clears."""
        message = m.add_session("phone-1", "room-token", call_id="c", number="+4930622",
                                displayname="+4930622", with_audio=True,
                                actor={"actorType": "guests", "actorId": "abc"})
        incall = message["internal"]["addsession"]["incall"]
        self.assertFalse(incall & m.FLAG_WITH_PHONE)
        self.assertTrue(incall & m.FLAG_IN_CALL)
        self.assertTrue(incall & m.FLAG_WITH_AUDIO)

    def test_a_caller_nextcloud_does_not_know_still_is(self):
        """Without an actor the session belongs to nobody, and the flag
        is the only thing saying what it is."""
        message = m.add_session("phone-1", "room-token", call_id="c",
                                number="+4930622", displayname="+4930622")
        self.assertTrue(message["internal"]["addsession"]["incall"] & m.FLAG_WITH_PHONE)

    def test_the_call_id_round_trips_so_the_session_can_be_recognised(self):
        """The server assigns its own room session id; this is what maps it
        back to the call."""
        message = m.add_session("phone-1", "room-token", call_id="call-42",
                                number="622", displayname="622")
        self.assertEqual(message["internal"]["addsession"]["user"]["callid"], "call-42")

    def test_no_actor_is_claimed_for_a_caller_nextcloud_does_not_know(self):
        """Nextcloud looks the room up by the actor and fails the whole
        addsession when it finds none - so an actor is named only when
        direct dial-in has made the caller a participant."""
        message = m.add_session("phone-1", "room-token", call_id="c", number="1", displayname="1")
        self.assertNotIn("options", message["internal"]["addsession"])

    def test_an_actor_is_named_when_there_is_one(self):
        actor = {"actorType": "guests", "actorId": "abc"}
        message = m.add_session("phone-1", "room-token", call_id="c", number="1",
                                displayname="1", actor=actor)
        self.assertEqual(message["internal"]["addsession"]["options"], actor)

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

    def test_an_empty_room_id_leaves_the_room(self):
        """There is no separate leave message; a session is in one room
        at a time and an empty id means none."""
        self.assertEqual(m.leave_room()["room"], {"roomid": ""})
        self.assertEqual(m.leave_room()["id"], m.ROOM_REQUEST_ID)

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


class InCallTest(unittest.TestCase):
    """Getting the phone into the room's call, not just into the room.

    Read in the signaling server's source (v2.1.1): `Room.AddSession`
    files a virtual session under the room's sessions and its virtual
    sessions, and never under `inCallSessions`. Only a participants
    update from Nextcloud or a *change* via "updatesession" puts it
    there - and `isInSameCall` refuses every client that asks for the
    stream of a session that is not in that set.
    """

    def test_joining_announces_being_in_the_call(self):
        message = m.add_session("phone-1", "room-token", call_id="c", number="+4930622",
                                displayname="+4930622", incall=m.FLAG_IN_CALL)
        self.assertEqual(message["internal"]["addsession"]["incall"], m.FLAG_IN_CALL)

    def test_the_update_carries_the_full_state(self):
        message = m.update_session("phone-1", "room-token", m.FLAG_IN_CALL | m.FLAG_WITH_AUDIO)
        update = message["internal"]["updatesession"]
        self.assertEqual(message["internal"]["type"], "updatesession")
        self.assertEqual(update["sessionid"], "phone-1")
        self.assertEqual(update["roomid"], "room-token")
        self.assertEqual(update["incall"], m.FLAG_IN_CALL | m.FLAG_WITH_AUDIO)

    def test_the_update_differs_from_what_was_announced(self):
        """A value that does not change is not a change: the server's
        SetInCall reports nothing, the room is never notified, and the
        session stays outside the call."""
        for with_audio, actor in ((True, {"actorType": "guests", "actorId": "x"}),
                                  (True, None), (False, None)):
            with self.subTest(with_audio=with_audio, actor=bool(actor)):
                self.assertNotEqual(m.incall_flags(with_audio, actor), m.FLAG_IN_CALL)

    def test_a_known_caller_is_in_the_call_with_audio_and_no_phone_flag(self):
        flags = m.incall_flags(True, {"actorType": "guests", "actorId": "x"})
        self.assertEqual(flags, m.FLAG_IN_CALL | m.FLAG_WITH_AUDIO)


class TalkingFlagsTest(unittest.TestCase):
    """The phone's own state, as the server publishes it.

    These flags are the only channel that says something about the
    phone: the server turns them into a "participants"/"flags" event
    naming the phone's session, while a message this bridge sends is
    stamped with the bridge's session id (hub.go stamps
    Sender.SessionId) and belongs to no tile a client draws.
    """

    def test_speaking_is_announced_for_the_phones_own_session(self):
        message = m.update_session("phone-1", "room-token", flags=m.FLAG_TALKING)
        update = message["internal"]["updatesession"]
        self.assertEqual(update["sessionid"], "phone-1")
        self.assertEqual(update["flags"], m.FLAG_TALKING)
        self.assertNotIn("incall", update, "the call state is not touched by speaking")

    def test_falling_silent_clears_the_flags_without_muting(self):
        """Zero is "not speaking", not "microphone off" - that would be
        FLAG_MUTED_SPEAKING, which a telephone never sets."""
        update = m.update_session("phone-1", "room-token", flags=0)["internal"]["updatesession"]
        self.assertEqual(update["flags"], 0)
        self.assertFalse(update["flags"] & m.FLAG_MUTED_SPEAKING)

    def test_the_two_kinds_of_state_can_be_sent_apart(self):
        only_call = m.update_session("p", "r", incall=m.FLAG_IN_CALL)["internal"]["updatesession"]
        self.assertNotIn("flags", only_call)


class PeerStateTest(unittest.TestCase):
    """What a client tells the others about itself. Talk's own does this
    on joining and for everyone who joins later; a participant that says
    nothing is rendered from guesswork."""

    def state(self, kind="unmute", payload=None):
        return m.peer_state("their-session", kind, payload or {"name": "audio"})

    def test_it_goes_to_one_participant(self):
        message = self.state()
        self.assertEqual(message["message"]["recipient"],
                         {"type": "session", "sessionid": "their-session"})
        self.assertEqual(message["message"]["data"]["to"], "their-session")

    def test_it_carries_the_state_and_what_it_is_about(self):
        data = self.state("mute", {"name": "video"})["message"]["data"]
        self.assertEqual(data["type"], "mute")
        self.assertEqual(data["payload"], {"name": "video"})
        self.assertEqual(data["roomType"], "video")

    def test_a_name_is_sent_the_same_way(self):
        data = self.state("nickChanged", {"name": "+4930622"})["message"]["data"]
        self.assertEqual(data["type"], "nickChanged")
        self.assertEqual(data["payload"]["name"], "+4930622")


@needs_media_stack
class AnnouncedStateTest(unittest.TestCase):
    """What the bridge actually announces for a call."""

    def announce(self, peers=("person-1",), number='"FritzFon" <sip:**611@fritz.box>'):
        import asyncio
        from unittest import mock
        import phone_participant
        import talk_client
        from call import Call, INBOUND

        client = talk_client.TalkClient.__new__(talk_client.TalkClient)
        client.own_sessionid = "bridge-session"
        client._call_sessions_lock = threading.Lock()
        entry = Call(sip_call_id="c1", kind=INBOUND, number=number)
        entry.media = mock.Mock(is_publishing=True)
        entry.virtual_sessionid = "phone-1"
        client._call_sessions = {"c1": entry}
        sent = []
        client.ws = mock.Mock(send=lambda raw: sent.append(json.loads(raw)))

        async def send(raw):
            sent.append(json.loads(raw))

        client.ws.send = send
        phone = phone_participant.PhoneParticipant(client)
        asyncio.run(phone.announce_state("c1", peers=set(peers)))
        return [(msg["message"]["data"]["to"], msg["message"]["data"]["type"],
                 msg["message"]["data"]["payload"]) for msg in sent]

    def test_a_phone_is_named_by_its_number_not_by_the_handset(self):
        """"FritzFon schwarz" is what the box calls the device; the room
        wants to know who is calling."""
        named = [payload["name"] for _, kind, payload in self.announce()
                 if kind == "nickChanged"]
        self.assertEqual(named, ["**611"])

    def test_a_caller_with_no_number_keeps_whatever_name_there_is(self):
        named = [payload["name"] for _, kind, payload
                 in self.announce(number='"Somebody" <sip:@gw>') if kind == "nickChanged"]
        self.assertEqual(named, ["Somebody"])

    def test_a_phone_is_unmuted_without_video_and_named(self):
        self.assertEqual(self.announce(),
                         [("person-1", "unmute", {"name": "audio"}),
                          ("person-1", "mute", {"name": "video"}),
                          ("person-1", "nickChanged", {"name": "**611"})])

    def test_everyone_named_is_told(self):
        told = {peer for peer, _, _ in self.announce(peers=("a", "b"))}
        self.assertEqual(told, {"a", "b"})

    def test_the_bridge_does_not_tell_itself(self):
        self.assertEqual(self.announce(peers=("bridge-session", "phone-1")), [])


if __name__ == "__main__":
    unittest.main()
