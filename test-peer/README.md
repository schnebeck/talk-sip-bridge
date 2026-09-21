<!--
talk-sip-bridge - test-peer/README.md
The Asterisk test peer: a second SIP endpoint to call and be called by.

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

# SIP test peer

An Asterisk the bridge can be pointed at instead of the FritzBox, so that
"works with the FritzBox" and "speaks SIP" stay distinguishable. A bridge
developed against a single gateway quietly grows that gateway's assumptions;
a second, standards-focused peer is what turns that from an opinion into a
test result.

Runs on a development machine, not on any host carrying production traffic.
It needs nothing from the phone network and nothing from the relay host.

## Passive unless a test is running

Started for a test and stopped afterwards. `restart: "no"` in the compose
file means it does not come back when Docker or the machine restarts, and
nothing enables it at boot. At rest it costs **the image on disk (~390 MB)
and nothing else** - no process, no port, no service unit.

Every listener is bound to `127.0.0.1` in `etc/pjsip.conf`, so even while
running it is reachable from the machine it runs on and nowhere else.

```
docker compose up -d      # or: docker-compose up -d
docker compose down       # when the test is over
```

## What is configured

Two accounts, both `test-peer-secret`, differing in how forgiving they are:

| Endpoint | Behaviour |
|---|---|
| `bridge-a` | `rewrite_contact`, `force_rport`, `rtp_symmetric` - the NAT-tolerant setup most deployments use |
| `bridge-b` | None of those: takes the Contact header at its word and sends calls exactly where it says |

The strict one is the interesting one. A client whose Contact does not
describe a reachable address registers against `bridge-a` and looks fine,
and fails against `bridge-b`.

Both transports listen on port 5060, so the bridge can be pointed at UDP or
TCP without changing anything here.

Dialplan (`etc/extensions.conf`), one extension per thing a test needs to
assert without a person on the other end:

| Extension | Does |
|---|---|
| `100` | Answers and echoes audio back - both directions, comparable against what was sent |
| `200` | Answers and plays a fixed file - a deterministic audio source |
| `300` | Rings for five seconds, then answers and echoes |
| `400` | Rings forever - the outbound timeout and CANCEL path |
| `500` | Rejects immediately - a final non-2xx, and the ACK the RFC requires for it |

## Watching the protocol

```
docker exec sip-test-peer asterisk -rx "pjsip set logger on"
docker compose logs -f
```

That prints every SIP message in full, which is also the way to capture
real messages from a standards-focused server as test fixtures.

Useful state:

```
docker exec sip-test-peer asterisk -rx "pjsip show endpoints"
docker exec sip-test-peer asterisk -rx "pjsip show contacts"
```

## Pointing the bridge at it

A line's configuration, nothing else (see `../docs/CONFIG.md`):

```
BRIDGE_SIP_USER=bridge-a
BRIDGE_SIP_PASS=test-peer-secret
BRIDGE_GATEWAY_HOST=127.0.0.1
BRIDGE_LOCAL_IP=127.0.0.1
BRIDGE_LOCAL_SIP_PORT=5062      # not 5060, which the peer owns
BRIDGE_LOCAL_RTP_PORT=41500
```

No media relay: the peer is on the same machine and reachable directly.

## Why Ubuntu

Debian no longer ships an `asterisk` package - neither bookworm nor trixie
has an installation candidate. Ubuntu 24.04 carries Asterisk 20 LTS, whose
pjsip stack is the strict, current one.
