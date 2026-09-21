<!--
talk-sip-bridge - docs/SIGNALING-API.md
The signaling server's internal-client protocol, as this bridge uses it.

  Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
  Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
  Written by Anthropic Claude Opus 5 - AI generated content.

  Free software under the GNU General Public License, version 3 or later.
  There is no warranty, to the extent permitted by law. The full text is
  in LICENSES/GPL-3.0-or-later.txt.

SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
SPDX-FileContributor: Anthropic Claude Opus 5 (AI generated content)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Talk signaling API reference

The protocol surface a SIP bridge needs from Nextcloud Talk: the standalone
signaling server's **internal client** interface (WebSocket) and the Talk
**OCS call API** (HTTPS). Nextcloud's own SIP bridge product is closed-source
and no reference implementation of this interface exists publicly — not in the
`nextcloud-spreed-signaling` repository itself, which contains only a Go
benchmarking client — so what follows is the working description this bridge is
built against.

Official protocol documentation:
<https://nextcloud-spreed-signaling.readthedocs.io/en/latest/standalone-signaling-api-v1/>
(sections "Internal clients", "Client features", "Dialout session", "Start
dialout from a room", "Add/update/remove virtual session"). Everything marked
**undocumented** below is absent from that page and is taken from the
signaling server's Go source or from observed server behavior.

This document describes the interface. For why this bridge uses it the way it
does, see [`CONCEPT.md`](./CONCEPT.md). The implementation is split by job:
`talk_client.py` holds the two connections and what they route to,
`signaling.py` keeps them up, and each half of a call has its own module -
`inbound_call.py`, `dialout.py`, `call_audio.py`, `human_audio.py`,
`phone_participant.py`, `room_presence.py`.

## Endpoints

| Purpose | Address |
|---|---|
| Signaling | The standalone signaling server's WebSocket, path `/spreed` |
| OCS call API | The Nextcloud instance, `/ocs/v2.php/apps/spreed/api/v4/...` |

Both are needed. The signaling server carries media negotiation and participant
state; it has **no** ringing/accept-decline exchange for inbound calls, so
alerting a person is only possible through the OCS API (see "Ringing" below).

## Prerequisites in Nextcloud

All native SIP endpoints (`POST /call/{token}/dialout/{attendeeId}`,
`verify-dialout` and the rest) are gated behind `Config::isSIPConfigured()` /
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
    "features": ["start-dialout"],
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
- `features` is per connection and says what this one is for — the example
  above is the dialout role. The two features cannot be combined; see
  [Two connections, one per role](#two-connections-one-per-role).

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

### Two connections, one per role

**The two features cannot share a connection.** A `start-dialout` session is
removed from the server's dialout candidates the moment it joins *any* room,
and the only code path that puts a session back is a fresh `hello` — leaving
the room does not (`hub.go`, `delete(h.dialoutSessions, session)` on join,
`h.dialoutSessions[session] = true` on hello). A bridge that publishes media on
the same connection it receives dialout requests on is therefore eligible for
exactly one dialout, after which Nextcloud reports "the phone number could not
be called" until that connection reconnects.

So a bridge keeps two, with disjoint jobs:

| | Dialout connection | Room connection |
|---|---|---|
| Features | `start-dialout` | `internal-incall` |
| Joins rooms | **never** | per call, leaves again afterwards |
| Sends | dialout replies and status updates | `room`, `addsession`/`updatesession`/`removesession`, `incall`, offers/answers/candidates |
| Receives | dialout requests | room and participant events, `control`, subscription errors |
| Lifetime | permanent | permanent; reconnects independently |

They are independent: a dialout can be refused while the room connection is
reconnecting, and a call in progress is unaffected by the dialout connection
dropping. A dialout **reply must go back on the connection that was asked** —
the server matches it against its own pending request, and that bookkeeping is
per session (`ClientSession.ProcessResponse`).

Verified against a live server: while the room connection sat in a room, the
dialout connection still received and placed a dialout; the room connection
received the room-wide end-of-call broadcast 0.4 s after the button was
pressed.

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

An empty `roomid` leaves whatever room the session is in:

```json
{"id": "bridge-room", "type": "room", "room": {"roomid": ""}}
```

- A successful join is answered with a `room` message carrying the same `id`.
- Re-joining a room this session is already in returns `error` with code
  **`already_joined`** instead. That is equivalent to success, and is the normal
  answer when a call is joined for twice (once while it rings, once to publish).
- A session can be in **one room at a time**; joining another leaves the first.
- **An internal client may join any room, and doing so creates nothing in
  Nextcloud.** The server short-circuits the backend request for internal
  clients (`hub.go`: *"Internal clients can join any room"*), so there is no
  attendee, no row in `oc_talk_sessions`, and no entry in the conversation. The
  session exists only inside the signaling server's room.
- **Joining a room permanently costs dialout eligibility** — see
  [Two connections, one per role](#two-connections-one-per-role). Leaving does
  not restore it.
- **Leave when the call is over.** Nothing expires the membership, and a room
  that still holds a session is a room the server still counts somebody in.

## Who is told what

Three different notions of "participant" overlap here, and a message reaches
one of them and not the others. Most of the effort in building a bridge goes
into getting this right, so it is worth stating flatly.

| | Nextcloud attendee | Real session (`ClientSession`) | Virtual session |
|---|---|---|---|
| Exists in | `oc_talk_attendees` | the signaling server | the signaling server |
| Created by | Nextcloud (adding a participant) | a `hello` | `addsession` |
| Carries media | — | yes | **never** |
| Receives room events | — | **yes** | **no** |
| Worth subscribing to | — | only if it is a person | never |
| Receives messages addressed to it | — | yes | yes, relayed to its owner |
| Shown in Talk's call grid | — | yes, unless `internal` | yes |

What follows from that:

- **Room-wide events reach real sessions only.** Only a `ClientSession`
  registers as a listener on the room's channel (`clientsession.go`), and
  `Room.PublishUsersInCallChangedAll` builds its recipient list by asserting
  each session to `*ClientSession` (`room.go`). A client that owns a virtual
  session in a room but is not in the room itself is told **nothing** about
  that room.
- **A virtual session relays only three things to its owner**
  (`virtualsession.go`): a `message` addressed to it, a `control` addressed to
  it, and a `roomlist`/`disinvite` naming its room — which the server turns
  into a `control`/`hangup`. Nothing else.
- **Internal sessions are in the room's user list but not in Talk's grid.**
  They appear in `participants`/`update` with `"internal": true`, and Talk
  filters them out of the call view unless they carry video
  (`callParticipantModels`). Virtual sessions carry `"virtual": true` and are
  **not** filtered — a phone left in a room after its call stays visible.
- **Nextcloud's signaling backend answers three request types**: `auth`, `room`
  and `ping` (`SignalingController::backend`). Everything else is rejected as
  `unknown_type`. In particular the `session` request the server sends for an
  `addsession` **without** an actor is discarded, so such a session is invisible
  to Nextcloud: no participant, nothing to disinvite, and no cleanup when the
  owning connection dies. An `addsession` **with** an actor is announced as a
  `room` request instead, which Nextcloud does handle.
- **Nextcloud never announces a `phones` attendee to the signaling server.**
  `BackendNotifier::roomInCallChanged` skips every actor type but `users`,
  `guests`, `emails` and `federated_users`, so a phone appears in
  `participants`/`update` only as the bare virtual entry the signaling server
  appends itself (`sessionId`, `inCall`, `lastPing`, `virtual`) — with no
  `actorId` and no `nextcloudSessionId` to tie it to the attendee.

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
- **Name an actor whenever one exists.** `options.actorType` /
  `options.actorId` is what makes the session visible to Nextcloud at all: with
  it the server announces the session as a `room` request, which Nextcloud
  answers by creating a session for that attendee
  (`SignalingController::backendRoom` → `createSessionForAttendee`); without it
  the announcement is a `session` request, which Nextcloud rejects as
  `unknown_type`. Two things follow only from having the actor: Talk's
  participant list gets a session id for the phone, and the server's cleanup on
  a dying connection reaches Nextcloud (`notifyBackendRemoved` sends
  `Action: "leave"` **only** when an actor is set).
- **The actor has to be a participant already.** Nextcloud looks the room up
  *by* the actor (`Manager::getRoomByActor`); an actor that is not a
  participant fails the whole `addsession` with "The user is not invited to
  this room". Where each one comes from:

  | Call | Actor |
  |---|---|
  | Dialout | `options.attendeeId` / `actorType` / `actorId` in the dialout request — Talk creates the `phones` attendee before asking |
  | Direct dial-in | the participant Nextcloud creates for the caller — see [Direct dial-in](#direct-dial-in) |
  | Anything else | none; the session is then a name plate the signaling server knows and Nextcloud does not |

- **The server assigns its own room session id**, unrelated to the chosen
  `sessionid`. It arrives in a `room`/`join` event and is the id that appears in
  room rosters. Match it back via `user.callid`, which round-trips unchanged.
  With an actor it is also the id Nextcloud stores in `oc_talk_sessions`, and
  therefore the one Talk's UI addresses a `control` to.

### Lifetime

A virtual session outlives its call unless something removes it:

```json
{"type": "internal", "internal": {"type": "removesession",
 "removesession": {"sessionid": "...", "roomid": "..."}}}
```

- Remove it on **every** way a call can end, including the ways that are not a
  call ending: a dialout refused with a SIP final response, one nobody answers,
  one hung up while it still rings. Each of those leaves a phone in the room
  otherwise, visible in Talk's grid, and the next call adds another beside it.
- A room holding such a leftover counts as occupied for Nextcloud
  (`hasActiveSessionsInCall` asks only for `in_call <> 0` and a recent ping), so
  leaving the call no longer resets the conversation's call state.
- Closing the owning connection removes them too — the server closes every
  virtual session of a `ClientSession` that goes away — but only reaches
  Nextcloud for sessions that named an actor.

### A phone that is only ringing

Announcing the phone while the call is still being placed is **wrong**, even
though `WITH_PHONE` is nominally the state for it. A virtual session is
announced as being *in the call*, and that has two effects a caller notices
immediately: Talk stops the ringback, so the line looks connected and carries
nothing, and the room counts as occupied. Announce the phone when the call is
answered.

There is nothing to gain from announcing it early either: the gesture that
ends a ringing dialout in Talk is "end meeting for everyone", and that reaches
the room's real sessions only (see [Who is told what](#who-is-told-what)).
Being there for it means having the **room connection** in the room while the
phone rings — not a virtual session.

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

### The answer, and what carries it

The answer comes back as a `message` **from the same session id it was
addressed to** - the client's own - and its SDP is applied to the publishing
peer connection:

```json
{"type": "message", "message": {
  "sender": {"sessionid": "<own session id>"},
  "data": {"type": "answer", "payload": {"type": "answer", "sdp": "..."}}}}
```

Because an offer is self-addressed, every message about the publisher arrives
with the client's own session as the sender. That is the only thing
distinguishing it from a subscriber's traffic, and it is how an incoming
message is routed to the right peer connection.

ICE candidates flow both ways as `message` with `data.type: "candidate"` and
the candidate in `data.payload`. Trickle is expected; a bridge that waits for
gathering to complete before sending its offer works but adds seconds to every
call, and a caller who gives up in the meantime leaves a publisher running for
a call that is gone - check the call is still alive after gathering.

Media the room must be able to use: **Opus**, which is what `audiocodec`
declares. A phone line's G.711 or G.722 has to be transcoded and resampled;
48 kHz is what Talk's clients expect.

## Subscribing to another participant

```json
{"id": "...", "type": "message", "message": {
  "recipient": {"type": "session", "sessionid": "<target>"},
  "data": {"type": "requestoffer", "roomType": "video"}}}
```

The server answers with an `offer` from that publisher, which is answered
with the shape below; `candidate` messages flow both ways.

```json
{"id": "bridge-subanswer-<call id>", "type": "message", "message": {
  "recipient": {"type": "session", "sessionid": "<the publisher>"},
  "data": {"to": "<the publisher>", "type": "answer",
           "sid": "<the sid from their offer>", "roomType": "video",
           "payload": {"type": "answer", "sdp": "..."}}}}
```

**`sid` identifies the negotiation, and it must be the one from the offer
being answered.** The server re-attaches its own end when a publisher is not
sending yet, and an answer carrying the handle from before is refused with
`answer message sid does not match subscriber sid`. That refusal arrives as
an `error` correlated to the request `id` — which is why the id names the
call: without it there is no way to tell which call has to try again.

- **A single `requestoffer` is not enough.** If the target's publisher does not
  exist yet the server replies `client_not_found` and **does not queue** the
  request. Talk's own client re-requests every 10 seconds for this reason.
- A late duplicate `offer` for an already-negotiated subscription must be
  ignored, or answering it resets the working connection.

### What the server requires, and what it does not

```go
// A user is only allowed to subscribe a stream if she is in the same room
// as the other user and both have their "inCall" flag set.
if !h.allowSubscribeAnyStream && !h.isInSameCall(ctx, session, message.Recipient.SessionId) {
```

That is the whole check (`hub.go`). Same room, both in the call — **no
moderator rights, no permission, and no difference between a signed-in user
and a guest**. Anyone allowed to join the call is allowed to be listened to.

### The server does not say who is speaking

**Measured, not assumed.** An internal client sat in a room while a
signed-in user joined the call, spoke several times with pauses between
them, muted the microphone and unmuted it again. Over those 25 seconds
the server sent that client **nothing at all** about the participant.

The only field that ever moved for them was `inCall`, and only at the
edges of the call:

```
 57.2  participants/update  schnebeck  inCall=3   <- joined the call
 83.7  participants/update  schnebeck  inCall=0   <- left
100.9  participants/update  schnebeck  inCall=3
126.2  participants/update  schnebeck  inCall=0
```

Two consequences, and both are structural rather than a gap in this
bridge:

- **There is no speaking indication to subscribe to.** A bridge cannot
  follow the active speaker, because nothing tells it who that is. The
  flags this bridge sends with `updatesession` (see
  `phone_participant.publish_talking`) inform Talk's clients about the
  phone; the traffic does not come back the other way.
- **Mute state is not in the signaling either.** `inCall` stayed `3`
  (`IN_CALL|WITH_AUDIO`) across muting and unmuting. Talk carries that
  between peers over their WebRTC data channels, which a bridge
  subscribing to one participant never sees for the others.

A client that wants to know who is speaking has to listen to them and
measure it. That is the same cost as carrying them, so the only way for
a caller to hear more than one person is to subscribe to each and mix.

### Choosing whom to listen to

A bridge carries one direction of the call by publishing and the other by
subscribing to exactly one participant, and picking the wrong one is silent:
the negotiation completes, the connection reports `connected`, and no audio
ever arrives.

| Rule | Why |
|---|---|
| Never a virtual session | It can carry no media at all |
| Never one of the bridge's own connections | Subscribing to yourself negotiates and connects and carries nothing — see [Telling the roster apart](#telling-the-roster-apart) |
| Prefer one whose flags carry `WITH_AUDIO` | A participant whose permissions do not let them speak joins without it (Talk sets it from `PERMISSIONS.PUBLISH_AUDIO`), and there is nothing to take from them |
| Fall back to any person rather than to nobody | The flags are the server's word, and a muted microphone is a stream that exists and is merely silent |
| Resolve several arrivals in a fixed order | They come in one event as a set; a retry that picks a different peer than the attempt before it is not a retry |

**Look again when somebody joins.** Looking once, at the moment the call
connects, is enough only when the other party is already there — which is the
case for a dialout, since they placed the call. A caller who dials in reaches
an empty room, so that one look finds nobody, and without a second look the
phone is heard in Talk and hears nothing back for the whole call. Whoever
enters the call afterwards is the first person there is to listen to.

Replacing a subscription that already works is the opposite mistake, and a
worse one: it tears down a connection that was carrying audio.

## Events

### `room` / `join`, `room` / `leave`

Room roster changes. A `join` entry carries `sessionid`, `userid`, `features`
and, for virtual sessions, `user`. Joining a room in progress delivers the
sessions already there as `join` events too, so this is the whole roster, not
only the changes.

Note that `leave` is **not reliable**: a client that drops silently (a
backgrounded mobile app, for instance) can stay in the roster indefinitely, and
requesting its audio then fails with `client_not_found`.

#### Telling the roster apart

Only a real person is worth subscribing to, and a roster holds three kinds of
entry. Getting this wrong is **silent**: a subscription to one's own session
negotiates, connects and carries no audio, so the call is up, the phone is
heard in Talk, and nobody in Talk is heard on the phone.

| Kind | How it is recognised |
|---|---|
| A phone | `user.type == "phone"` |
| A bridge's own connection | `features` contains `start-dialout` **or** `internal-incall` |
| A person | everything else |

- **`features` is what that session declared in its `hello`**, so a bridge with
  two connections announces *different* features on each. Testing for one
  particular feature misses the other connection. Test for any internal
  feature, and for one's own session id as well.
- **An empty `userid` is not the test.** A guest has none either.
- For a phone, this event is also the **only** place the server-assigned room
  session id appears. Match it back through `user.callid`.

### `participants` / `update`

Arrives in three shapes, all under the same event:

| Shape | Server-side origin | Meaning |
|---|---|---|
| `users: [...]` | `NotifySessionChanged` | Full room-membership snapshot. Replaces what is known — a session missing from it is gone. The only way to learn about sessions that vanished without a `leave`. |
| `changed: [...]` | backend-driven in-call updates | Per-session delta. |
| `all: true` with lowercase `incall` | `Room.PublishUsersInCallChangedAll` | Room-wide broadcast: the call itself started or ended for everyone, sent to every room member. This is what Talk's "end call" button produces. |

In every shape the in-call state is a bit field; `incall & 1 == 0` means not in
the call. Field names vary (`sessionId` / `sessionid`, `incall` / `inCall`) —
accept both.

**Decide from transitions, never from state.** The server can keep listing a
session as in-call long after its client is gone, which reading state makes
indistinguishable from somebody answering. Tracking transitions also makes
several sessions of one person (Talk in a browser and on a phone) unremarkable:
only the one that moves counts.

The flags are worth keeping whole rather than reduced to in-call yes/no: the
remaining bits say which media a session publishes, and that decides whom
there is any point subscribing to (see [Choosing whom to listen to](#choosing-whom-to-listen-to)).

#### A client that leaves and comes back

Saving a new microphone in Talk is a **leave and a rejoin**, and the
conversation records it in as many words. So is reloading the page, and so
is a network blip. Measured on one deployment, mid-call:

| | |
|---|---|
| `inCall` drops from 3 to 1 | the audio flag goes first, while the dialog is open |
| twelve seconds later, 1 to 0 | the client actually leaves the call |
| 5-7 seconds after that | it is back, with a **new session id** |

Three separate mistakes follow from taking any of that at face value, and
each of them was made here before it was found:

- **"Everyone left" is not the end of the call.** Acting on the first empty
  room ends a live conversation. Wait long enough for a rejoin - measured
  between 4.8 and 7.1 seconds, so the wait wants a generous multiple - and
  check again. An *explicit* end is different and needs no wait: the
  room-wide broadcast, or the phone participant being hung up.
- **The subscription does not survive it.** The publisher it was attached
  to is gone, so the peer connection closes and the audio in that direction
  stops for good unless something rebuilds it. "Am I subscribed to
  somebody" is the wrong question, because the answer stays yes; ask
  whether that connection is still alive.
- **A rebuilt subscription is a new negotiation.** Anything that tracks the
  state of one - and something has to, or two repairs run at once - must
  start from nothing. A state machine carried over from the last
  subscription has already reached "audio is flowing" and will refuse to
  ask for any, correctly and uselessly.

#### A stale in-call list, in every released server

Up to and including **v2.1.1**, `Room.PublishUsersInCallChangedAll` does not
update the cached user list it then sends to clients, so the next
`participants`/`update` carries in-call entries for people who have left. What
a browser shows for it: a phone conversation that drops back to "calling …"
after the call ended, a participant count that is one too high, and a ring for
somebody who is no longer there.

None of that is visible from a bridge, and nothing a bridge does causes or
cures it. The fix is two lines:

```go
 func (r *Room) PublishUsersInCallChangedAll(inCall int) {
+	for _, user := range r.users {
+		user["inCall"] = inCall
+	}
```

merged 2026-06-02 as `strukturag/nextcloud-spreed-signaling#1256`, after the
v2.1.1 release of 2026-03-12 — so **no released version contains it**. Talk
tracks the same symptom as `nextcloud/spreed#18185`, where its maintainer
attributes it to the signaling server. Until a release carries the fix, treat
a participant count that disagrees with `oc_talk_sessions` as the client's
own bookkeeping, not as a call that failed to tear down.

## Control messages

```json
{"type": "control", "control": {"data": {"type": "hangup"}}}
```

Reaches the owner of a virtual session in exactly two cases, and the server
rewrites the recipient to the owning client's session in both:

1. **The session is explicitly targeted with a hangup.** This is what Talk's
   "hang up phone" button next to a phone participant sends, addressed to the
   session id Nextcloud holds for that attendee. Internal clients may send it
   too — `isAllowedToControl` admits any internal client, no moderator rights
   and no room membership needed — which makes it testable without a browser.
2. **The session is disinvited**, i.e. Nextcloud sends a `disinvite` naming its
   room session id, which the server turns into this message
   (`virtualsession.go`). Nextcloud does that when an attendee is removed or a
   guest's session leaves the room — **not** when a call ends.

Things that produce no hangup, and are frequently assumed to:

- **Ending the call in Talk** (for oneself or for everyone). It tears down no
  internal client's publisher and disinvites nobody. The signal is the
  `participants`/`update` broadcast above, which only reaches sessions that are
  **in the room**.
- **The last person leaving a call** while a dialout rings.
  `CallController::leaveCall` resets the conversation's call state only if
  `hasActiveSessionsInCall` is false, and a phone announced as in-call makes it
  true.

The publisher's WebRTC `connectionState` is a useful secondary check
(`iceConnectionState` alone is not — it does not reliably move on a DTLS-level
teardown).

## Call sequences

The order is not free. Each step below either enables the next or is the only
moment at which the thing it does still works; the "why" column says which.
**D** is the dialout connection, **R** the room connection.

### Outbound: Talk calls a phone

| # | On | Do | Condition / why |
|---|---|---|---|
| 1 | D | receive `internal`/`dialout` | Carries `roomid` and `request.options` (the `phones` attendee). Both are needed later and available nowhere else. |
| 2 | — | map the number to the gateway's dial plan | Nextcloud sent E.164; an internal extension has to be recovered from it. |
| 3 | — | place the SIP call, keep its call id | The call id is what every later status names. |
| 4 | D | reply `status: accepted`, echoing the request `id` | **Synchronously**, within the server's timeout. Same connection as step 1. On failure reply `type: error` instead and stop. |
| 5 | R | `room` join `roomid` | Not before step 4 — waiting for the join confirmation would miss the timeout. From here the room's end-of-call broadcast is heard. |
| 6 | R | on `participants`/`update` with `all: true, incall: 0` → hang up the SIP call | Only while the call is unanswered is this the caller giving up. |
| | | *— the phone answers —* | |
| 7 | R | `addsession` with the actor from step 1, `incall: IN_CALL` | Not earlier: a session announced while it rings stops Talk's ringback. |
| 8 | R | `updatesession` with the full flags | A *change* is what puts the session into the room's in-call set; a value equal to step 7's is not a change. |
| 9 | R | publish: offer addressed to R's own session id | Needs the room join from step 5. |
| 10 | R | `incall` = `IN_CALL \| WITH_AUDIO` for R's own session | Only once the publisher exists, or clients chase a stream that is not there. |
| 11 | R | `requestoffer` to a **person** in the room, answer the offer that comes back | Both sides must be in the call first, which step 10 completes. Picking the wrong roster entry - a phone, or one of the bridge's own connections - connects and carries silence; see [Telling the roster apart](#telling-the-roster-apart). |

### Outbound: the call ends

Every one of these paths must run the teardown; they are not variations of one
signal.

| The call ends because | Reaches the bridge as | Notes |
|---|---|---|
| the phone answers, then hangs up | SIP `BYE` | |
| the phone refuses | SIP final response `4xx`/`5xx`/`6xx` | No `on_call_ended` equivalent — this is the only notification. |
| nobody answers | the bridge's own timeout | Same. |
| Talk ends the call for everyone | `participants`/`update`, `all: true, incall: 0` | Only if R is in the room — step 5. |
| the phone participant is hung up in Talk | `control`/`hangup` | |
| the last person leaves the call | `participants`/`update` without them | Read as a transition, not as state. |

Teardown, in this order:

| # | On | Do | Why here |
|---|---|---|---|
| 1 | R | close the media | Before removing what advertises it. |
| 2 | R | `removesession` | Or the phone stays in Talk's grid and keeps the room occupied. |
| 3 | R | `incall` = 0 for R's own session | Stops clients asking for a publisher that is gone. |
| 4 | D | `status: cleared` (or `rejected`), no `id` | Talk clears the attendee's ringing state on either. |
| 5 | R | `room` leave (empty `roomid`) | Nothing expires the membership. |

### Inbound: a phone calls in

| # | On | Do | Condition / why |
|---|---|---|---|
| 1 | — | answer or ring, per the dialled number | The protocol has no inbound ringing exchange; see [Room association](#room-association-for-inbound-calls). |
| 2 | — | find the room | Direct dial-in yields a room **and** an actor; a PIN or a spoken meeting id yields a room only. |
| 3 | R | `room` join | As step 5 above. |
| 4 | R | `addsession` (+ actor if there is one), then `updatesession` | As steps 7–8 above. |
| 5 | R | publish, then `incall` with audio, then subscribe | As steps 9–11 above. |
| 6 | R | on `participants`/`update`, subscribe to whoever enters the call, if nobody is subscribed yet | **The step a dialout never needs.** A caller who dials in reaches an empty room, so step 5 finds nobody to listen to; without this the phone is heard in Talk and hears nothing back for the whole call. |

Teardown is the same as for an outbound call, minus the dialout status.

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

### What ends somebody else's call

A participant leaving never ends anyone else's call, and that includes the
phone: when a call is hung up on the phone, the person in Talk stays in the
call, alone, until they leave it themselves. **That is Talk's behaviour, not a
defect, and this bridge does not work around it.** The model is consistent —
a person leaving a call ends nobody else's either — and Talk's own SIP dialout
tests walk exactly this sequence with the user hanging up in Talk
(`nextcloud/spreed#18185`). Nothing here should be re-litigated by sending a
status that means something else; see the table below for why `rejected` is
the only lever and what it costs.

Two things override the rule, and a bridge can reach only one of them.

| Trigger | Effect on a Talk client | Reachable by a bridge |
|---|---|---|
| Dialout status `rejected` | Leaves the call (`all: true`) and says "Call rejected" — **only** in a phone conversation | yes, but it means the call never came about; sending it for a call that was answered and then hung up puts "Call rejected" in front of the user every time |
| Chat system message `call_ended_everyone` | Leaves the call — unless the conversation is `TYPE_ONE_TO_ONE` or the client itself caused it | no |

The second one is a control channel that is invisible from the signaling
interface: Talk's clients watch the chat for `call_started`, `call_missed`,
`call_ended` and `call_ended_everyone`, and act on the last of those.
`call_ended` — the message posted when a call is over — deliberately does
**not** end anything.

The `TYPE_ONE_TO_ONE` exception does not apply to phone conversations. Those
are `TYPE_GROUP` marked with an object type (`phone_legacy`,
`phone_persistent`, `phone_temporary`) and an object id (`phone_incoming`,
`phone_outgoing`); it is that pair, not the room type, that Talk's clients test
for when they treat a conversation as a phone call.

`call_ended_everyone` comes from `ParticipantService::endCallForEveryone`,
reachable through `DELETE /call/{token}?all=true` as a moderator, and through
no SIP-bridge-authenticated endpoint — those are `GET /room/{token}`,
`GET /room/{token}/pin/{pin}`, `verify-dialin`, `direct-dial-in`,
`verify-dialout`, `open-dial-in` and `DELETE /room/{token}/rejected-dialout`,
none of which ends a call. A bridge whose own account is not a member of the
conversation therefore cannot end the call it was part of.

## Room association for inbound calls

The signaling protocol carries a room id for dialout only; an inbound SIP call
has no association to a room anywhere in it. There are two ways to get one, and
they lead to different rooms and different participants.

**By configuration.** The line answering the call names a room token
(`BRIDGE_DEFAULT_ROOM`, see [`CONFIG.md`](./CONFIG.md)). Simple, static, and
the caller exists only as a virtual session in the signaling server - Nextcloud
knows nothing about them, which is why `options` cannot name them as an actor.

**Direct dial-in**, which is what Nextcloud's own SIP bridge does. Nextcloud
creates the room and the participant, and hands both back.

### Direct dial-in

```
POST /ocs/v2.php/apps/spreed/api/v4/room/direct-dial-in
     phoneNumber=<the number that was called>&caller=<the number calling>
```

Authenticated not as a user but **as a SIP bridge**, with two headers:

| Header | Content |
|---|---|
| `Talk-SIPBridge-Random` | at least 32 characters of randomness |
| `Talk-SIPBridge-Checksum` | `HMAC-SHA256(secret, random + data)`, lowercase hex |

`data` is whatever the endpoint validates against - the dialled number here, the
room token elsewhere. The secret is Talk's `sip_bridge_shared_secret`
(`occ config:app:get spreed sip_bridge_shared_secret`).

What Nextcloud does with it, from `RoomController::directDialIn`:

1. looks up which account the dialled number belongs to, in the
   `talk_phone_numbers` table maintained with `occ talk:phone-number:add`
   (404 when the number is mapped to nobody),
2. creates a conversation of type group, named after the caller, with SIP
   enabled without a PIN and object type `phone_temporary` - **one conversation
   per call**, not a standing room,
3. joins the caller as a new guest with the caller's number as display name,
4. returns the room, including the token and that participant's actor.

The caller is then a participant Nextcloud knows, so `addsession` may name them
in `options` and Talk's clients see a real participant rather than a session
that exists only in the signaling server.

Requires the `sip-direct-dialin` capability (Talk 21+) and a number mapped to a
user; without the mapping the endpoint answers 404 and there is no room.

Measured against a live Talk 24.0.5, with consequences that decide where this
can be used at all:

- The mapping is an **exact string match** on the stored number
  (`PhoneNumberMapper::findByPhoneNumber`). `occ talk:phone-number:add` strips
  the leading `+`, so a bridge asking for `+4930621` gets a 404 for a number
  stored as `4930621`. Internal extensions are refused outright by the command
  ("Not a valid phone number **621") - a gateway announcing one has to be
  configured with the external number it stands for.
- **The conversation belongs to the account the number is mapped to**, and
  nobody else is in it. A bridge account cannot join it to ring anybody, which
  is how the room-per-line arrangement rings people. Ringing is Nextcloud's
  own doing here.
- Which means **the call has to be answered**: the caller is already a
  participant waiting in a conversation of their own, and nothing else will
  pick up. That suits numbers that belong to the bridge - a lobby a caller
  dials into - and it is wrong for a number that also rings a person's own
  phone, where answering takes the call away from them.

### Dialling into a conversation

The other way in, and the one Talk's own SIP bridge holds a dialogue for: the
caller says which conversation they want.

```
POST /ocs/v2.php/apps/spreed/api/v4/room/{token}/open-dial-in
POST /ocs/v2.php/apps/spreed/api/v4/room/{token}/verify-dialin   pin=<digits>
```

Both are signed like every SIP-bridge request, with the **room token** as the
signed data. What they do differs:

- `open-dial-in` joins the caller as a new guest, and works only where the
  conversation is SIP-enabled **without** a PIN (`Webinary::SIP_ENABLED_NO_PIN`,
  state 2). Anything else answers 400, which is the signal to ask for a PIN.
- `verify-dialin` looks up the participant that PIN belongs to
  (`ParticipantService::getParticipantByPin`) and returns the conversation with
  that participant's actor. It creates nobody: the participant already exists,
  and the PIN says which one is on the phone. 404 when no participant has it.

Both return the room in the same shape `direct-dial-in` does, so the actor in
it can be named in `addsession` exactly the same way.

What a caller keys in, from Talk's own rules:

- The **meeting id is the conversation token**. Where SIP is configured, Talk
  generates digits-only tokens of at least 10 characters for new conversations
  (`Manager::getNewToken`); older conversations keep alphanumeric tokens and
  **cannot be dialled at all** - `setSIPEnabled` refuses a token that contains a
  non-digit, starts with `0`, or repeats a digit twice in a row
  (`Room::SIP_INCOMPATIBLE_REGEX`).
- A **PIN is per participant**, not per conversation: seven digits, generated by
  Talk (`ParticipantService::generatePin`), never starting with `0` and never
  repeating a digit consecutively - both because telephone systems mishandle
  those.
- Enabling SIP on a conversation is a moderator action, restricted to the groups
  in `occ config:app:get spreed sip_bridge_groups` when that is set.

Which is why dial-in is configured per number and not per line (`DIALIN_NUMBERS`
in `docs/CONFIG.md`). The number a call was placed to is read from the INVITE's
Request-URI, falling back to `To`; a number in the mapping is the bridge's own
and is answered into the conversation Nextcloud creates for it, and every other
number on the same line rings the room that line names, untouched. One
registration therefore carries both, which matters where there is only one:
a person's own number and a set of dial-in numbers can share a single line.
A number in `CONFERENCE_NUMBERS` is answered the same way and asked the
question above instead (`bridge/dialin_ivr.py`).

How the caller learns what to key in is Nextcloud's own business, and it
happens before the meeting: a participant invited by email address into a
SIP-enabled conversation gets a PIN the moment they are added
(`ParticipantService::addEmail`), and the invitation mail then carries the
dial-in text from `occ config:app:set spreed sip_bridge_dialin_info`, the
meeting id and that PIN (`GuestManager::sendEmailInvitation`). Enabling SIP
**after** inviting leaves that block out of the mail that already went;
`POST /room/{token}/participants/resend-invitations` sends it again.
