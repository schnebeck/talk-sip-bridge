# Talk signaling API reference

The protocol surface a SIP bridge needs from Nextcloud Talk: the standalone
signaling server's **internal client** interface (WebSocket) and the Talk
**OCS call API** (HTTPS). Nextcloud's own SIP bridge product is closed-source
and no reference implementation of this interface exists publicly, so what
follows is the working description this bridge is built against.

Official protocol documentation:
<https://nextcloud-spreed-signaling.readthedocs.io/en/latest/standalone-signaling-api-v1/>
(sections "Internal clients", "Client features", "Dialout session", "Start
dialout from a room", "Add/update/remove virtual session"). Everything marked
**undocumented** below is absent from that page and is taken from the
signaling server's Go source or from observed server behavior.

This document describes the interface. For why this bridge uses it the way it
does, see [`CONCEPT.md`](./CONCEPT.md); the authoritative implementation is
`bridge/talk_client.py`.

## Endpoints

| Purpose | Address |
|---|---|
| Signaling | The standalone signaling server's WebSocket, path `/spreed` |
| OCS call API | The Nextcloud instance, `/ocs/v2.php/apps/spreed/api/v4/...` |

Both are needed. The signaling server carries media negotiation and participant
state; it has **no** ringing/accept-decline exchange for inbound calls, so
alerting a person is only possible through the OCS API (see "Ringing" below).

## Prerequisites in Nextcloud

All native SIP endpoints are gated behind `Config::isSIPConfigured()` /
`isSIPDialOutEnabled()` (`spreed/lib/Config.php`). Without these `spreed` app
config values, Talk offers no "call a phone number" UI and rejects SIP-related
requests:

| Key | Requirement |
|---|---|
| `sip_bridge_shared_secret` | Non-empty. Authenticates the separate REST `/signaling/settings` SIP path; not used by the WebSocket dialout mechanism, but required for `isSIPConfigured()`. |
| `sip_bridge_dialin_info` | Non-empty. Free text shown to users. |
| `sip_dialout` | Anything but `no`, for `isSIPDialOutEnabled()`. |
| `sip_bridge_groups` | Optional. Restricts which groups may enable SIP per room; empty means all users. |

## Authentication: the internal client

An internal client is an external service that joins rooms without mapping to
a Nextcloud user. It may join **any** room without an invitation.

```json
{
  "id": "bridge-hello",
  "type": "hello",
  "hello": {
    "version": "1.0",
    "features": ["start-dialout", "internal-incall"],
    "auth": {
      "type": "internal",
      "params": {
        "random": "<random string, at least 32 bytes>",
        "token": "<hex HMAC-SHA256 of random, keyed with internalsecret>",
        "backend": "<Nextcloud instance URL>"
      }
    }
  }
}
```

- The key is the signaling server's own `internalsecret` (`server.conf`,
  `[clients]` section) — no separate credential to provision.
- **Undocumented:** `params.backend` is mandatory; the server rejects the hello
  without it (`ClientTypeInternalAuthParams.CheckValid()` in the Go source).
- The server sends a **welcome banner first**, then the hello response. The
  session id is `hello.sessionid` of the second message.

### Client features

| Feature | Effect |
|---|---|
| `start-dialout` | Makes this connection a candidate for dialout requests. Required to receive them at all. |
| `internal-incall` | Transfers ownership of the in-call flags to this client, for its own session **and** for the virtual sessions it adds. |

`internal-incall` is not optional in practice. Without it the server marks the
session as in-call **with audio** the moment it connects, so every Talk client
in the room immediately asks this session for an audio stream, receives
`client_not_found`, and backs off to one retry every 10 seconds — which costs a
real call its first seconds of audio.

## Message envelopes

Every message is a JSON object with a `type` and a same-named payload key.

| `type` | Direction | Purpose |
|---|---|---|
| `hello` | out | Authentication (above) |
| `room` | out / in | Join a room; the reply echoes the request `id` |
| `internal` | both | `dialout`, `incall`, `addsession`, `updatesession`, `removesession` |
| `message` | both | WebRTC negotiation between sessions (`offer`, `answer`, `candidate`, `requestoffer`) |
| `event` | in | Room and participant state (`room`/`join`, `room`/`leave`, `participants`/`update`) |
| `control` | in | Server-side instructions, notably `hangup` |
| `error` | in | Correlated to a request `id` where one was sent |

## Rooms

```json
{"id": "bridge-room", "type": "room", "room": {"roomid": "<token>"}}
```

- A successful join is answered with a `room` message carrying the same `id`.
- Re-joining a room this session is already in returns `error` with code
  **`already_joined`** instead. That is equivalent to success — a session that
  never explicitly leaves will meet this on every subsequent call.
- **Joining a room permanently costs dialout eligibility.** The server removes
  any `start-dialout` session from its dialout candidates as soon as it joins a
  room, and there is no code path that restores it — leaving the room again does
  not. The only way back is a **new connection with a fresh hello**. Any client
  that both publishes media and accepts dialout requests must therefore
  reconnect between calls.

## In-call flags

Bit flags shared by Talk's clients and the server:

| Name | Value |
|---|---|
| `IN_CALL` | 1 |
| `WITH_AUDIO` | 2 |
| `WITH_PHONE` | 8 |

Announced for the client's own session with:

```json
{"type": "internal", "internal": {"type": "incall", "incall": {"incall": 3}}}
```

Talk's clients subscribe only to participants carrying `WITH_AUDIO` or video
(`spreed`'s `webrtc.js`, `userHasStreams()`). Set `WITH_AUDIO` **only once the
publisher exists**, and clear the flags when the call ends; otherwise clients
chase a stream that is not there.

## Virtual sessions

A virtual session is how a phone participant appears in the room's participant
list. `addsession` and `removesession` are what a bridge needs;
`updatesession` exists for changing an existing entry.

```json
{
  "type": "internal",
  "internal": {
    "type": "addsession",
    "addsession": {
      "sessionid": "<id chosen by the client>",
      "roomid": "<token>",
      "incall": 9,
      "user": {
        "type": "phone",
        "callid": "<the bridge's own call id>",
        "number": "<E.164 or internal number>",
        "displayname": "<what Talk shows>"
      }
    }
  }
}
```

Properties that are not obvious and cost real debugging time:

- **A virtual session can never carry media.** In MCU mode the signaling server
  looks a publisher up by the raw recipient session id and has no
  virtual-to-owner mapping; publishers exist exclusively under a real client
  session's id. The virtual session is a name plate, and the call's audio has to
  ride on the bridge's own session.
- **Do not set `WITH_AUDIO` on it** (`IN_CALL | WITH_PHONE` = 9 is right).
  Advertising audio on a session that cannot publish leaves every Talk client
  retrying forever against a silent tile. The flags must be spelled out
  explicitly, because `internal-incall` also disables the server's default for
  virtual sessions.
- **`user.displayname` is the field Talk renders participants by.** Without it
  the caller shows as "Gast".
- **`options.actorType` / `options.actorId` do not work for an unknown caller.**
  The signaling server registers such a session with Nextcloud as that actor,
  and Nextcloud rejects an actor that is not already an invited participant of
  the room ("The user is not invited to this room") — failing the whole
  `addsession`.
- **The server assigns its own room session id**, unrelated to the chosen
  `sessionid`. It arrives in a `room`/`join` event and is the id that appears in
  room rosters. Match it back via `user.callid`, which round-trips unchanged.

Removal:

```json
{"type": "internal", "internal": {"type": "removesession",
 "removesession": {"sessionid": "...", "roomid": "..."}}}
```

## Dialout

Talk's native "call a phone number" UI sends the request to one dialout-eligible
internal client:

```json
{
  "id": "<request id>",
  "type": "internal",
  "internal": {
    "type": "dialout",
    "dialout": {
      "roomid": "<token>",
      "backend": "<Nextcloud URL>",
      "request": {"number": "<E.164>", "options": {}}
    }
  }
}
```

The room id comes **from this message**; there is no other way to learn it and
no default to fall back on.

The reply must echo the request `id` and keep the same `internal` wrapper. A
flat `{"type": "dialout", ...}` at the top level, without the wrapper or the
`id`, is **silently ignored**:

```json
{
  "id": "<same request id>",
  "type": "internal",
  "internal": {"type": "dialout", "dialout": {
    "roomid": "<token>",
    "type": "status",
    "status": {"callid": "<bridge call id>", "status": "accepted"}
  }}
}
```

- `accepted` is expected **synchronously**, within a fixed server-side timeout.
  Call progress follows later as unsolicited status updates — the same shape
  without an `id`. This bridge reports `connected`, `rejected` and `cleared`.
- Failure uses `"type": "error"` with an `error` object
  (`{"code": ..., "message": ...}`) in place of `status`.
- **Nextcloud validates the number before the request is sent** (`libphonenumber`,
  not configurable), and Talk formats it as E.164. A bare internal extension
  never reaches the bridge; mapping a valid-looking number back to an internal
  dial notation is the bridge's job.

## Publishing media

Media is published by sending a WebRTC offer **addressed to the client's own
session id**. The server routes it to the room's Janus instance and returns an
`answer`. This requires having joined the room first — `addsession` alone routes
nothing.

```json
{
  "id": "bridge-offer-<call id>",
  "type": "message",
  "message": {
    "recipient": {"type": "session", "sessionid": "<own session id>"},
    "data": {
      "to": "<own session id>", "type": "offer", "sid": "<random>",
      "roomType": "video",
      "payload": {"nick": "<displayed name>", "type": "offer", "sdp": "..."},
      "audiocodec": "opus"
    }
  }
}
```

`roomType` is `"video"` even for an audio-only stream. `payload.nick` names the
tile that actually carries the audio — distinct from the virtual session's
`displayname`.

There is **no way to feed raw RTP into a room**: Janus' VideoRoom plugin accepts
WebRTC publishers only. `rtp_forward` forwards media *out* of a room, and the
Streaming plugin re-streams external RTP outside the participant list. A bridge
must therefore be a complete WebRTC endpoint (ICE, DTLS-SRTP, a media engine).

## Subscribing to another participant

```json
{"id": "...", "type": "message", "message": {
  "recipient": {"type": "session", "sessionid": "<target>"},
  "data": {"type": "requestoffer", "roomType": "video"}}}
```

The server answers with an `offer` from that publisher, which is answered
normally; `candidate` messages flow both ways.

- **A single `requestoffer` is not enough.** If the target's publisher does not
  exist yet the server replies `client_not_found` and **does not queue** the
  request. Talk's own client re-requests every 10 seconds for this reason.
- Both sides must be in the call before either may subscribe to the other.
- A late duplicate `offer` for an already-negotiated subscription must be
  ignored, or answering it resets the working connection.

## Events

### `room` / `join`, `room` / `leave`

Room roster changes. A `join` entry carries `sessionid`, `userid`, `features`
and, for virtual sessions, `user`. Note that `leave` is **not reliable**: a
client that drops silently (a backgrounded mobile app, for instance) can stay in
the roster indefinitely, and requesting its audio then fails with
`client_not_found`.

### `participants` / `update`

Arrives in three shapes, all under the same event:

| Shape | Meaning |
|---|---|
| `users: [...]` | Full room-membership snapshot. Replaces what is known — a session missing from it is gone. The only way to learn about sessions that vanished without a `leave`. |
| `changed: [...]` | Per-session delta from backend-driven in-call updates. |
| `all: true` with lowercase `incall` | Room-wide broadcast: the call itself started or ended for everyone. This is what Talk's "end call" button produces. |

In every shape the in-call state is a bit field; `incall & 1 == 0` means not in
the call. Field names vary (`sessionId` / `sessionid`, `incall` / `inCall`) —
accept both.

**Decide from transitions, never from state.** The server can keep listing a
session as in-call long after its client is gone, which reading state makes
indistinguishable from somebody answering. Tracking transitions also makes
several sessions of one person (Talk in a browser and on a phone) unremarkable:
only the one that moves counts.

## Control messages

```json
{"type": "control", "control": {"data": {"type": "hangup"}}}
```

Sent when a call's virtual phone session is disinvited (the participant hung up
in Talk, or the room's call ended) or when the session is explicitly targeted
with a hangup. The server rewrites the recipient to the owning client's session
either way.

Note that ending a call in Talk's UI does **not** tear down an internal client's
own publisher, and produces no explicit hangup for it. The reliable end-of-call
signal is the `participants`/`update` broadcast above; the publisher's WebRTC
`connectionState` is a useful secondary check (`iceConnectionState` alone is
not — it does not reliably move on a DTLS-level teardown).

## Talk OCS call API

Used for what the signaling protocol cannot express: making a person's devices
ring, and ending a room's call. Authentication is HTTP Basic with a Nextcloud
**app password**, plus the `OCS-APIREQUEST: true` header. The room join is
cookie/session based, so one cookie jar has to be reused across the calls that
belong together.

| Method | Path (under `/ocs/v2.php/apps/spreed/api/v4`) | Purpose |
|---|---|---|
| POST | `/room/{token}/participants/active` | Establish a room session. Returns `ocs.data.sessionId`. |
| POST | `/call/{token}` (`{"flags": 1}`) | Join the room's call — this is what starts real ringing. |
| GET | `/room/{token}/participants` | List attendees, for `attendeeId`. |
| POST | `/call/{token}/ring/{attendeeId}` | Ring one attendee for the ongoing call. |
| DELETE | `/call/{token}?all=false` | Leave the call. |
| DELETE | `/call/{token}?all=true` | End the call for everyone. Needs moderator rights, else 403. |
| DELETE | `/room/{token}/participants/active` | Leave the room session. |

- **Joining the call requires an existing room session.** Calling `/call/{token}`
  without `participants/active` first fails with 404 (Talk's `RequireParticipant`
  check).
- **Joining the call alone is not enough to get an answer.** A client whose
  signaling session has gone stale still paints an incoming-call screen and then
  does nothing when tapped. `/call/{token}/ring/{attendeeId}` delivers a real
  call notification and gets the app to open a live session again.
- Ringing an attendee returns an error status for do-not-disturb and stays
  silent for someone already in the call. Neither is a fault of the caller.
- The account used must already be a member of the room.

## Room association for inbound calls

The protocol carries a room id for dialout only. An inbound SIP call has no
association to a room anywhere in the protocol, so the mapping from a line or
dialed number to a room token is the bridge's own configuration
(`BRIDGE_DEFAULT_ROOM`, see [`CONFIG.md`](./CONFIG.md)).
