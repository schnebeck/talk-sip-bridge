"""Talk's own OCS call API, for what the signaling protocol cannot express.

There is no ringing or accept-decline exchange for an inbound call in the
signaling protocol, so making a person's devices ring means acting as a
Talk client would: establish a room session, join the call, ring the
attendees. Joining the call without a room session first fails with 404.

Every call here is blocking HTTP and belongs on a worker thread. The room
session lives in a cookie jar, so the opener returned by start_ring has to
be handed back to stop_ring for Talk to attribute the leave correctly.
"""
import base64
import http.cookiejar
import json
import time
import urllib.error
import urllib.request

from config import config


def request(opener, base: str, auth_header: str, method: str, path: str, body: dict = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"OCS-APIREQUEST": "true", "Accept": "application/json", "Authorization": auth_header}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base}{path}", data=data, method=method, headers=headers)
    with opener.open(req, timeout=5) as resp:
        return resp.read()
def start_ring(roomid: str, nc_user: str, nc_app_password: str):
    """Uses Talk's own OCS call-signaling API (POST .../call/{token}) to
    make the bridge's Nextcloud account one more device joining the room's
    call - confirmed live that this is what actually triggers real ringing
    (push notification, full-screen call UI) on every other device logged
    into that account or already in the room, not just a chat message or a
    custom notification.

    Joining the call requires an existing room session first - confirmed
    live that calling the call endpoint directly, without having joined the
    room, fails with 404 (Talk's RequireParticipant check rejects it). The
    room join is cookie/session-based (like a browser), so the returned
    opener/cookie jar has to be kept and reused for the matching
    stop_ring call - joining a fresh session there and leaving
    immediately would end the call before anyone had a chance to answer it.

    Returns (opener, session_id) on success, or (None, None) if the feature
    isn't configured or the calls failed. session_id is the room session id
    Talk assigned to this triggering join (from the join-room response) -
    the signaling server broadcasts this same account joining the call to
    every room member including our own internal client, so it has to be
    recognized and excluded from "a human accepted" detection, or the
    bridge would immediately mistake its own ring-trigger for an accept."""
    if not nc_user or not nc_app_password:
        return None, None
    base = config.backend_url.rstrip('/')
    auth_header = "Basic " + base64.b64encode(f"{nc_user}:{nc_app_password}".encode()).decode()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        join_resp = request(opener, base, auth_header, "POST", f"/ocs/v2.php/apps/spreed/api/v4/room/{roomid}/participants/active", {})
        session_id = json.loads(join_resp)["ocs"]["data"].get("sessionId")
        started = time.monotonic()
        request(opener, base, auth_header, "POST", f"/ocs/v2.php/apps/spreed/api/v4/call/{roomid}", {"flags": 1})
        # This is the moment Talk clients start showing an incoming call.
        # A caller waits about twenty seconds, so how much of that this
        # takes is worth knowing on every call rather than guessing later.
        print(f"[talk] Talk is ringing for room {roomid} ({time.monotonic() - started:.1f}s to join the call)")
        _ring_attendees(opener, base, auth_header, roomid, nc_user)
        return opener, session_id
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        print(f"[talk] Starting Talk call ring for room {roomid} failed: {e!r}")
        return None, None
def _ring_attendees(opener, base: str, auth_header: str, roomid: str, nc_user: str):
    """Asks Talk to ring every real user in the room for the call that was
    just started. Joining the call alone does make Talk show an incoming
    call, but confirmed live: a client whose signaling session has gone
    stale still paints that screen and then does nothing when it is tapped -
    no session ever joins the call. This is Talk's own "ring a participant
    for the ongoing call" path and delivers a fresh call notification, which
    is what gets the app to open a live session again. Best effort: the
    join-driven ring stays in place regardless of what happens here."""
    try:
        raw = request(opener, base, auth_header, "GET",
                                f"/ocs/v2.php/apps/spreed/api/v4/room/{roomid}/participants")
        participants = json.loads(raw)["ocs"]["data"]
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        print(f"[talk] Could not list participants of room {roomid} to ring them: {e!r}")
        return
    for participant in participants:
        if participant.get("actorType") != "users" or participant.get("actorId") == nc_user:
            continue
        attendee_id = participant.get("attendeeId")
        if attendee_id is None:
            continue
        try:
            request(opener, base, auth_header, "POST",
                              f"/ocs/v2.php/apps/spreed/api/v4/call/{roomid}/ring/{attendee_id}", {})
            print(f"[talk] Rang {participant.get('actorId')} for the call in room {roomid}")
        except urllib.error.HTTPError as e:
            # Talk refuses with "status" for do-not-disturb and stays silent
            # for someone already in the call - neither is a bridge fault.
            print(f"[talk] Could not ring {participant.get('actorId')}: HTTP {e.code} {e.read()[:200]!r}")
        except (urllib.error.URLError, OSError) as e:
            print(f"[talk] Could not ring {participant.get('actorId')}: {e!r}")
def end_room_call(roomid: str, nc_user: str, nc_app_password: str):
    """Ends the room's call once the phone call behind it is over.

    The room's call only exists because this bridge started it for an
    inbound call, so it has to end with that call. Without this the person
    who answered is left alone in a call with nobody on the other end, and
    any call notification that went out for it stays alive with it.

    `all` goes in the body, which is where Talk's own client puts it and
    where its controller reads it from; as a query parameter it is ignored.

    Ending it for everyone also requires moderator rights in the room. An
    account without them is not refused - Talk's leaveCall falls through to
    disconnecting just this participant and answers 200, so the bridge's
    own participant leaves and the person who answered stays in a call
    with nobody on the other end. That is why the room is asked afterwards
    whether a call is still running rather than the 200 being believed."""
    base = config.backend_url.rstrip('/')
    auth_header = "Basic " + base64.b64encode(f"{nc_user}:{nc_app_password}".encode()).decode()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        request(opener, base, auth_header, "POST",
                          f"/ocs/v2.php/apps/spreed/api/v4/room/{roomid}/participants/active", {})
        request(opener, base, auth_header, "DELETE",
                          f"/ocs/v2.php/apps/spreed/api/v4/call/{roomid}", {"all": True})
        still_running = room_has_call(opener, base, auth_header, roomid)
        request(opener, base, auth_header, "DELETE",
                          f"/ocs/v2.php/apps/spreed/api/v4/room/{roomid}/participants/active")
        if still_running:
            print(f"[talk] Left the call in room {roomid}, but it is still running - "
                  f"ending it for everyone needs moderator rights for {nc_user} in that room "
                  f"(occ talk:room:promote)")
        else:
            print(f"[talk] Ended the call in room {roomid} - the phone call behind it is over")
    except urllib.error.HTTPError as e:
        print(f"[talk] Could not end the call in room {roomid}: HTTP {e.code} {e.read()[:200]!r}")
    except (urllib.error.URLError, OSError) as e:
        print(f"[talk] Could not end the call in room {roomid}: {e!r}")


def room_has_call(opener, base: str, auth_header: str, roomid: str):
    """Whether the room still has a call running, or None if the room did
    not say. Read back rather than assumed: the request to end the call
    answers 200 whether it ended it or only left it."""
    try:
        room = json.loads(request(opener, base, auth_header, "GET",
                                  f"/ocs/v2.php/apps/spreed/api/v4/room/{roomid}"))
        return room["ocs"]["data"].get("hasCall")
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return None
def stop_ring(opener, roomid: str, nc_user: str, nc_app_password: str):
    """Ends what start_ring started - leaves the call, then the
    room, using the same session (opener) so Talk attributes it to the
    right participant. This one only ever leaves: the call belongs to
    whoever answered it, not to the account that made it ring."""
    base = config.backend_url.rstrip('/')
    auth_header = "Basic " + base64.b64encode(f"{nc_user}:{nc_app_password}".encode()).decode()
    try:
        request(opener, base, auth_header, "DELETE",
                f"/ocs/v2.php/apps/spreed/api/v4/call/{roomid}", {"all": False})
        request(opener, base, auth_header, "DELETE", f"/ocs/v2.php/apps/spreed/api/v4/room/{roomid}/participants/active")
    except (urllib.error.URLError, OSError) as e:
        print(f"[talk] Stopping Talk call ring for room {roomid} failed: {e!r}")
