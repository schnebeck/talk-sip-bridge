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

Connects a SIP phone line to Nextcloud Talk using Talk's native SIP bridge
protocol: incoming calls appear as real, named "phone" participants in a
room, and outgoing calls are placed from Talk's own call UI.

Any SIP registrar will do - which gateway, which transport, which dial-plan
notation are configuration, one line at a time. What is specific to a
gateway is named as such: the recorded messages in `tests/fixtures/fritzbox/`
came off the wire from a FRITZ!Box, which is also what this deployment runs
against and what `test-peer/` exists to keep honest. The one real dependency
on it today is digest authentication in its simple form, without `qop` -
see the known gaps in `docs/CONCEPT.md`.

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

`test-peer/` is an Asterisk in a container to point the bridge at instead of
the FritzBox, so that "works with the FritzBox" and "speaks SIP" stay
distinguishable. It runs only while a test needs it.

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

- `bridge/` - the daemon: gateway registration, bidirectional real audio
  (G.722 preferred, then PCMA and PCMU, with automatic gain control on the
  phone side) for inbound and outbound calls, native Talk dialout integration,
  reliable call-end detection, dial-in with a spoken meeting id, local HTTP
  control API (status/toggle). Verified against the production gateway and
  signaling server: dialout, inbound, dial-in, a caller who joins before
  anybody is there, and a participant who leaves and comes back mid-call -
  plus an independent FFT-verified audio check with no phone involved
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
