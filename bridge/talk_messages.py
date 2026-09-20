"""The messages this bridge sends to the standalone signaling server.

Every one of them is a shape the server either recognises or ignores
without a word - a reply in the wrong wrapper produces no error, just a
dialout that never happens. Building them in one place keeps those shapes
next to each other and next to the documentation that describes them
(docs/SIGNALING-API.md), instead of spread through the code that decides
when to send what.

Dicts in, dicts out: nothing here has a connection or any state.
"""
import hashlib
import hmac
import secrets

# Call flags, shared by Talk's clients and the signaling server. Talk's own
# clients only ever subscribe to a participant carrying AUDIO or VIDEO
# (spreed's webrtc.js: userHasStreams()), which is what makes the
# distinction matter rather than being cosmetic.
FLAG_IN_CALL = 1
FLAG_WITH_AUDIO = 2
FLAG_WITH_PHONE = 8

# What this client declares it can do. "start-dialout" makes it a
# candidate for dialout requests; "internal-incall" hands it responsibility
# for its own in-call flags, without which the server announces audio from
# the moment it connects and every Talk client asks for a stream that does
# not exist yet.
FEATURES = ["start-dialout", "internal-incall"]

ROOM_REQUEST_ID = "bridge-room"


def hello(internal_secret: str, backend_url: str, request_id: str = "bridge-hello") -> dict:
    """Authenticates as an internal client. `backend` is required and is
    documented nowhere - it comes from the signaling server's own source."""
    random_str = secrets.token_hex(32)
    token = hmac.new(internal_secret.encode(), random_str.encode(), hashlib.sha256).hexdigest()
    return {
        "id": request_id, "type": "hello",
        "hello": {
            "version": "1.0",
            "features": list(FEATURES),
            "auth": {"type": "internal",
                     "params": {"random": random_str, "token": token, "backend": backend_url}},
        },
    }


def join_room(roomid: str) -> dict:
    """Joining is what makes the server route this client's own offer to
    the room's Janus. It also costs the connection its dialout eligibility
    for good - see docs/CONCEPT.md point 3."""
    return {"id": ROOM_REQUEST_ID, "type": "room", "room": {"roomid": roomid}}


def set_incall(flags: int) -> dict:
    return {"type": "internal", "internal": {"type": "incall", "incall": {"incall": flags}}}


def add_session(sessionid: str, roomid: str, call_id: str, number: str, displayname: str) -> dict:
    """The phone participant Talk shows in the room. A name plate only: a
    virtual session can never carry media, so it is announced without
    WITH_AUDIO - pointing clients at a stream that cannot exist leaves them
    retrying against a silent tile forever."""
    return {
        "type": "internal",
        "internal": {
            "type": "addsession",
            "addsession": {
                "sessionid": sessionid,
                "roomid": roomid,
                "incall": FLAG_IN_CALL | FLAG_WITH_PHONE,
                # No actor here: the signaling server would register this
                # session with Nextcloud as that actor, and Nextcloud
                # rejects one that is not already invited to the room -
                # failing the whole addsession. displayname is the field
                # Talk renders participants by.
                "user": {"type": "phone", "callid": call_id,
                         "number": number, "displayname": displayname},
            },
        },
    }


def remove_session(sessionid: str, roomid: str) -> dict:
    return {"type": "internal",
            "internal": {"type": "removesession",
                         "removesession": {"sessionid": sessionid, "roomid": roomid}}}


def publish_offer(own_sessionid: str, sip_call_id: str, sdp: str, nick: str) -> dict:
    """Addressed to our own session: a virtual session has no client that
    could answer an offer, so publishing happens as ourselves. `nick` names
    the tile that actually carries the call's audio."""
    return {
        "id": f"bridge-offer-{sip_call_id}", "type": "message",
        "message": {
            "recipient": {"type": "session", "sessionid": own_sessionid},
            "data": {
                "to": own_sessionid, "type": "offer", "sid": secrets.token_hex(8),
                "roomType": "video",
                "payload": {"nick": nick, "type": "offer", "sdp": sdp},
                "audiocodec": "opus",
            },
        },
    }


def request_offer(sip_call_id: str, human_sessionid: str) -> dict:
    """Asks for another participant's stream. The server answers
    "client_not_found" rather than queuing if their publisher does not
    exist yet, so this gets repeated."""
    return {
        "id": f"bridge-reqoffer-{sip_call_id}", "type": "message",
        "message": {
            "recipient": {"type": "session", "sessionid": human_sessionid},
            "data": {"type": "requestoffer", "roomType": "video"},
        },
    }


def subscribe_answer(peer_sessionid: str, sid, sdp: str) -> dict:
    return {
        "id": f"bridge-subanswer-{secrets.token_hex(4)}", "type": "message",
        "message": {
            "recipient": {"type": "session", "sessionid": peer_sessionid},
            "data": {"to": peer_sessionid, "type": "answer", "sid": sid, "roomType": "video",
                     "payload": {"type": "answer", "sdp": sdp}},
        },
    }


def dialout_status(roomid: str, call_id: str, status: str, request_id: str = "") -> dict:
    """The reply to a dialout request, and the later unsolicited updates.
    The `internal` wrapper and the echoed id are what make the server read
    it at all - a flat reply is dropped without a word."""
    return _dialout(roomid, {"type": "status", "status": {"callid": call_id, "status": status}},
                    request_id)


def dialout_error(roomid: str, message: str, request_id: str = "", code: str = "call_failed") -> dict:
    return _dialout(roomid, {"type": "error", "error": {"code": code, "message": message}},
                    request_id)


def _dialout(roomid: str, payload: dict, request_id: str) -> dict:
    envelope = {"type": "internal",
                "internal": {"type": "dialout", "dialout": {"roomid": roomid, **payload}}}
    if request_id:
        envelope["id"] = request_id
    return envelope
