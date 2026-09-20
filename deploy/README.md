# Deployment

Plain configuration artifacts (file contents and target paths), consumed by
systemd - no ad-hoc scripts.

## Bridge daemon

1. Create a dedicated system user: `useradd --system --no-create-home fritzbox-talk-bridge`.
2. Create a Python venv at `/opt/fritzbox-talk-bridge-venv` (`bridge/requirements.txt`)
   and copy `bridge/*.py` to `/opt/fritzbox-talk-bridge/`, owned by that user.
   `bridge/` holds the daemon and nothing else - the tests are installed
   separately or not at all, see [`../tests/README.md`](../tests/README.md).
3. Copy [`env.example`](./env.example) to `/etc/fritzbox-talk-bridge/env`, fill
   in the required values (see `../docs/CONFIG.md`), `chmod 600`, owned by
   the service user.
4. Copy [`fritzbox-talk-bridge.service`](./fritzbox-talk-bridge.service) to
   `/etc/systemd/system/`, then `systemctl daemon-reload && systemctl enable
   --now fritzbox-talk-bridge`.

Registration is off by default - toggle it on via the Nextcloud app's admin
settings page, or `curl -X POST http://<BRIDGE_CONTROL_BIND>:<BRIDGE_CONTROL_PORT>/toggle`.

## Nextcloud app

Copy [`../nextcloud-app/fritzboxbridge`](../nextcloud-app/fritzboxbridge) into
`custom_apps/`, owned by the web server user, then `occ app:enable
fritzboxbridge`. If the daemon's control API isn't reachable at the default
`http://127.0.0.1:8765` from the Nextcloud container/host (e.g. it's bound
to a Docker bridge gateway address instead), set the correct URL:
`occ config:app:set fritzboxbridge bridge_url --value='http://<address>:<port>'`.

## Talk configuration

For Talk's native "call a phone number" UI and virtual phone participants to
work at all, three `spreed` app config values must be set (see
`../docs/SIGNALING-API.md` "Prerequisites in Nextcloud" for what each one
does): `sip_bridge_shared_secret`, `sip_bridge_dialin_info`, `sip_dialout`.
Optionally restrict to specific groups via `sip_bridge_groups` while
evaluating.
