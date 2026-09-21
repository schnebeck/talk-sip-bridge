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

A standard Nextcloud installation comes with Talk, and Talk is a capable
meeting tool. Chat, audio and video calls, screen sharing, guests by link.
For many teams it does the job of Microsoft Teams.

One thing it cannot do out of the box: let someone join a call by
telephone. No dial-in number, and no way to ring a phone from a meeting.

Talk's own side of this is finished. It knows what a phone participant is.
It has a *Call a phone number* button. It gives every invited guest a PIN.
And the interface a bridge plugs into is public and documented. Only the
bridge itself is missing. Nextcloud sells one, but it is closed source,
and no open implementation of that interface has been published.

**This is one.** It fills that gap, and then:

- Someone rings your number. They type the meeting ID, and a PIN if the
  meeting has one. Now they are in the call and can talk.
- Or a number leads straight into one conversation, with nothing to type.
- Someone in a conversation presses *Call a phone number*, and a phone
  rings.
- Either way, Nextcloud adds the caller as a participant. They appear with
  a name, and anyone can mute them or hang up on them.

That covers the people a meeting keeps losing: no laptop to hand, a weak
connection, or simply unwilling to install an app.

Nobody has to learn anything for this. It is Talk's own interface
throughout, with no bot and no chat commands.

**You can set up several numbers.** One phone account carries as many
dial-in numbers as you like. A second account allows a second call at the
same time. The gateway, the transport and the dial plan are all just
settings. A home VoIP box and a business PBX both work.

**It is small.** One program, one service file, a few settings. No extra
server, no database. The line stays off until someone turns it on, from a
page in Nextcloud's admin settings or with one command. It answers only the
numbers you give it, and only calls from people you allow. If an account
also carries a private number, that phone keeps ringing as before. This
will never pick up a call meant for a person.

**What it cannot do.** One phone account handles one call at a time. A
second caller hears the busy tone. More accounts mean more calls at once. They
are not a pool yet, though. If the account that gets the request is busy,
the call fails, even when another one is free. Outgoing calls cannot be
assigned to a person or a team;
[what can be assigned](#what-can-be-assigned-and-what-cannot) says how
far that goes and why. Voice only, no video. The phone side of a call is not encrypted, so keep the gateway on a
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

The flat `BRIDGE_SIP_USER=...` block above is the short form for a single
account. Set `BRIDGE_LINES` and each account gets its own prefix instead:

```
BRIDGE_LOCAL_IP=192.0.2.10                  # these four stay global
BRIDGE_WS_URL=ws://127.0.0.1:8080/spreed
BRIDGE_INTERNAL_SECRET=...
BRIDGE_BACKEND_URL=https://cloud.example

BRIDGE_LINES=office,support

BRIDGE_LINE_office_SIP_USER=...
BRIDGE_LINE_office_SIP_PASS=...
BRIDGE_LINE_office_GATEWAY_HOST=192.168.1.1
BRIDGE_LINE_office_LOCAL_SIP_PORT=5101
BRIDGE_LINE_office_LOCAL_RTP_PORT=41000

BRIDGE_LINE_support_SIP_USER=...
BRIDGE_LINE_support_SIP_PASS=...
BRIDGE_LINE_support_GATEWAY_HOST=sip.provider.example
BRIDGE_LINE_support_LOCAL_SIP_PORT=5102
BRIDGE_LINE_support_LOCAL_RTP_PORT=41010
```

An id is any word of letters, digits and underscores. **Every** per-line
setting takes the same prefix, so `BRIDGE_CONFERENCE_NUMBERS` becomes
`BRIDGE_LINE_office_CONFERENCE_NUMBERS`. The four shown above are the
ones that stay flat, along with the AGC and control-API settings.

Watch the ports. In this form `LOCAL_SIP_PORT` and `LOCAL_RTP_PORT` have
no default, and each account needs its own. In the single-account form
they default to 5060 and 40000.

The accounts are independent, so they can point at entirely different
gateways: a VoIP box in the building for internal extensions, and a
provider's trunk for outside calls.
[`deploy/test-multiline.env.example`](./deploy/test-multiline.env.example)
is a ten-account template, and [`CONFIG.md`](./docs/CONFIG.md#single-line-vs-multiple-lines)
lists every setting that can be prefixed.

Which account handles what is the subject of the next section.

## What can be assigned, and what cannot

The two directions are not symmetrical, and one sentence explains the
whole of it:

> **An incoming call says which number was dialled. An outgoing call says
> nothing about who is calling.**

So the incoming side can be assigned in detail, and the outgoing side
almost not at all.

| What you may want | |
|---|---|
| This number always reaches that conversation | **Yes.** One entry per number in `BRIDGE_DIALIN_NUMBERS` |
| This number asks the caller which meeting they want | **Yes.** `BRIDGE_CONFERENCE_NUMBERS` |
| Only these callers may dial in | **Yes.** `BRIDGE_CONFERENCE_CALLERS`, per account |
| Use an account for outgoing calls only | **Yes.** Leave its four inbound settings unset and calls arriving there just ring |
| Keep an account *out* of outgoing calls | **No.** Every account offers itself for outgoing, and the signaling server picks |
| Each user dials out with their own number | **No.** Talk does not say who started the call, and the number shown is the account's own |
| Each team dials out with its own number | **Not from the dial-a-number dialog** — see below. From a conversation that already exists it would be possible, but it is not built |
| Move to a free account when one is busy | **No.** The request is refused, not passed on |
| A different caller ID per call | **No.** One SIP account is one identity |
| Restrict who may call which numbers | **Per account only**, with `BRIDGE_DIALOUT_NUMBER_ALLOWLIST`. Since you cannot choose the account, that is in practice per installation |

**A caller hears one participant, not the room.** Measured: two people
played different tones in the same conversation, and the caller's audio
carried one of them at full level and the other 57 dB down, which is
silence (`tests/hardware/test_two_publishers.py`). Following whoever is
speaking is not a way out either - the signaling server never says who
that is, and does not even report muting
([measured](./docs/SIGNALING-API.md#the-server-does-not-say-who-is-speaking)).
Carrying more than one would mean subscribing to each and mixing, which
is not built.

**Why the dialog does not help.** In Talk you dial a number without
picking a conversation. That dialog creates a **call room** at that
moment. From then on you re-dial from inside it, and you can add further
numbers to it. So the room is neither the caller nor the callee, it is
one dialling session, and its id does not exist until somebody dials.
There is nothing for an administrator to map an account to.

Two of the noes are ours to fix: moving to a free account, and routing
from a conversation that already exists. The rest need Talk to name the
person who started the call, and today it does not.

**Two optional parts.** Install [`nextcloud-app/`](./nextcloud-app) for
the admin page that switches the line on and off. Use
[`relay/`](./relay) if the gateway sits in a different network.

## The documents

Two of them describe the two interfaces. Together they should be enough
to build a bridge like this one from scratch. They also cover the parts
that upstream does not document, and the places where a real gateway or a
real client does not match the specification.

| | |
|---|---|
| [`docs/SIGNALING-API.md`](./docs/SIGNALING-API.md) | The Talk side: the signaling server's internal-client protocol and Talk's OCS call API |
| [`docs/SIP-API.md`](./docs/SIP-API.md) | The phone side: registration, calls in both directions, SDP, RTP, key presses |
| [`docs/ADMIN.md`](./docs/ADMIN.md) | How to install it, check it, run it and fix it |
| [`docs/CONCEPT.md`](./docs/CONCEPT.md) | How this bridge uses those interfaces, and why each choice was made |
| [`docs/CONFIG.md`](./docs/CONFIG.md) | Every setting, and what it costs you either way |
| [`docs/REFERENCE-CALL.md`](./docs/REFERENCE-CALL.md) | What a working call looks like in the log, so you can compare a broken one |
| [`deploy/README.md`](./deploy/README.md) | The files you install, and where they go |
| [`relay/README.md`](./relay/README.md) | For a gateway the bridge cannot reach directly |
| [`docs/TESTING.md`](./docs/TESTING.md) | How the tests work, and how to write one that fits |
| [`nextcloud-app/README.md`](./nextcloud-app/README.md) | The admin page: what it is, why it proxies, its one setting |
| [`tests/README.md`](./tests/README.md) | Which tests run offline, and which need real calls |

## How it is put together

The bridge keeps **two** connections to the signaling server, because one
cannot do both jobs. Once a session joins a room, the server stops
offering it outgoing calls, permanently. So one connection only takes
those requests and never joins a room. The other carries the call.

Each part of a call is its own module:

| | |
|---|---|
| `inbound_call`, `dialout` | a call arriving, and a call being placed |
| `call_audio`, `human_audio` | the phone into the room, and the room back to the phone |
| `phone_participant` | the name in the participant list, which carries no sound |
| `room_presence` | who else is there |
| `talk_client` | routes between all of them |
| `sip_call` | owns the one call an account can have |
| `signaling` | keeps both connections up |

## Tests

`tests/` needs no phone gateway, no signaling server and no network:

```
python3 -m unittest discover -s tests -t .
```

They check that every module imports on its own (`test_build.py`). They
check that the methods one module calls on another really exist there
(`test_api.py`). That one reads the source instead of running it, so even
code that only a hangup or a timeout reaches is covered. And they check what the SIP, SDP and
signaling functions actually compute.

Some tests need numpy, av and aiortc. Those skip themselves when the
libraries are missing, so the suite still says something useful on a bare
machine. Run it in the deployment venv to get all of it.

`tests/hardware/` is the other kind: scripts that place real calls. They
need a gateway, a signaling server or the Asterisk test peer, they are run
by hand, and they are not part of the suite above.

The tests live outside `bridge/` and are installed, or deleted, separately
from the daemon. `bridge/` holds the running code and nothing else. See
[`tests/README.md`](./tests/README.md) for what each test covers, and
[`docs/TESTING.md`](./docs/TESTING.md) for how to write one.

`test-peer/` is an Asterisk in a container. Pointing the bridge at it
instead of a real gateway keeps "works with my box" apart from "speaks
SIP". The recordings in `tests/fixtures/fritzbox/` came off the wire from
a FRITZ!Box, because that is what the first deployment used. Recordings
from another gateway belong beside them, under its own name.

### Which of these to run

Run what the change can break, not everything every time. A check should
cost less than the change it guards.

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

This is not a prototype. It runs a real phone line. Below is what has
actually been confirmed, not what has been written:

- **`bridge/`** runs against a real gateway and a real signaling server.
  Confirmed there: a call placed from Talk, a call coming in, a dial-in by
  meeting ID, a caller who arrives before anyone else is in the room, and
  a participant who leaves and comes back during the call.
- **Audio works in both directions.** G.722 first, then PCMA and PCMU,
  with automatic gain control on the phone side. This was measured, not
  assumed: `tests/hardware/test_publish_and_verify.py` publishes a tone
  and checks it by FFT, with no phone involved.
- **`nextcloud-app/`** is installed on that Nextcloud and works. It shows
  the line's status and switches it on and off.
- **`relay/`** is only for a gateway the bridge cannot reach directly.
  `sip_pipe.py` carries the SIP, `rtp_relay.py` the audio. If your gateway
  is reachable, you need neither.
- It runs as a systemd service from [`deploy/`](./deploy), as the primary
  bridge on that installation.

## Licence

Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
Written by Anthropic Claude Opus 5: every line of code and documentation
here is AI generated content, directed and reviewed by the copyright
holder. Copyright (C) 2026 Thorsten Schnebeck.

| | |
|---|---|
| `nextcloud-app/talk_sip_bridge/` | **AGPL-3.0-or-later** |
| everything else | **GPL-3.0-or-later** |

The app is the one part with no choice. It builds on Nextcloud's `OCP`
interfaces, and Nextcloud is AGPL. The app and the daemon only ever talk
over HTTP, so nothing else is affected.

Everywhere else the choice was free, and nothing forced it either way.
`aiortc`, `av`, `numpy` and `websockets` are BSD-licensed. The relay uses
only the standard library. `test-peer/` ships configuration, not any part
of Asterisk.

Both licence texts sit in [`LICENSES/`](./LICENSES) word for word, and
every file carries its own header. A few files cannot: JSON has no
comments, and the recordings under `tests/fixtures/` are read back byte
for byte. Those are listed in [`REUSE.toml`](./REUSE.toml) instead. The
layout follows the [REUSE](https://reuse.software) specification, which
is what Nextcloud uses too. `tests/test_headers.py` checks that no file
is missed, that each header names the file it sits in, and that a Python
header still says what its module says.

This program is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
or FITNESS FOR A PARTICULAR PURPOSE.
