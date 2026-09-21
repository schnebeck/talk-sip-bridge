<!--
talk-sip-bridge - nextcloud-app/README.md
The Nextcloud app: a switch for the phone line, and nothing else.

  Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
  Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
  Written by Anthropic Claude Opus 5 - AI generated content.

  Free software under the GNU Affero General Public License, version 3 or
  later. There is no warranty, to the extent permitted by law. The full
  text is in LICENSES/AGPL-3.0-or-later.txt.

SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
SPDX-FileContributor: Anthropic Claude Opus 5 (AI generated content)
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# The Nextcloud app

An admin settings panel showing whether the phone line is registered,
with a button that turns it on and off. That is the whole app.

**No call goes through it.** Calls are Talk's own: incoming ones appear
as phone participants through the signaling server, outgoing ones are
placed from Talk's native "call a phone number" UI. Removing this app
changes nothing about telephony — the bridge keeps running, and the line
can be switched with `curl` instead. It exists so that switching it does
not require a shell on the bridge host.

## What it is made of

| | |
|---|---|
| `appinfo/info.xml` | the manifest: id, version, the admin section and panel it registers |
| `appinfo/routes.php` | two routes, `GET /status` and `POST /toggle` |
| `lib/Controller/BridgeController.php` | the only code with any behaviour: it forwards both to the daemon |
| `lib/Settings/AdminSection.php` | the entry in Nextcloud's admin sidebar (phone icon, priority 80) |
| `lib/Settings/AdminSettings.php` | the panel inside that section |
| `templates/admin.php` | its markup — a status line and a button |
| `js/admin-settings.js` | fills them in: reads `/status` at load and every 5 s, posts `/toggle` on click |
| `css/admin-settings.css` | spacing for the two of them |
| `l10n/de.*` | German. Source strings are English and go through `t()` — see [`../docs/CONCEPT.md`](../docs/CONCEPT.md) |

## Why a proxy and not a direct call

The browser cannot reach the daemon. Its control API binds to an address
reachable from the Nextcloud host — loopback, or a container bridge
address — and is deliberately not exposed anywhere else. So the page
talks to Nextcloud, and Nextcloud talks to the daemon.

That is also what supplies the authentication the control API does not
have. It has none by design: it is unauthenticated, unversioned, and
safe only because of where it is bound. Nextcloud puts a session and a
CSRF token in front of it.

**Both endpoints are admin-only**, and that is Nextcloud's default rather
than something this app declares: a controller method requires an admin
unless it is annotated otherwise. `BridgeController` carries no
annotation at all, which is exactly right — adding `#[NoAdminRequired]`
out of habit would let any logged-in user switch off the telephone.

The HTTP client is given `allow_local_address`, which Nextcloud
otherwise refuses as SSRF protection. It is safe here because the
address is never user input: it comes from an app config value only an
admin can write.

## Configuration

One setting, and only if the default is wrong:

```
occ config:app:set talk_sip_bridge bridge_url --value='http://<address>:<port>'
```

Default `http://127.0.0.1:8765`. A containerised Nextcloud does not reach
the host at `127.0.0.1` — use the Docker bridge gateway address, and make
the daemon's `BRIDGE_CONTROL_BIND` match it.

Everything else about the bridge is configured in its own environment
file, not here: [`../docs/CONFIG.md`](../docs/CONFIG.md).

## What the page shows

| | |
|---|---|
| *Active (registered as `<user>`)* | the gateway holds a registration |
| *Inactive (not registered)* | it does not — either switched off, or it never came up |
| *Error: …* | the daemon did not answer. Its message is the exception text; the Nextcloud log has it under `talk_sip_bridge` |

The page cannot tell "switched off" from "tried and failed" — both read
*Inactive*. `last_error` says which, and `GET /status` carries it; the
panel does not show it yet. Until it does, `journalctl -u talk-sip-bridge`
is the answer, and the full response shape is documented in
[`../docs/ADMIN.md`](../docs/ADMIN.md#the-control-api-in-full).

## Installing it

Copy `talk_sip_bridge/` into `custom_apps/`, owned by the web server
user, then `occ app:enable talk_sip_bridge`. The surrounding steps — the
`spreed` settings, the daemon, the unit — are in
[`../docs/ADMIN.md`](../docs/ADMIN.md).

Put it in `custom_apps/` and nowhere else. The official Nextcloud images
run `rsync --delete` over `/var/www/html` on every version change, and
`apps/` is not exempt from it.

## Licence

**AGPL-3.0-or-later**, unlike the rest of this repository, which is GPL.
A Nextcloud app builds on `OCP` and Nextcloud is AGPL, so there is no
choice to make here. The daemon and this app only ever talk over HTTP,
so nothing else in the project is affected.
