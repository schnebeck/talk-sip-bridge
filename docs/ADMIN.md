<!--
talk-sip-bridge - docs/ADMIN.md
Running this bridge: what to install, what to check, what the failures
look like.

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

# Running this bridge

From a Nextcloud and a phone line to a working bridge, and everything
after that: what to check, what the failures look like, and which
settings have consequences.

For what the interfaces are, see [`SIGNALING-API.md`](./SIGNALING-API.md)
and [`SIP-API.md`](./SIP-API.md); for why it is built this way,
[`CONCEPT.md`](./CONCEPT.md); for every setting one by one,
[`CONFIG.md`](./CONFIG.md). This document is the path through them.

## What has to exist first

| | | Without it |
|---|---|---|
| A SIP account on a gateway | user, password, host | nothing to register |
| A standalone Talk signaling server | its WebSocket URL and its `internalsecret` from `server.conf` | no way to reach Talk at all |
| Nextcloud with the Talk app | its base URL | — |
| A host that reaches both | or a relay host — see [`relay/README.md`](../relay/README.md) | see the relay case |

**One line carries one call at a time.** Everything below is per line. A
second concurrent call needs a second SIP account, with its own ports and
its own pair of signaling connections (`BRIDGE_LINES`).

Two things to know before planning on that. **No deployment has yet run
more than one line against real hardware** - the configuration is covered
by offline tests, the telephony is not. And the lines are **not a pool**:
which one a dialout request reaches is the signaling server's choice, and
a request handed to a busy line is refused rather than passed to a free
one.

## Installing

1. **A system user and a venv.**

   ```
   useradd --system --no-create-home talk-sip-bridge
   python3 -m venv /opt/talk-sip-bridge-venv
   /opt/talk-sip-bridge-venv/bin/pip install -r bridge/requirements.txt
   install -d -o talk-sip-bridge /opt/talk-sip-bridge
   install -o talk-sip-bridge bridge/*.py /opt/talk-sip-bridge/
   ```

   `bridge/` holds the daemon and nothing else. The tests are installed
   separately or not at all — [`tests/README.md`](../tests/README.md).

2. **Configuration**, from [`deploy/env.example`](../deploy/env.example) to
   `/etc/talk-sip-bridge/env`, `chmod 600`, owned by the service user. The
   seven required values:

   ```
   BRIDGE_SIP_USER          BRIDGE_WS_URL
   BRIDGE_SIP_PASS          BRIDGE_INTERNAL_SECRET
   BRIDGE_GATEWAY_HOST      BRIDGE_BACKEND_URL
   BRIDGE_LOCAL_IP
   ```

   **`BRIDGE_SIP_TRANSPORT` is the setting to get right early.** Its
   default is UDP and a FRITZ!Box drops UDP registrations **without a
   word** — no error, no response, just a line that never registers. If
   registration does not come up, try `tcp` before anything else.

3. **The unit**, from [`deploy/talk-sip-bridge.service`](../deploy/talk-sip-bridge.service)
   to `/etc/systemd/system/`, then `systemctl daemon-reload && systemctl
   enable --now talk-sip-bridge`.

4. **Three Talk settings**, or the native UI does not appear and SIP
   requests are refused. What each one does is in
   [`SIGNALING-API.md`](./SIGNALING-API.md#prerequisites-in-nextcloud):

   ```
   occ config:app:set spreed sip_bridge_shared_secret --value='<a generated secret>'
   occ config:app:set spreed sip_bridge_dialin_info   --value='<text users see>'
   occ config:app:set spreed sip_dialout              --value='yes'
   ```

   `sip_bridge_dialin_info` only has to be non-empty — Talk treats an
   empty one as "SIP is not configured" — but it is not a placeholder:
   it goes into the **invitation e-mail** a guest receives, under
   "Dial-in information", beside the meeting id and their PIN
   (`GuestManager::sendEmailInvitation`). It has to say which number to
   call, and it is worth saying so when the answer is "none you can
   reach": a recipient outside the phone system otherwise looks for a
   number that is not there. It should also say that the meeting id and
   the PIN are each finished with `#` — the prompt is two beeps, and a
   beep cannot say that.

   Write it in the language its recipients read. Talk translates the
   heading above it and the labels beside it, never this text.

   While evaluating, restrict who sees the call button with
   `sip_bridge_groups`.

5. **The admin app**, optionally — see
   [`nextcloud-app/README.md`](../nextcloud-app/README.md) for what it is
   and is not: copy
   [`nextcloud-app/talk_sip_bridge`](../nextcloud-app/talk_sip_bridge) into
   `custom_apps/` and `occ app:enable talk_sip_bridge`. It shows the line's
   status and lets registration be switched on and off. If the daemon's
   control API is not at `http://127.0.0.1:8765` as seen from Nextcloud —
   a containerised Nextcloud reaches the host at a different address —
   set it: `occ config:app:set talk_sip_bridge bridge_url --value='http://<address>:<port>'`.

**Registration starts off.** Switch it on in the app's admin settings, or
`curl -X POST http://127.0.0.1:8765/toggle`. That state is remembered
across restarts in `/var/lib/talk-sip-bridge`, so a crash comes back
registered rather than silently unreachable.

## Checking that it works

```
systemctl is-active talk-sip-bridge
curl -s http://127.0.0.1:8765/status
journalctl -u talk-sip-bridge -n 20
```

A healthy idle bridge answers `"registered": true, "last_error": null,
"active_call": null` and logs **five lines at startup and then nothing**.
Silence is the correct state: a registration refresh happens every few
minutes and says nothing unless it fails.

```
[talk] room connection up as internal client, session <id>
[talk] dialout connection up as internal client, session <id>
[sip:default] TCP connection to <gateway or relay> established
[daemon] Line default (<user>@<gateway>) was registered before restart - resumed: True (last_error=None)
[daemon] Control API on 127.0.0.1:8765 (1 line(s): default)
```

**Two signaling connections, always.** One missing means half a bridge:
either no dialout, or no calls at all.

What a call then looks like, line by line and with the numbers to compare
against, is [`REFERENCE-CALL.md`](./REFERENCE-CALL.md).

## Operating it

| | |
|---|---|
| `GET /status` | every line: registered, username, proxy, last error, active call |
| `POST /toggle` | registers the line, or deregisters it |
| `POST /hangup` | ends whatever call is on the line - the escape hatch for a call that is stuck |

Add `?line=<id>` for a specific line; without it the first configured one
is used. **None of this is authenticated.** `BRIDGE_CONTROL_BIND` must
name an address that Nextcloud can reach and nobody else can — loopback,
or a container bridge address. Never a public interface.

### The control API in full

Three endpoints, no authentication, no versioning, JSON in both
directions (`bridge/control_api.py`). The admin app is one client of it;
anything that can reach the address is another.

**Every response is a status document**, including the answer to a
`POST`, so nothing has to ask again to see what changed:

```json
{
  "line": "default",
  "registered": true,
  "username": "sip-phone",
  "proxy": "192.0.2.1:5060",
  "last_error": null,
  "active_call": null,
  "lines": [ { … the same fields, once per configured line … } ]
}
```

| Field | |
|---|---|
| `line` | the line's id — `default` unless `BRIDGE_LINES` names others |
| `registered` | whether the gateway currently holds a registration for it |
| `username` | the SIP user it registers as |
| `proxy` | `host:port` it registers with — the relay's address where one is used, not the gateway's |
| `last_error` | why the last registration attempt failed, as a string, or `null`. Cleared by the next attempt that succeeds, so `registered: true` always comes with `last_error: null` |
| `active_call` | `null`, or an object |
| `lines` | always present: one entry per configured line, each the fields above |

**`active_call`**, when there is one:

```json
{"direction": "inbound", "status": "connected", "number": "**622"}
```

`direction` is `inbound` or `outbound`; `status` is `ringing`, `dialing`
or `connected`; `number` is the other party as the gateway wrote it, or
`null`.

**The single-line shape is the flat one.** With one line configured — the
common case — that line's fields sit at the top level *and* in `lines`.
With several, only `lines` carries them and the top level has just that
array. A client that reads `lines[0]` works in both cases; one that reads
`registered` at the top level silently reads the first line's state once
a second is added.

| Request | Answers |
|---|---|
| `GET /status` | `200` with the document above |
| `POST /toggle` | `200` when the gateway agreed, `502` when the attempt did not get through — `last_error` then says why. Both carry the full status |
| `POST /hangup` | `200` and the status. Ending nothing is not an error |
| any other path | `404` `{"error": "not found"}` |
| `?line=<id>` naming a line that is not configured | `404` `{"error": "unknown line 'x'"}` |

`?line=<id>` selects the line for `POST`; `GET /status` ignores it and
always reports everything.

Three things a client must not assume.

**`POST /toggle` has no direction.** It registers a line that is off and
deregisters one that is on, so two clients racing each other end up
wherever the second one left it. Read `registered` back rather than
tracking it.

**A `502` is not final.** Switching a line on marks it as wanted and
starts the keepalive, which keeps retrying every few seconds whatever
the first REGISTER did. `502` says the gateway had not agreed *yet*, not
that the line is off — a client that treats it as a failure and offers
to try again is offering something already happening. Poll `/status`.

**The server holds no state of its own.** It reports what the registrar
and the call manager say at that instant, so polling it during a call is
cheap and safe.

### Updating without cutting a call

```
curl -s http://127.0.0.1:8765/status | grep active_call   # must be null
install -o talk-sip-bridge bridge/*.py /opt/talk-sip-bridge/
systemctl restart talk-sip-bridge
```

Check first, in its own step. A restart during a call drops it without
warning to either side, and the check is worth nothing if it runs in the
same breath as the restart that ignores it.

### What holds secrets

`/etc/talk-sip-bridge/env` carries the SIP password, the signaling
server's internal secret, and — if the ring feature is configured — a
Nextcloud app password, all in plain text. Mode 0600, owned by the
service user. The unit runs with `ProtectSystem=strict`, no capabilities,
and one writable directory.

## When it does not work

| What you see | Where to look |
|---|---|
| Never registers, no error anywhere | `BRIDGE_SIP_TRANSPORT`. A FRITZ!Box drops UDP silently; TCP-only providers are common |
| Registers, but inbound calls never arrive | `BRIDGE_CONTACT_HOST`/`_PORT`. A registrar delivers a call by connecting to the **Contact** address, not by answering on the registration's connection - so a wrong Contact costs inbound calls only, while registration keeps succeeding |
| "The phone number could not be called" in Talk | The dialout connection is not eligible. It must never have joined a room; check for `room connection`/`dialout connection` both being up |
| Call connects, nobody hears anything | Compare against [`REFERENCE-CALL.md`](./REFERENCE-CALL.md). "Talk's audio reaches the phone" is the line that says the return direction works |
| Call connects, the phone hears nothing | Look for `Requested audio from`. Without it, nobody was found to listen to |
| A key press does nothing | `BRIDGE_DTMF_DEBUG=true` for one call: it says which of the three roads carried the press, and writes what the far end sent to a WAV |
| Calls end a few seconds in | `BRIDGE_EMPTY_ROOM_GRACE`. A client saving a microphone setting leaves the call and rejoins |
| Nothing in the journal explains it | `BRIDGE_SIGNALING_DEBUG=true` logs every message the bridge did not act on. It buries everything else, so turn it off again |

Both debug switches record real call content — audio in one case, message
payloads in the other. They belong on for one call, not for a week.

## Settings with consequences

Everything is in [`CONFIG.md`](./CONFIG.md); these four decide how the
bridge behaves towards people rather than towards machines.

- **`BRIDGE_AUTO_ANSWER`** — off by default, and on a line that also
  carries a person's own number it should stay off. On, the bridge picks
  up every inbound call before any phone can ring.
- **`BRIDGE_CONFERENCE_NUMBERS` / `_CALLERS`** — which numbers reach the
  dial-in prompt, and **who may reach it**. The callers pattern is the
  safety property: `[*][*][0-9]+` admits the gateway's own extensions and
  leaves every external call ringing as before. An empty pattern admits
  everybody.
- **`BRIDGE_DIALIN_NUMBERS`** — maps a dialled number to the public
  number Nextcloud knows, which is what makes Nextcloud create a
  conversation for the call and the caller a participant of it. Only
  numbers named here take that path; everything else rings as usual.
- **`BRIDGE_NOTIFY_USER` / `_APP_PASSWORD` / `BRIDGE_DEFAULT_ROOM`** —
  the only way a call arriving on an ordinary number reaches Talk at all.
  The signaling protocol has no ringing exchange for inbound calls, so
  the bridge signs into that Nextcloud account and joins the room's call
  the way a client would, which is what makes other devices ring. It
  needs a **real account with an app password**, a member of that room.
  Leave all three empty and inbound calls ring unnoticed, which is the
  safe default.

## Removing it

```
systemctl disable --now talk-sip-bridge
rm /etc/systemd/system/talk-sip-bridge.service /etc/talk-sip-bridge/env
rm -rf /opt/talk-sip-bridge /opt/talk-sip-bridge-venv /var/lib/talk-sip-bridge
occ app:remove talk_sip_bridge
```

The `spreed` settings from step 4 are Talk's own and stay unless they are
unset. Leaving `sip_dialout` on with no bridge running gives users a call
button whose calls go nowhere.
