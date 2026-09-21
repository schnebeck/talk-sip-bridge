<!--
talk-sip-bridge - README.md
What this bridge is, what it does, and where to read on.

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

# Talk SIP Bridge

**Gives Nextcloud Talk a telephone number.** People ring it, key in the
meeting id they were sent and a PIN if the conversation wants one, and they
are in the meeting - from a mobile in a car park, from a hotel phone, from
a customer who will not install anything. Nextcloud makes them a
participant of that conversation itself, so they arrive named, and everyone
can see, mute and hang up on them like anybody else. It works the other way
round too: somebody in a conversation presses *Call a phone number* and a
phone rings. Both directions carry real audio, and both use the telephone
support Talk already has - no bot, no chat commands, no second interface
for anyone to learn.

**Numbers are configured one at a time, and there can be several.** Each
one is either a dial-in number that lands in a particular conversation, or
a conference number that asks the caller which meeting they want - one
phone account can carry a whole set of them, and a second account adds a
second call that can run at the same time. Everything else about a number
is configuration too: which gateway, which transport, which dial-plan
notation. A home VoIP box and a business PBX are the same thing here, and
both have been used.

**It is small, and it stays out of the way.** One program, one service
file, a handful of settings; no extra service to run alongside it, no
database to keep. The line stays switched off until somebody turns it on -
from a page in Nextcloud's own admin settings, or one command - and it
answers **only the numbers you give it**, from **only the callers you
allow**. An account that also carries somebody's own number keeps ringing
that person, untouched: this will not quietly pick up a call meant for a
human being.

**What it will not do.** One phone account carries **one call at a time**:
a second caller hears the busy signal. More accounts mean more
simultaneous calls, but they are not yet a pool - a call is refused if the
account it was handed to is busy, even when another is free. **Outgoing
calls cannot yet show a per-user number**: Talk's request says which
conversation the call is for, but not who pressed the button, so "everyone
in sales dials out as the sales number" is not something this can do today
(per conversation would be the next step; see below). Voice only, no
video. The telephone half of a call is **not encrypted**, so the gateway
belongs on a network you trust. And it speaks a plain, slightly
old-fashioned dialect of the telephone protocol: most gateways and
providers are happy with it, but a strict one, or a chain of telephone
proxies, can turn it away. Exactly which dialect, and what each limit
costs, is spelled out in
[`CONCEPT.md`](./docs/CONCEPT.md#known-gaps-in-the-sip-implementation) -
worth two minutes before you commit to it.

**What it has been tried on.** One setup, and it is worth knowing which:
a FRITZ!Box with a **single external line** and several internal SIP
devices - a DECT handset, a softphone, a second account standing in for a
caller. Everything stated as confirmed below was confirmed there. Against
a second SIP implementation, an Asterisk 20 in a container, the protocol
side is exercised too, which is what keeps "works with my box" and
"speaks SIP" apart. But a provider's SIP trunk, a PBX with several
external lines, a real public dial-in number, and more than one account
running at once have **never been tried** - multiple accounts are covered
by offline tests and by design, not by a telephone. None of that is known
to fail; it is simply unproven, and the protocol work is deliberately
gateway-independent so that it has a fair chance. If you try one, the
recordings in `tests/fixtures/` are how a second gateway's behaviour gets
written down.

**What you need.** A phone account - username, password, address - the
kind a VoIP box or a telephony provider hands out. A Nextcloud with Talk
*and* its separate signaling server, the High Performance Backend, since
the built-in one cannot carry this. And a small Linux machine with Python
3.10 or newer that can reach both; if it cannot reach the phone gateway
directly, the [`relay/`](./relay) in this repository joins the two
networks. That is the whole shopping list.

## Getting the first number running

The short path: one phone account, a gateway the bridge host reaches
directly, calls placed from Talk. Dial-in is the section after this one. [`ADMIN.md`](./docs/ADMIN.md)
is the same path with the reasons, the checks and the failure modes.

**1. Install the daemon.**

```
useradd --system --no-create-home talk-sip-bridge
python3 -m venv /opt/talk-sip-bridge-venv
/opt/talk-sip-bridge-venv/bin/pip install -r bridge/requirements.txt
install -d -o talk-sip-bridge /opt/talk-sip-bridge
install -o talk-sip-bridge bridge/*.py /opt/talk-sip-bridge/
install -d /etc/talk-sip-bridge
install -m 600 -o talk-sip-bridge deploy/env.example /etc/talk-sip-bridge/env
install -m 644 deploy/talk-sip-bridge.service /etc/systemd/system/
```

**2. Fill in seven values** in `/etc/talk-sip-bridge/env`. Nothing else is
required; every other setting has a working default
([`CONFIG.md`](./docs/CONFIG.md)).

```
BRIDGE_SIP_USER=          BRIDGE_WS_URL=ws://127.0.0.1:8080/spreed
BRIDGE_SIP_PASS=          BRIDGE_INTERNAL_SECRET=
BRIDGE_GATEWAY_HOST=      BRIDGE_BACKEND_URL=https://cloud.example
BRIDGE_LOCAL_IP=
```

If the line never registers and nothing says why, set
`BRIDGE_SIP_TRANSPORT=tcp`. A FRITZ!Box drops UDP registrations without a
word, and TCP-only providers are common.

**3. Tell Talk that a SIP bridge exists**, or its call button does not
appear:

```
occ config:app:set spreed sip_bridge_shared_secret --value="$(openssl rand -hex 32)"
occ config:app:set spreed sip_bridge_dialin_info   --value='<the number to call, in your users language>'
occ config:app:set spreed sip_dialout              --value='yes'
```

**4. Start it and switch the line on.**

```
systemctl daemon-reload && systemctl enable --now talk-sip-bridge
curl -X POST http://127.0.0.1:8765/toggle
curl -s  http://127.0.0.1:8765/status
```

`"registered": true, "last_error": null` and the line is up. Open any Talk
conversation, start a call, *Call a phone number* - the phone rings.

## Letting people dial in

The step most installations actually want. Two kinds of number, and one
account can carry both:

| | |
|---|---|
| `BRIDGE_CONFERENCE_NUMBERS` | **One number, any meeting.** A call to it is answered and asked which conversation it wants; the caller keys in the meeting id, and a PIN if that conversation has one. `**900`, say, or a real number your provider routes to you. |
| `BRIDGE_DIALIN_NUMBERS` | **One number, one meeting.** `4930622=4930622` maps a number straight into the conversation Nextcloud has registered for it (`occ talk:phone-number:add`), with no keypad step at all. Several numbers, several conversations, one account. |

**`BRIDGE_CONFERENCE_CALLERS` decides who gets that far** - a regular
expression the caller's own number must match in full. Leaving it empty
admits every caller in the world, which is the right setting for a number
that exists to be dialled and the wrong one for anything else.

Nextcloud does the rest: it creates a participant for the caller, so they
appear by number rather than as an anonymous guest, and the PIN it shows
each invited person is the one the prompt asks for. Recorded prompts in
your own languages come from
[`deploy/make-ivr-prompts.sh`](./deploy/make-ivr-prompts.sh); without them
the caller hears two beeps, which nobody can act on unaided.

## More than one number at a time

`BRIDGE_LINES` gives each phone account its own settings, ports and
connections in one daemon, so several calls can run at once - one per
account. Dial-in numbers are cheaper than that: a single account carries as
many of them as you like, because only one call occupies it at a time
either way. [`deploy/test-multiline.env.example`](./deploy/test-multiline.env.example)
is a ten-account template.

What is **not** there yet: choosing the outgoing number per Nextcloud user,
and treating the accounts as a pool that skips a busy one. The first needs
something Talk does not send today; the second is ours to do.

**Two more optional pieces.** For the admin on/off page, install
[`nextcloud-app/`](./nextcloud-app). For a gateway on the far side of a
network boundary, [`relay/`](./relay).

## The documents

Two of them define the interfaces, and between them they are meant to be
enough to write an equivalent bridge from - including the parts that are
undocumented upstream and the ones where a real gateway or client
contradicts the specification.

| | |
|---|---|
| [`docs/SIGNALING-API.md`](./docs/SIGNALING-API.md) | The Talk side: the signaling server's internal-client protocol and Talk's OCS call API |
| [`docs/SIP-API.md`](./docs/SIP-API.md) | The phone side: registration, calls in both directions, SDP, RTP, key presses |
| [`docs/ADMIN.md`](./docs/ADMIN.md) | Installing, checking, operating and fixing it - the path through the rest |
| [`docs/CONCEPT.md`](./docs/CONCEPT.md) | What this bridge does with them, and why each decision went the way it did |
| [`docs/CONFIG.md`](./docs/CONFIG.md) | Every setting, what it costs either way |
| [`docs/REFERENCE-CALL.md`](./docs/REFERENCE-CALL.md) | What a working call logs, so a broken one can be held against it |
| [`deploy/README.md`](./deploy/README.md) | The installation artifacts themselves |
| [`relay/README.md`](./relay/README.md) | For a gateway the bridge cannot reach directly |
| [`docs/TESTING.md`](./docs/TESTING.md) | How the tests are built: the three tiers, the helpers, and what a new one has to do to fit |
| [`nextcloud-app/README.md`](./nextcloud-app/README.md) | The admin app: what it is, why it proxies, and the one setting it has |
| [`tests/README.md`](./tests/README.md) | What is covered offline, and what needs real calls |

## How it is put together

Two connections to the signaling server, because one cannot do both jobs:
a session that joins a room stops being a dialout candidate for good. One
takes dialout requests and never enters a room; the other carries the
call. Each half of a call is its own module - `inbound_call`, `dialout`,
`call_audio` (the phone into the room), `human_audio` (the room to the
phone), `phone_participant` (the name plate, which carries no sound),
`room_presence` (who is there). `talk_client` is what routes between
them, `sip_call` owns the one call a line can have, and `signaling` keeps
the connections up.

## Tests

`tests/` needs no phone gateway, no signaling server and no network:

```
python3 -m unittest discover -s tests -t .
```

It covers that every module imports on its own (`test_build.py`), that the
calls modules make into each other exist there (`test_api.py`, read from the
source, so paths that only a hangup or a timeout reaches are covered too),
and what the message, SDP and request-building functions compute. Tests
needing the media stack (numpy, av, aiortc) skip themselves where it is not
installed; run the suite in the deployment venv for the full set.

`tests/hardware/` is the other kind: scripts that place real calls against a
gateway, a signaling server, or the Asterisk test peer. They are run by hand
and are not part of the suite above.

Tests live outside `bridge/` and are installed - or removed - separately from
the daemon; `bridge/` carries the runtime and nothing else. See
[`tests/README.md`](./tests/README.md).

`test-peer/` is an Asterisk in a container to point the bridge at instead
of whatever gateway a deployment happens to use, so that "works with my
box" and "speaks SIP" stay distinguishable. The recordings in
`tests/fixtures/fritzbox/` came off the wire from a FRITZ!Box because that
is what the first deployment ran against; a second gateway's recordings
belong beside them under its own name. It runs only while a test needs it.

### Which of these to run

What a change can break, not everything every time - the cost of a check
should stay below the cost of the change it guards.

| Changed | Worth running |
|---|---|
| `sip_messages`, `sip_sdp`, `sip_requests`, `payload_types` | `tests/` - sub-second, no setup |
| A module boundary: new module, moved code, changed signature | `tests/` - `test_build` and `test_api` are what catch it |
| `sip_call`, `sip_transport` | `tests/`, then `test_peer_outbound.py` / `test_peer_inbound.py` against the test peer |
| `sip_registrar` | `tests/`, then `tests/hardware/test_registration_recovery.py` - stops the test peer mid-flight and checks the line returns on its own |
| `rtp`, `g711`, `g722`, `agc` | `test_audio_quality.py`, and `test_audio_over_sip.py` for the real phone path |
| `room_state`, `talk_ocs`, `media`, `call`, `call_media` | `tests/` - covered offline, including against recorded signaling traffic |
| `talk_client`, `signaling`, `dialout`, `room_presence` | `tests/`, then a real dialout: place one, let it ring, end the call in Talk - nothing offline covers the two connections against a live server |
| `call_audio`, `human_audio`, `phone_participant` | `tests/`, then `tests/hardware/test_publish_and_verify.py` against a signaling server - it publishes a tone into a room and checks it by FFT, with no phone involved |
| `inbound_call`, `dialin_ivr` | `tests/`, then `tests/hardware/test_dialin_ivr.py` - dials a meeting id in and checks that a wrong one ends the call |
| Anything an inbound call touches | `tests/hardware/test_call_lifecycle.py <sip-phone2-password>` - places a real call at the running bridge and asserts ringing, answering and teardown without anybody present |
| Deployment, config, the relay host | A real call; nothing offline covers that path |

## Status

Not a prototype: this runs a real telephone line. What has actually been
confirmed, rather than what is implemented -

- `bridge/` - verified against the production gateway and signaling
  server: a dialout placed from Talk, an inbound call, a dial-in by
  meeting id, a caller who reaches the room before anybody is there, and
  a participant who leaves and comes back mid-call. Audio in both
  directions, G.722 preferred with PCMA and PCMU behind it and automatic
  gain control on the phone side; measured, not assumed, by an
  FFT-verified check with no phone involved
  (`tests/hardware/test_publish_and_verify.py`).
- `nextcloud-app/talk_sip_bridge/` - admin settings page (status/toggle),
  installed and verified on the production Nextcloud instance.
- `relay/` - for a gateway the bridge cannot reach directly: `sip_pipe.py`
  carries SIP between the two networks and `rtp_relay.py` the media. Only
  needed for that case; a directly reachable gateway needs neither.
- Deployed as the primary bridge (`deploy/`), running as the
  `talk-sip-bridge` systemd service.

## Licence

Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
Written by Anthropic Claude Opus 5: every line of code and documentation
here is AI generated content, directed and reviewed by the copyright
holder. Copyright (C) 2026 Thorsten Schnebeck.

| | |
|---|---|
| `nextcloud-app/talk_sip_bridge/` | **AGPL-3.0-or-later** |
| everything else | **GPL-3.0-or-later** |

The app is the one part with no free choice: it builds on Nextcloud's
`OCP` interfaces, and Nextcloud is AGPL-3.0-or-later. The two halves only
ever talk over HTTP, so nothing else is affected by it. Everything else
depends on nothing that constrains the choice - `aiortc`, `av`, `numpy`
and `websockets` are BSD-licensed, the relay is pure standard library, and
`test-peer/` ships configuration rather than any part of Asterisk.

Both licence texts are in [`LICENSES/`](./LICENSES) verbatim, and every
file carries its own header. Files that cannot - JSON, and the recordings
under `tests/fixtures/` that are read back byte for byte - are covered by
[`REUSE.toml`](./REUSE.toml). The project follows the
[REUSE](https://reuse.software) specification, which is also what
Nextcloud itself uses; `tests/test_headers.py` checks that every file is
covered, that each header names the file it sits in, and that a Python
header still says what its module docstring says.

This program is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
or FITNESS FOR A PARTICULAR PURPOSE.
