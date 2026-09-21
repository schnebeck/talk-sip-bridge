<!--
talk-sip-bridge - docs/SIP-API.md
The SIP this bridge speaks, in enough detail to write another client from.

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

# The phone side

What a bridge has to send and survive on a SIP line, and which of it is
not in the RFCs. The companion to [`SIGNALING-API.md`](./SIGNALING-API.md),
which covers the Talk side; between them they are what this bridge is
built on.

Everything marked **measured** was observed against a real gateway (a
FRITZ!Box 7590 with DECT handsets and the FRITZ!Fon app) or against
Asterisk 20, and several items contradict what the specification alone
would suggest. The recorded messages are in `tests/fixtures/fritzbox/`.

Scope: one line, one call at a time, audio only. A line is one SIP
account; everything below is per line.

## What has to exist before a call

| | |
|---|---|
| Registration | Kept alive continuously; a lapsed registration means the gateway cannot deliver a call and nothing says so |
| A media port | Bound before the answer goes out, because the answer names it |
| A signaling path to Talk | Separate matter entirely — see the companion document |

## Transport

Two choices, per line, and the wrong one fails **silently**: a FRITZ!Box
drops UDP registrations without a word, and TCP-only providers are common.

| | UDP | TCP |
|---|---|---|
| Framing | one message per datagram | a stream; message boundaries must be found |
| Replies | to the address the request came from | on the connection the request arrived on |
| Inbound calls | arrive on the bound socket | the gateway **opens its own connection** to the Contact address |
| After a drop | nothing to do | reconnect, or the line is unreachable |

**A registrar delivers a call by connecting to the Contact address**, not
by answering on the registration's connection. A TCP bridge therefore
needs a listener as well as its outgoing connection, and the two carry
different dialogs.

Finding message boundaries in a TCP stream: read headers to the blank
line, take `Content-Length`, then that many bytes. A message can be split
across reads and several can arrive in one.

### Via and Contact name the transport twice

`Via` carries the transport of the socket the message goes out on.
`Contact` carries the one the gateway should come back over. They are
usually the same and **legitimately differ** when a relay changes
transport in between: the bridge speaks UDP to the relay, the relay speaks
TCP onwards, so `Via` says UDP and `Contact` says TCP. Getting `Contact`
wrong costs inbound calls only — registration still succeeds, which is
what makes it hard to spot.

## Registration

```
REGISTER sip:<gateway> SIP/2.0
Via: SIP/2.0/<transport> <local ip>:<port>;branch=z9hG4bK<random>;rport
Max-Forwards: 70
From: <sip:<user>@<gateway>>;tag=<tag>
To: <sip:<user>@<gateway>>
Call-ID: <stable for the whole registration, including refreshes>
CSeq: <increments per attempt> REGISTER
Contact: <sip:<user>@<contact host>:<contact port>;transport=<contact transport>>
Expires: <seconds>
Content-Length: 0
```

Answered `401 Unauthorized` (or `407`) with a `WWW-Authenticate` challenge;
repeat with an `Authorization` header and the next `CSeq`.

- **Refresh at around 60 % of `Expires`.** A refresh that fails is normal
  and must be retried, not treated as "switched off": one unanswered
  REGISTER otherwise ends the line until somebody notices.
- **Keep "is registered" and "is meant to be registered" apart.** They
  were one field here once, and a failed refresh set it to false, which
  the keepalive read as "turned off" and stopped.
- **A refresh that raises rather than fails must not end the loop
  either** — the line then stays reachable until it expires and is
  silently gone after that.
- To deregister, repeat with `Expires: 0`. A wildcard `Contact: *` clears
  every binding the account has, which is how a stale binding from a
  crashed instance is removed.

### Digest authentication

This bridge implements the RFC 2069 form: `MD5(HA1:nonce:HA2)`, with no
`qop`, `cnonce`, nonce count or `opaque` echo.

- **Measured:** Asterisk 20 challenges with `qop="auth"` and `opaque=...`
  and accepts the older response anyway. A registrar that *requires*
  `qop` would reject it.
- `;rport` is sent; the value that comes back is not read. A registrar
  reporting the source address it actually saw is telling a NATed client
  something worth having.

## An inbound call

```
      gateway                          bridge
        |------------- INVITE + SDP ----->|
        |<------------ 100 Trying --------|
        |<------------ 180 Ringing -------|   while deciding
        |<------------ 200 OK + SDP ------|   answering
        |------------- ACK -------------->|
        |<============ RTP ==============>|
        |------------- BYE -------------->|   or the bridge sends it
        |<------------ 200 OK ------------|
```

- **Which number was dialled is in `P-Called-Party-ID`**, not in the
  Request-URI. Measured: a FRITZ!Box puts the extension that was dialled
  (`**9`) there and its own account in the Request-URI. A bridge that
  decides anything by the dialled number — dial-in, a conference
  extension, whether to answer at all — has to read that header first and
  fall back to the Request-URI and `To` only if it is absent.
- **The caller's number and the caller's display name are different
  things.** `From: "FritzFon schwarz" <sip:**620@fritz.box>` carries a
  device name, not a person; anything that identifies the caller has to
  use the URI's user part.
- **Answer with a codec the caller actually offered.** Answering with one
  they did not gives a call that connects and carries nothing, with no
  error anywhere. Refuse with `488 Not Acceptable Here` instead.
- **Send audio to the address in the offer's SDP**, not to the caller's
  signaling address on your own port number. The latter happens to work
  when the gateway uses the same port and is silent when it does not.

### Ending a call that was never answered

A call in this state has **no dialog**, and the correct message depends on
which side started it. All three are measured; the wrong one leaves the
phone ringing.

| Situation | Send | What the wrong choice does |
|---|---|---|
| Inbound, still ringing | a final response, e.g. `603 Decline` | `BYE` is answered `481 Call/Transaction Does Not Exist` while the caller keeps hearing ringback |
| Outbound, still ringing | `CANCEL` | `BYE` is answered `481` and the phone rings on until the INVITE times out — measured at half a minute |
| Either, answered | `BYE` | — |

A `CANCEL` repeats the INVITE's branch and `CSeq` rather than taking the
next one; an `ACK` for a 2xx repeats the INVITE's `CSeq` too.

### In-dialog requests a gateway sends mid-call

Both must be answered, and neither is optional in practice:

- **re-INVITE** — hold, resume, a codec change. Answer `200 OK` with an
  SDP answer for the new offer. Measured: answering `486 Busy Here`
  because a call is already in progress drops the call.
- **INFO** — one road for DTMF (below). An in-dialog request nobody
  answers is retransmitted and then taken as a dead dialog.

## An outbound call

```
      bridge                           gateway
        |------------- INVITE + SDP ----->|
        |<------------ 407 + challenge ---|
        |------------- ACK -------------->|
        |------------- INVITE + auth ---->|
        |<------------ 180 Ringing -------|
        |<------------ 200 OK + SDP ------|
        |------------- ACK -------------->|
        |<============ RTP ==============>|
```

- The `407` must be **ACKed** before the retry, or the gateway keeps
  retransmitting and holds the transaction open. The same applies to any
  final non-2xx: without an ACK it shows up as a spurious `486 Busy Here`
  on later calls until it times out.
- **In-dialog requests go to the `Contact` of the 200 OK**, which is
  often an opaque per-dialog URI and not the number that was dialled.
- A refusal (`4xx`/`5xx`/`6xx`) and a call nobody answers are the same
  event to everything upstream: the call never happened. Both need the
  same cleanup, and there is no "call ended" to hang it on.

## SDP

Offer everything that can be answered, in preference order, plus
telephone-event:

```
v=0
o=- <session id> <version> IN IP4 <local ip>
s=-
c=IN IP4 <local ip>
t=0 0
m=audio <port> RTP/AVP 9 8 0 101
a=rtpmap:9 G722/8000
a=rtpmap:8 PCMA/8000
a=rtpmap:0 PCMU/8000
a=rtpmap:101 telephone-event/8000
a=fmtp:101 0-15
a=sendrecv
```

- **G.722 is `a=rtpmap:9 G722/8000` although it samples at 16 kHz.** The
  8000 is a historical error preserved by RFC 3551; the clock rate in RTP
  is 8000 while the audio is 16 kHz. A bridge that resamples has to use
  16 kHz for the samples and 8000 for the timestamps.
- **Only claim telephone-event in an answer if it was offered.** Claiming
  it otherwise invites events on a payload type the far end never agreed
  to.
- The payload type for telephone-event is **negotiated per call** — 101 is
  conventional, not fixed. Read it out of the answer.
- A caller who offers no telephone-event will send key presses as audible
  tones. That is not a fault; it is the only road left.

## RTP

Plain RTP/AVP, no SRTP. 20 ms packets: 160 samples at 8 kHz for
G.711, 320 at 16 kHz for G.722.

- **Pace against a fixed schedule**, not by sleeping one packet's worth
  after each send. Sending takes time, and that error accumulates into a
  stream slower than real time.
- **Only accept media from the negotiated peer address.** Any host that
  can reach the port could otherwise inject audio into a live call.
- Late or duplicated packets are better dropped than played; a packet
  that arrives faster than real time and is kept becomes latency that
  never comes back. Both are worth counting separately — thrown-away
  audio sounds like chopping, missing audio sounds like a gap.
- **One bad packet must not end the receive loop.** It runs in a thread
  of its own, and if it dies the call stays up and carries silence in
  that direction, with everything else looking healthy.
- Binding the media port again for the next call can fail with
  `EADDRINUSE` while the previous receive thread is still inside
  `recvfrom`: closing the socket does not free the port until that call
  returns. Wait for the thread, and retry the bind.

### G.722 is stateful

Unlike G.711, its sub-band ADPCM coding carries state between frames.
Encoder and decoder instances belong to the call and cannot be recreated
per packet, and a gap in the stream is audible past the gap itself.

### Gain

A phone line's level is far below what a conference expects. Automatic
gain control on the phone-side audio only — applying it to what comes
from the conference as well risks audible pumping, because that side is
already normalised.

## Key presses arrive by three roads

A bridge that reads only one of them will miss presses from real
handsets. All three deliver the same press, so they must be collapsed.

| Road | What it looks like |
|---|---|
| RFC 4733 events | a packet every 20 ms for as long as the key is held, **all carrying the same RTP timestamp**, then the last one repeated with an end marker |
| Tones in the audio | two sine tones at once, one from 697/770/852/941 Hz, one from 1209/1336/1477/1633 Hz |
| `SIP INFO` | a body naming the digit, in-dialog |

- **Measured:** this deployment's gateway agrees to RFC 4733 and then
  plays the tones into the audio instead — seen twice, once from a
  synthetic caller and once from its own DECT handset. Reading only
  events would have read nothing.
- One press is dozens of event packets; the **RTP timestamp** is what
  distinguishes a second press of the same key from a repeat of the
  first.
- Collapse a press reported by two roads into one, per digit and with a
  short window. A single guard for all digits drops the second key of a
  fast pair.
- Detecting tones in speech is the hard part, not detecting tones.
  Speech contains every frequency eventually, and a false digit in a
  meeting id is worse than a missed one. Require a press to persist
  across blocks, and tolerate a one-block gap: a real handset's tones
  are short and arrive broken up — measured, a detector built against
  textbook tones of equal amplitude read one key in twelve.

## What this bridge does not implement

Honest limits, each of which some deployment will meet:

- **`Record-Route` and `Route` are ignored.** In-dialog requests go
  straight to the peer's `Contact`. A proxy that builds a route set for
  the dialog would not be honoured.
- **No SRTP, no ICE on the phone side.** Plain RTP to the address in the
  SDP.
- **Three codecs** (G.722, PCMA, PCMU). Anything else is answered with
  PCMU.
- **No forking, no early media, no REFER**, no attended transfer.
- **One call at a time per line.** Everything above assumes it; a second
  call needs a second line, with its own registration, ports and
  signaling connection.
