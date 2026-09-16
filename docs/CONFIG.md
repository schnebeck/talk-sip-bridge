# Configuration

All configuration is via environment variables (`bridge/config.py`), loaded
by `deploy/bridge.env` in a real deployment (see `deploy/bridge.service`).
No IPs, ports, or secrets are hardcoded in code.

## Required

| Variable | Meaning |
|---|---|
| `BRIDGE_SIP_USER` | SIP account username on the phone gateway. |
| `BRIDGE_SIP_PASS` | SIP account password. |
| `BRIDGE_GATEWAY_HOST` | The phone gateway's SIP registrar address (e.g. the FritzBox). |
| `BRIDGE_LOCAL_IP` | This host's address, used in SIP Via/Contact and RTP binding. |
| `BRIDGE_WS_URL` | The Talk standalone signaling server's WebSocket URL. |
| `BRIDGE_INTERNAL_SECRET` | The signaling server's `internalsecret` (from its `server.conf`). |
| `BRIDGE_BACKEND_URL` | The Nextcloud instance URL (sent as the internal-client `backend` hello param). |

## Optional

| Variable | Default | Meaning |
|---|---|---|
| `BRIDGE_PROXY_HOST` / `BRIDGE_PROXY_PORT` | gateway host / 5060 | Where SIP requests are actually sent - a relay if one is needed (see "Media relay" below), otherwise the gateway itself. |
| `BRIDGE_LOCAL_SIP_PORT` | 5060 | Local UDP port for SIP signaling. |
| `BRIDGE_CONTACT_HOST` / `BRIDGE_CONTACT_PORT` | `BRIDGE_LOCAL_IP` / `BRIDGE_LOCAL_SIP_PORT` | Address advertised in the SIP Contact header - where the gateway sends calls for this registration. Only needs to differ from the defaults when a SIP proxy/relay sits between this host and the gateway; it is then that relay's address, not this host's or the media relay's. |
| `BRIDGE_LOCAL_RTP_PORT` | 40000 | Local UDP port for RTP media. |
| `BRIDGE_REGISTER_EXPIRES` | 600 | SIP registration lifetime in seconds. |
| `BRIDGE_SIP_RESPONSE_TIMEOUT` | 6 | How long to wait for a SIP response before giving up. |
| `BRIDGE_OUTBOUND_CALL_TIMEOUT` | 30 | How long an outbound call may ring before giving up. |
| `BRIDGE_MAX_CALL_DURATION` | 14400 (4h) | Safety net: a connected call is hung up after this many seconds even without a BYE, so a stuck call (e.g. the gateway silently drops it) can't block every other call indefinitely - only one is ever handled at a time. |
| `BRIDGE_DEFAULT_ROOM` | (empty) | Talk room token that inbound/outbound calls are bridged into. |
| `BRIDGE_CONTROL_BIND` / `BRIDGE_CONTROL_PORT` | `127.0.0.1` / `8765` | Local HTTP control API (`GET /status`, `POST /toggle`) for the Nextcloud app. Not authenticated - bind only to an address reachable from the Nextcloud container/host (e.g. the Docker bridge gateway), never a public interface. |
| `BRIDGE_AUTO_ANSWER` | `false` | Whether an incoming call is answered automatically. When `false` (the default), incoming calls just keep ringing - there is no native ringing/accept-decline exchange in the signaling protocol to gate this on, so answering must be opted into explicitly. |
| `BRIDGE_DIALOUT_NUMBER_ALLOWLIST` | (empty) | Optional regex a dialout number must fully match, in addition to the fixed character allowlist in `sip_core.py` (always enforced, rejects anything that isn't digits/`+*#.-`). Applied after `BRIDGE_DIALOUT_STRIP_PREFIX`. Empty means no additional restriction. |
| `BRIDGE_DIALOUT_STRIP_PREFIX` | (empty) | Prefix stripped from the start of a dialout number before dialing - the gateway's dial plan expects short internal extensions bare, without it. Nextcloud validates any number entered in its call-a-number UI as a real phone number (`libphonenumber`, not configurable) before it ever reaches this bridge, so a bare 3-digit extension is rejected by Nextcloud itself; a real, syntactically valid area code prefix like `+4930` (Berlin) followed by any 3-digit extension passes that validation while never being an assigned, reachable number - safe to use here since this bridge intercepts and redirects it to the internal extension before anything would reach a real trunk line. |
| `BRIDGE_DIALOUT_INTERNAL_DIAL_PREFIX` | (empty) | Prepended to a number that matched `BRIDGE_DIALOUT_NUMBER_ALLOWLIST` (i.e. was recognized as an internal extension) before dialing - the gateway's own notation for reaching a physical device by extension, e.g. `**` on a FritzBox. A bare extension may be accepted at the SIP signaling level without this but never actually alert the device. |

## Media relay (only if the gateway can't reach this host directly)

Leave unset if the phone gateway can deliver SIP and RTP directly to
`BRIDGE_LOCAL_IP`. Set all four if a relay is needed (see the PoC's
`NETZWERK.md` and `rtp_relay.py` for why the FritzBox specifically needs
this):

| Variable | Meaning |
|---|---|
| `BRIDGE_RELAY_LAN_HOST` / `BRIDGE_RELAY_LAN_PORT` | The relay's address reachable from the gateway's own LAN - advertised in our SIP Contact header and SDP. |
| `BRIDGE_RELAY_OVERLAY_HOST` / `BRIDGE_RELAY_OVERLAY_PORT` | The relay's address reachable from this host - where our own SIP/RTP sockets actually send to. |
