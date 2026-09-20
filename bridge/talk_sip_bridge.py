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
import secrets
import urllib.error
import urllib.parse
import urllib.request

from config import config

# The endpoint validates the checksum against the dialled number; other
# SIP-bridge endpoints use the room token. The shared secret is Talk's
# sip_bridge_shared_secret.
DIRECT_DIAL_IN = "/ocs/v2.php/apps/spreed/api/v4/room/direct-dial-in"


def bridge_headers(secret: str, data: str) -> dict:
    """What marks a request as coming from a SIP bridge.

    At least 32 characters of randomness, and the SHA256-HMAC of that
    randomness followed by the data the endpoint checks against."""
    random = secrets.token_hex(32)
    checksum = hmac.new(secret.encode(), (random + data).encode(), hashlib.sha256).hexdigest()
    return {"Talk-SIPBridge-Random": random, "Talk-SIPBridge-Checksum": checksum}


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
    body = urllib.parse.urlencode({"phoneNumber": dialled, "caller": caller}).encode()
    headers = {"OCS-APIREQUEST": "true", "Accept": "application/json",
               "Content-Type": "application/x-www-form-urlencoded",
               **bridge_headers(secret, dialled)}
    request = urllib.request.Request(config.backend_url.rstrip("/") + DIRECT_DIAL_IN,
                                     data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())["ocs"]["data"]
    except urllib.error.HTTPError as e:
        reason = {401: "the SIP bridge secret was not accepted",
                  404: f"no Nextcloud account is mapped to {dialled} "
                       f"(occ talk:phone-number:add)",
                  501: "SIP is not configured in Talk"}.get(e.code, f"HTTP {e.code}")
        print(f"[talk] No dial-in conversation for a call to {dialled}: {reason}")
        return None
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        print(f"[talk] Could not ask for a dial-in conversation for {dialled}: {e!r}")
        return None
    if not data or not data.get("token"):
        return None
    return data
