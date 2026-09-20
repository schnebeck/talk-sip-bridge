"""Talking to Nextcloud as a SIP bridge rather than as a user.

Talk has a second door for telephony backends: requests carrying
`Talk-SIPBridge-Random` and `Talk-SIPBridge-Checksum`, signed with the
shared SIP secret, are let in without a user account and without being a
member of anything. Its own SIP bridge uses it, and behind it sits the one
call this bridge cannot make as a user - direct dial-in, where Nextcloud
creates the conversation for an incoming call and makes the caller a real
participant of it.

That participant is the point. A caller announced only as a virtual
session exists in the signaling server alone: Nextcloud has never heard of
them, so nothing can name them as an actor and Talk's clients are left
with a session that belongs to nobody. See docs/SIGNALING-API.md.
"""
import hashlib
import hmac
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request

from config import config

API = "/ocs/v2.php/apps/spreed/api/v4"
# The endpoint validates the checksum against the dialled number; the two
# below use the room token. The shared secret is Talk's
# sip_bridge_shared_secret.
DIRECT_DIAL_IN = f"{API}/room/direct-dial-in"
OPEN_DIAL_IN = f"{API}/room/{{token}}/open-dial-in"
VERIFY_DIAL_IN = f"{API}/room/{{token}}/verify-dialin"

# What a caller is allowed to have typed. Both of these go into a URL and
# a signature, and both are keyed in by whoever called - a token with a
# slash in it would address a different endpoint entirely. Talk's own
# route accepts [a-z0-9]{4,30}; a conversation reachable by telephone has
# a digits-only token (Manager::getNewToken), and nothing else can be
# dialled on a keypad anyway.
MEETING_ID = re.compile(r"[0-9]{4,30}")
PIN = re.compile(r"[0-9]{3,32}")

# What asking about a meeting id can end in. The caller hears a different
# tone for each, and only OK carries a room.
OK = "ok"
NEEDS_PIN = "needs-pin"
UNKNOWN = "unknown"        # no such conversation, or a PIN nobody has
REFUSED = "refused"        # this bridge is not allowed to ask at all


def bridge_headers(secret: str, data: str) -> dict:
    """What marks a request as coming from a SIP bridge.

    At least 32 characters of randomness, and the SHA256-HMAC of that
    randomness followed by the data the endpoint checks against."""
    random = secrets.token_hex(32)
    checksum = hmac.new(secret.encode(), (random + data).encode(), hashlib.sha256).hexdigest()
    return {"Talk-SIPBridge-Random": random, "Talk-SIPBridge-Checksum": checksum}


def _post(path: str, signed_over: str, form: dict, secret: str, timeout: float):
    """One signed POST. Returns (status, data) - status 0 when the request
    never got an answer at all, which is not the same as a refusal."""
    body = urllib.parse.urlencode(form).encode()
    headers = {"OCS-APIREQUEST": "true", "Accept": "application/json",
               "Content-Type": "application/x-www-form-urlencoded",
               **bridge_headers(secret, signed_over)}
    request = urllib.request.Request(config.backend_url.rstrip("/") + path,
                                     data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())["ocs"]["data"]
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        print(f"[talk] {path} did not answer: {e!r}")
        return 0, None


def direct_dial_in(dialled: str, caller: str, secret: str = None, timeout: float = 5.0):
    """Asks Nextcloud for a conversation for an incoming call.

    Returns the room as Nextcloud describes it - token, the caller's
    actor, their session - or None when there is no room to be had: no
    secret configured, the dialled number mapped to nobody (404), SIP not
    configured (501). A caller whose number is unknown to Nextcloud is not
    an error to shout about; it just means this call has to fall back to
    the room the line is configured with."""
    secret = secret if secret is not None else config.sip_shared_secret
    if not secret:
        return None
    status, data = _post(DIRECT_DIAL_IN, dialled,
                         {"phoneNumber": dialled, "caller": caller}, secret, timeout)
    if status != 200:
        reason = {401: "the SIP bridge secret was not accepted",
                  404: f"no Nextcloud account is mapped to {dialled} "
                       f"(occ talk:phone-number:add)",
                  501: "SIP is not configured in Talk",
                  0: "the request did not get through"}.get(status, f"HTTP {status}")
        print(f"[talk] No dial-in conversation for a call to {dialled}: {reason}")
        return None
    if not data or not data.get("token"):
        return None
    return data


def join_by_meeting_id(token: str, pin: str = None, secret: str = None, timeout: float = 5.0):
    """Lets a caller who typed a meeting id into the conversation it names.

    Returns (outcome, room). Two endpoints stand behind this, and which
    one applies is the conversation's own setting rather than anything the
    caller can say:

    - a conversation that takes callers without a PIN admits them as
      guests (`open-dial-in`), which is the only one of the two that
      creates a participant,
    - a conversation that wants a PIN has the participant already, and
      the PIN says which one they are (`verify-dialin`). The caller then
      appears under their own name rather than as a guest.

    NEEDS_PIN is the answer to a meeting id alone when the conversation
    wants a PIN - ask for one and call again with it. UNKNOWN covers both
    "no such conversation" and "no participant has that PIN": telling a
    caller which of the two it was would let them find out which meeting
    ids exist."""
    secret = secret if secret is not None else config.sip_shared_secret
    if not secret:
        return REFUSED, None
    if not MEETING_ID.fullmatch(token or ""):
        return UNKNOWN, None
    if pin is not None and not PIN.fullmatch(pin):
        return UNKNOWN, None

    if pin is None:
        path, form = OPEN_DIAL_IN.format(token=token), {}
    else:
        path, form = VERIFY_DIAL_IN.format(token=token), {"pin": pin}
    status, data = _post(path, token, form, secret, timeout)
    if status == 200 and data and data.get("token"):
        return OK, data
    if status == 400 and pin is None:
        # The conversation is SIP-enabled but not without a PIN.
        return NEEDS_PIN, None
    if status == 401:
        print("[talk] The SIP bridge secret was not accepted for a dial-in")
        return REFUSED, None
    if status in (403, 404, 200):
        return UNKNOWN, None
    print(f"[talk] Dial-in for meeting {token} answered HTTP {status}")
    return UNKNOWN, None
