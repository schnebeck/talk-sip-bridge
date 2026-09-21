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

**Gives Nextcloud Talk a phone number.** Someone calls it. They type in
the meeting ID, and a PIN if the meeting has one. Then they are in the
meeting and can talk. Nextcloud adds them as a participant, so they appear
with a name, and anyone can mute them or hang up on them. This helps when
a person has no laptop, a weak connection, or will not install an app.

It works the other way round too. In a conversation, press *Call a phone
number*, and a phone rings. Both directions carry real audio, and both use
the phone support Talk already has. No bot, no chat commands, nothing new
for anyone to learn.

**You can set up several numbers.** A number can lead straight to one
conversation, or it can ask the caller which meeting they want. One phone
account carries as many such numbers as you like. A second account allows a
second call at the same time. The gateway, the transport and the dial plan
are all just settings. A home VoIP box and a business PBX both work.

**It is small.** One program, one service file, a few settings. No extra
server, no database. The line stays off until someone turns it on, from a
page in Nextcloud's admin settings or with one command. It answers only the
numbers you give it, and only calls from people you allow. If an account
also carries a private number, that phone keeps ringing as before. This
will never pick up a call meant for a person.

**What it cannot do.** One phone account handles one call at a time. A
second caller hears the busy tone. More accounts mean more calls at once. They
are not a pool yet, though. If the account that gets the request is busy,
the call fails, even when another one is free. You also cannot give each
Nextcloud user their own outgoing number. Talk tells the bridge which
conversation a call belongs to, but not who started it. Voice only, no
video. The phone side of a call is not encrypted, so keep the gateway on a
network you trust. The SIP dialect here is plain and a little
old-fashioned. Most gateways and providers accept it. A strict one, or a
chain of SIP proxies, may not.
[`CONCEPT.md`](./docs/CONCEPT.md#known-gaps-in-the-sip-implementation)
lists every gap and what it costs.

**What it has been tested on.** One setup. A FRITZ!Box with one external
line and several internal SIP devices: a DECT handset, a softphone, and a
spare account playing the caller. Everything called confirmed further down
was confirmed there. An Asterisk 20 container covers the protocol side,
which keeps "works with my box" apart from "speaks SIP". Never tested: a
provider's SIP trunk, a PBX with several external lines, a real public
dial-in number, and more than one account at once. Multiple accounts are
covered by offline tests and by the design, not by a phone. None of this is
known to break. It is simply unproven. If you try one of them,
`tests/fixtures/` is where a new gateway's behaviour gets written down.

**What you need.** A phone account: username, password, address, the kind a
VoIP box or a phone provider gives you. A Nextcloud with Talk and its
separate signaling server, the High Performance Backend - the built-in one
cannot do this. And a small Linux machine with Python 3.10 or newer that
reaches both. If it cannot reach the gateway directly, the
[`relay/`](./relay) here joins the two networks.

## Getting the first number running

The short path: one phone account, a gateway the bridge host can reach
directly, and calls placed from Talk. Dial-in comes in the next section.
[`ADMIN.md`](./docs/ADMIN.md) walks the same path, but explains each step
and what to do when it fails.

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

This is what most people install it for. There are two kinds of number,
and one account can carry both:

| | |
|---|---|
| `BRIDGE_CONFERENCE_NUMBERS` | **One number, any meeting.** The bridge answers and asks which meeting the caller wants. They type the meeting ID, and a PIN if there is one. |
| `BRIDGE_DIALIN_NUMBERS` | **One number, one meeting.** The caller types nothing. The number leads straight to the conversation Nextcloud has registered for it with `occ talk:phone-number:add`. You can map many numbers to many conversations on one account. |

**`BRIDGE_CONFERENCE_CALLERS` decides who gets that far.** It is a
regular expression, and the caller's number has to match all of it. Leave
it empty and every caller in the world is let through. That is what you
want for a public dial-in number, and what you do not want for anything
else.

Nextcloud handles the rest. It creates a participant for the caller, so
they show up by number instead of as an anonymous guest. And the PIN it
mails to each invited person is the one the prompt asks for. For spoken
prompts in your own languages, use
[`deploy/make-ivr-prompts.sh`](./deploy/make-ivr-prompts.sh). Without
them, callers just hear two beeps and will not know what to do.

## More than one number at a time

You only need a second phone account if you want a second call at the
same time. Numbers themselves are free: one account can serve many dial-in
numbers, because either way only one call can use it at once.

`BRIDGE_LINES` puts several accounts in one daemon, each with its own
settings, ports and connections.
[`deploy/test-multiline.env.example`](./deploy/test-multiline.env.example)
shows ten of them.

Two things are missing here. You cannot pick the outgoing number per
Nextcloud user, because Talk does not tell the bridge who started the
call. And the accounts are not a pool: a call handed to a busy account
fails instead of moving to a free one. The first needs a change in Talk.
The second is ours to fix.

**Two optional parts.** Install [`nextcloud-app/`](./nextcloud-app) for
the admin page that switches the line on and off. Use
[`relay/`](./relay) if the gateway sits in a different network.

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
