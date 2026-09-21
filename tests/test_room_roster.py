"""Who in a room is somebody to subscribe to.

The bridge takes the caller's audio from one other participant in the
room, and it picks that participant from the room roster. Everything in
that roster that is not a person has to be recognised as such: the
phones this bridge announced, and the bridge's own connections.

Getting it wrong is silent. A subscription to the bridge's own session
negotiates, connects and carries nothing, so the call is up, the caller
is heard in Talk, and nobody in Talk is heard on the phone.

The join entries here are the shapes a live server sent, down to which
fields are absent.
"""
import unittest

from tests.support import needs_media_stack

try:
    import talk_client
    import talk_messages
    from call import DIALOUT, Call
except ImportError:  # no media stack; every test here is skipped
    talk_client = None


OWN_ROOM_SESSION = "Md0YMJ2yQO3aGeTpzueWwHbS9rQ49jCvWqYKVq85tP32gyxU"
HUMAN_SESSION = "8Td-GS0EtQpgYvlozxrA8UPtN6nLyoVqimLy0fsP4fKEIyRKc22F"


def human(sessionid=HUMAN_SESSION, userid="schnebeck"):
    """A Talk client in a browser."""
    return {"sessionid": sessionid, "userid": userid, "features": ["chat-relay"],
            "user": {"displayname": "Thorsten Schnebeck"},
            "roomsessionid": "IbF1DXckdwSaL6UpfcV4zH"}


def guest(sessionid="guest-1"):
    """No user id, and none of the features a bridge declares."""
    return {"sessionid": sessionid, "userid": "", "features": ["chat-relay"],
            "user": {"displayname": "Gast"}}


def internal(sessionid, features):
    """One of this bridge's own connections, as the room announces it."""
    return {"sessionid": sessionid, "userid": "", "features": list(features)}


def phone(sessionid="Qc1WhHDNFszqW48r", call_id="call-1", number="620"):
    return {"sessionid": sessionid, "userid": "",
            "user": {"type": "phone", "callid": call_id,
                     "number": number, "displayname": number}}


def roster_of(client, *entries):
    client._handle_room_join(list(entries))
    return {sessionid: state["is_human"] for sessionid, state in client._room_roster.items()}


@needs_media_stack
class RosterTest(unittest.TestCase):
    def client(self):
        client = talk_client.TalkClient(call_manager=None)
        client.own_sessionid = OWN_ROOM_SESSION
        return client

    def test_a_person_is_somebody_to_subscribe_to(self):
        self.assertEqual(roster_of(self.client(), human()), {HUMAN_SESSION: True})

    def test_a_guest_is_too(self):
        """No user id of their own, which is the one thing they share
        with an internal client - so that cannot be the test."""
        self.assertEqual(roster_of(self.client(), guest()), {"guest-1": True})

    def test_the_bridges_own_room_connection_is_not(self):
        """The regression this file exists for. It declares only
        `internal-incall`, and a check for `start-dialout` alone took it
        for a person: the bridge then asked itself for the room's audio,
        the subscription connected, and nothing from Talk reached the
        phone."""
        entry = internal(OWN_ROOM_SESSION, talk_messages.ROOM_FEATURES)
        self.assertEqual(roster_of(self.client(), entry), {OWN_ROOM_SESSION: False})

    def test_the_bridges_own_dialout_connection_is_not(self):
        entry = internal("dialout-session", talk_messages.DIALOUT_FEATURES)
        self.assertEqual(roster_of(self.client(), entry), {"dialout-session": False})

    def test_no_connection_this_bridge_opens_is_ever_a_person(self):
        """Whatever roles exist, and whatever each declares. A role
        added later must not be able to bring the bug back."""
        for features in (talk_messages.DIALOUT_FEATURES, talk_messages.ROOM_FEATURES,
                         list(talk_messages.INTERNAL_FEATURES)):
            with self.subTest(features=features):
                entry = internal("some-bridge-session", features)
                self.assertEqual(roster_of(self.client(), entry),
                                 {"some-bridge-session": False})

    def test_our_own_session_is_not_a_person_however_it_is_announced(self):
        """The server announces a session's features as that session
        declared them; our own id is the one thing we always know."""
        entry = {"sessionid": OWN_ROOM_SESSION, "userid": "", "features": []}
        self.assertEqual(roster_of(self.client(), entry), {OWN_ROOM_SESSION: False})

    def test_a_phone_is_not_a_person(self):
        """It carries no media at all - a virtual session cannot."""
        self.assertEqual(roster_of(self.client(), phone()), {"Qc1WhHDNFszqW48r": False})

    def test_everyone_arriving_together_is_classified_apart(self):
        """One event carries several entries; joining a room in progress
        delivers the whole room at once."""
        self.assertEqual(
            roster_of(self.client(), human(), internal(OWN_ROOM_SESSION,
                                                       talk_messages.ROOM_FEATURES), phone()),
            {HUMAN_SESSION: True, OWN_ROOM_SESSION: False, "Qc1WhHDNFszqW48r": False})

    def test_an_entry_without_a_session_id_is_skipped(self):
        self.assertEqual(roster_of(self.client(), {"userid": "nobody"}), {})


@needs_media_stack
class PhoneSessionTest(unittest.TestCase):
    """The room session id the server assigns a phone is unrelated to
    the name the bridge chose for it, and arrives only here. Without it
    the bridge cannot tell its own phone from a participant still on the
    call."""

    def test_the_phones_room_session_id_is_kept_on_its_call(self):
        client = talk_client.TalkClient(call_manager=None)
        client.own_sessionid = OWN_ROOM_SESSION
        client._call_sessions["call-1"] = Call(sip_call_id="call-1", kind=DIALOUT,
                                               number="620", roomid="room-token")
        client._handle_room_join([phone(sessionid="server-chosen", call_id="call-1")])
        self.assertEqual(client._call_sessions["call-1"].virtual_room_sessionid,
                         "server-chosen")

    def test_a_phone_for_a_call_that_is_gone_is_ignored(self):
        client = talk_client.TalkClient(call_manager=None)
        client.own_sessionid = OWN_ROOM_SESSION
        client._handle_room_join([phone(call_id="a-call-that-ended")])
        self.assertEqual(client._call_sessions, {})


if __name__ == "__main__":
    unittest.main()
