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

# What a connection declares it can do, per role. The bridge keeps two,
# because one connection cannot hold both: the server drops a
# "start-dialout" session from its dialout candidates the moment it joins
# any room, and puts it back only on a fresh hello (hub.go).
#
# "start-dialout" makes a connection a candidate for dialout requests;
# "internal-incall" hands it responsibility for its own in-call flags,
# without which the server announces audio from the moment it connects
# and every Talk client asks for a stream that does not exist yet.
DIALOUT_FEATURES = ["start-dialout"]
ROOM_FEATURES = ["internal-incall"]
# Neither is a feature any Talk client declares, which is what makes them
# usable the other way round: a room roster entry carrying one of them is
# a bridge, not somebody to subscribe to.
INTERNAL_FEATURES = frozenset(DIALOUT_FEATURES) | frozenset(ROOM_FEATURES)

ROOM_REQUEST_ID = "bridge-room"


def hello(internal_secret: str, backend_url: str, features: list = None,
          request_id: str = "bridge-hello") -> dict:
    """Authenticates as an internal client. `backend` is required and is
    documented nowhere - it comes from the signaling server's own source.

    `features` is what this connection is for: DIALOUT_FEATURES or
    ROOM_FEATURES."""
    random_str = secrets.token_hex(32)
    token = hmac.new(internal_secret.encode(), random_str.encode(), hashlib.sha256).hexdigest()
    return {
        "id": request_id, "type": "hello",
        "hello": {
            "version": "1.0",
            "features": list(features if features is not None else ROOM_FEATURES),
            "auth": {"type": "internal",
                     "params": {"random": random_str, "token": token, "backend": backend_url}},
        },
    }


def join_room(roomid: str) -> dict:
    """Joining is what makes the server route this client's own offer to
    the room's Janus, and what makes the connection a member of the room
    for events - only a real client session is told when the call ends
    for everyone (room.go).

    It also costs the connection its dialout eligibility for good, which
    is why only the room connection ever sends this - see
    docs/CONCEPT.md point 3."""
    return {"id": ROOM_REQUEST_ID, "type": "room", "room": {"roomid": roomid}}


def leave_room() -> dict:
    """An empty room id leaves whatever room the connection is in. A
    session can only be in one room at a time, so this matters less for
    the next call than for not lingering in a conversation that is over:
    the server counts the connection among the room's sessions until it
    goes."""
    return {"id": ROOM_REQUEST_ID, "type": "room", "room": {"roomid": ""}}


def set_incall(flags: int) -> dict:
    return {"type": "internal", "internal": {"type": "incall", "incall": {"incall": flags}}}


# What a virtual session says about its microphone, from the signaling
# server's virtualsession.go. These are the phone's own state, published
# by the server as a "participants"/"flags" event naming that session -
# the only channel that can say anything *about the phone*, since a
# message sent by this bridge is stamped with the bridge's own session
# id and belongs to no tile a client shows.
#
# A session whose flags are zero is also a session the server tells a
# newcomer nothing about (room.go skips flags == 0), which leaves clients
# to infer the microphone from the audio - seen as the muted marker
# appearing and disappearing with the caller's speech.
FLAG_MUTED_SPEAKING = 1     # the microphone is off
FLAG_MUTED_LISTENING = 2    # the loudspeaker is off
FLAG_TALKING = 4            # speaking right now


def incall_flags(with_audio: bool = False, actor: dict = None) -> int:
    """What the phone's session announces about itself.

    FLAG_WITH_PHONE is the state Talk renders for a call still being
    placed, so it is wrong for a caller Nextcloud already knows: they
    are in the room because the call went through. The actor is what
    says so."""
    return (FLAG_IN_CALL
            | (0 if actor else FLAG_WITH_PHONE)
            | (FLAG_WITH_AUDIO if with_audio else 0))


def update_session(sessionid: str, roomid: str, incall: int = None, flags: int = None) -> dict:
    """Changes a virtual session's in-call state after it exists.

    Needed for more than bookkeeping. Adding a session hands the server
    its flags, but `Room.AddSession` never puts a virtual session into
    the room's in-call set - only a participants update from Nextcloud
    or a *change* through this message does (hub.go's "updatesession" ->
    `NotifySessionChanged(SessionChangeInCall)` -> `addSessionToCall`).
    Until then `isInSameCall` refuses every client that asks for the
    phone's stream: "Session ... is not in the same call as session ...,
    not requesting offer". Measured against a live call, and read in the
    server's source at v2.1.1."""
    return {
        "type": "internal",
        "internal": {
            "type": "updatesession",
            "updatesession": {"sessionid": sessionid, "roomid": roomid,
                              **({"incall": incall} if incall is not None else {}),
                              **({"flags": flags} if flags is not None else {})},
        },
    }


def add_session(sessionid: str, roomid: str, call_id: str, number: str, displayname: str,
                with_audio: bool = False, actor: dict = None, incall: int = None) -> dict:
    """The phone participant Talk shows in the room.

    Normally a name plate only: a virtual session can never carry media,
    so announcing WITH_AUDIO points clients at a stream that cannot exist
    and leaves them retrying against a silent tile.

    with_audio exists because that was once thought to be a trade: Talk's
    clients build a peer only for a participant carrying audio or video,
    and the "waiting for someone" sound was believed to repeat for the
    length of a call without one. Measured again, it does not - the sound
    is capped at four plays and any arrival stops it, a silent phone
    included. What announcing audio does buy is an hourglass on a tile
    waiting for a stream that cannot come. See config.phone_participant."""
    return {
        "type": "internal",
        "internal": {
            "type": "addsession",
            "addsession": {
                "sessionid": sessionid,
                "roomid": roomid,
                # The phone flag says "this session is a telephone, not
                # a client that failed to send anything". Where Nextcloud
                # already knows the caller - dial-in makes them a real
                # participant - that is covered by the actor, and the
                # flag only adds the state Talk's clients render for a
                # call still being placed.
                #
                # What is announced here is what Nextcloud is told the
                # participant joined with; the rest follows in an
                # update_session, which is the only thing that puts the
                # session into the room's in-call set.
                "incall": incall if incall is not None else incall_flags(with_audio, actor),
                # An actor only when Nextcloud already knows the caller -
                # direct dial-in makes them a participant, and then the
                # signaling server can register this session as them.
                # Naming an actor Nextcloud cannot find fails the whole
                # addsession: it looks the room up by the actor. displayname
                # is the field Talk renders participants by.
                "user": {"type": "phone", "callid": call_id,
                         "number": number, "displayname": displayname},
                **({"options": actor} if actor else {}),
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


def subscribe_answer(peer_sessionid: str, sid, sdp: str, sip_call_id: str = "") -> dict:
    """The answer to an offer the server relayed.

    The request id names the call, because the answer can be refused -
    the server re-attaches a subscriber whose publisher is not sending
    yet, and an answer carrying the old handle's id is then rejected.
    Without the call in the id there is no way to tell which call has to
    ask again."""
    return {
        "id": f"bridge-subanswer-{sip_call_id or secrets.token_hex(4)}", "type": "message",
        "message": {
            "recipient": {"type": "session", "sessionid": peer_sessionid},
            "data": {"to": peer_sessionid, "type": "answer", "sid": sid, "roomType": "video",
                     "payload": {"type": "answer", "sdp": sdp}},
        },
    }


def peer_state(peer_sessionid: str, state: str, payload: dict) -> dict:
    """What a Talk client tells the others about itself.

    Read off Talk's own client: on joining, and for every participant
    that joins later, it sends "unmute"/"mute" per media kind and
    "nickChanged" with its name, as plain peer messages. A participant
    that sends none of these leaves the others guessing - measured, the
    muted-microphone marker on the phone then comes and goes with the
    speech level, because there is no state to render.
    """
    return {
        "id": f"bridge-state-{secrets.token_hex(4)}", "type": "message",
        "message": {
            "recipient": {"type": "session", "sessionid": peer_sessionid},
            "data": {"to": peer_sessionid, "roomType": "video",
                     "type": state, "payload": payload},
        },
    }


def dialout_actor(options: dict) -> dict:
    """Who Nextcloud means by the number in a dialout request.

    Talk makes a "phones" attendee before it asks for the call and names
    it here (`SIPDialOutService`/`BackendNotifier::dialOutToAttendee`),
    alongside options this bridge has no use for. Passing it back on the
    virtual session is what makes Nextcloud aware of the phone at all:
    an addsession that names an actor is announced to the backend as a
    "room" request, which Talk answers by creating a session for that
    attendee, and only a participant it holds a session for can be
    disinvited when the call ends."""
    actor = {"actorType": options.get("actorType"), "actorId": options.get("actorId")}
    return actor if actor["actorType"] and actor["actorId"] else None


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
