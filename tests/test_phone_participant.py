"""What the bridge announces about the phone, as messages on the wire.

Separate from test_talk_messages.py on purpose: that file asserts the
shape of a message built from arguments, this one asserts what the
bridge decides to send about a call it is carrying - which name, to
whom, and with which state.
"""
import json
import threading
import unittest

from tests.support import needs_media_stack


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
