# Configuration

All configuration is via environment variables (`bridge/config.py`), loaded
by `deploy/bridge.env` in a real deployment (see `deploy/bridge.service`).
No IPs, ports, or secrets are hardcoded in code.

## Single line vs. multiple lines

By default the bridge runs one line, configured with the flat `BRIDGE_*`
variables below (e.g. `BRIDGE_SIP_USER`). To run several lines side by side
- independent SIP accounts, even against entirely different
registrars/gateways (a FritzBox and an Asterisk box at once, say) - set:

```
BRIDGE_LINES=<id1>,<id2>,...
```

with `<id>` any identifier made of letters, digits and underscore. Every
per-line variable in the tables below (everything except
`BRIDGE_REGISTER_EXPIRES`, `BRIDGE_SIP_RESPONSE_TIMEOUT`,
`BRIDGE_OUTBOUND_CALL_TIMEOUT`, `BRIDGE_MAX_CALL_DURATION`,
`BRIDGE_AUTO_ANSWER`, `BRIDGE_AGC_*`, `BRIDGE_CONTROL_BIND`/`_PORT`,
`BRIDGE_WS_URL`, `BRIDGE_INTERNAL_SECRET`, `BRIDGE_BACKEND_URL`,
`BRIDGE_LOCAL_IP`, which stay global) is then
set per line as
`BRIDGE_LINE_<id>_<name>`, e.g. `BRIDGE_LINE_<id>_SIP_USER`,
`BRIDGE_LINE_<id>_LOCAL_SIP_PORT`. `LOCAL_SIP_PORT` and `LOCAL_RTP_PORT`
have no default in this form and must be set explicitly and distinctly per
line (unlike the flat single-line form, where they default to 5060/40000).

Each line gets its own registration, its own `CallManager` (one call at a
time, per line), and its own dedicated connection to the Talk signaling
server - see `docs/CONCEPT.md` point 3 for why a shared connection across
lines wouldn't work (joining a room to publish one line's call audio makes
that connection ineligible for new dial-out requests on any other line for
as long as the connection stays open).

Dial-out routing across multiple lines is not yet deterministic by number:
a Talk-initiated "call a phone number" request is placed with whichever
line's connection the signaling server currently considers available, and
only that line's own `DIALOUT_NUMBER_ALLOWLIST` decides whether it's
accepted. This is not a concern for inbound calls, which always land on
whichever line's own registered number was actually dialed.

## Ring notification (a human "wins" an inbound call in Talk)

For a line whose gateway already rings several physical devices in parallel
for the same number (e.g. a FritzBox call-distribution group), setting
`NOTIFY_USER` and `NOTIFY_APP_PASSWORD` makes the bridge one more competing
device: an inbound call on that line (while `BRIDGE_AUTO_ANSWER` is off,
the default) signs into that Nextcloud account and uses Talk's own OCS call
API to join the call in the configured room - the same mechanism a real
Talk client uses to start a call, so it triggers real ringing (push,
full-screen call UI) on every other device logged into that account or
already in the room, not just a chat message. The bridge answers the SIP
side automatically the moment a different, real session joins that call -
racing whichever device (physical phone or Talk) answers first. If a
physical device wins, the gateway cancels the bridge's SIP leg as usual and
the bridge leaves the Talk call it triggered. See `docs/CONCEPT.md` point
12.

`NOTIFY_APP_PASSWORD` is an app password for `NOTIFY_USER`'s account
(Nextcloud Settings -> Security -> "Create new app password"), not that
account's real login password. That account must already be a member of
the line's `DEFAULT_ROOM`.

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
| `BRIDGE_SIP_TRANSPORT` | `udp` | `udp` or `tcp`: which transport this line's SIP runs over. Registrars differ, and a wrong choice is silent - a FritzBox drops UDP without a word, and TCP-only providers are common. With `tcp` the bridge keeps one outgoing connection for its own requests and listens for the ones the gateway opens to its Contact, because a registrar delivers a call by connecting to that address rather than answering on the registration's connection. |
| `BRIDGE_CONTACT_TRANSPORT` | follows `BRIDGE_SIP_TRANSPORT` | Which transport the gateway is told to use towards the Contact. Only differs when a relay changes transport on the way: with the bridge speaking UDP to a relay that speaks TCP onwards, this is `tcp` while `BRIDGE_SIP_TRANSPORT` stays `udp`. Getting it wrong costs inbound calls only - registration still succeeds. |
| `BRIDGE_LOCAL_SIP_PORT` | 5060 | Local port for SIP signaling - bound as a UDP socket, or as a listener the gateway connects to, depending on `BRIDGE_SIP_TRANSPORT`. |
| `BRIDGE_CONTACT_HOST` / `BRIDGE_CONTACT_PORT` | `BRIDGE_LOCAL_IP` / `BRIDGE_LOCAL_SIP_PORT` | Address advertised in the SIP Contact header - where the gateway sends calls for this registration. Only needs to differ from the defaults when a SIP proxy/relay sits between this host and the gateway; it is then that relay's address, not this host's or the media relay's. |
| `BRIDGE_LOCAL_RTP_PORT` | 40000 | Local UDP port for RTP media. |
| `BRIDGE_REGISTER_EXPIRES` | 600 | SIP registration lifetime in seconds. |
| `BRIDGE_SIP_RESPONSE_TIMEOUT` | 6 | How long to wait for a SIP response before giving up. |
| `BRIDGE_OUTBOUND_CALL_TIMEOUT` | 30 | How long an outbound call may ring before giving up. |
| `BRIDGE_MAX_CALL_DURATION` | 14400 (4h) | Safety net: a connected call is hung up after this many seconds even without a BYE, so a stuck call (e.g. the gateway silently drops it) can't block every other call indefinitely - only one is ever handled at a time. |
| `BRIDGE_DEFAULT_ROOM` | (empty) | Talk room token that inbound/outbound calls are bridged into. |
| `BRIDGE_CONTROL_BIND` / `BRIDGE_CONTROL_PORT` | `127.0.0.1` / `8765` | Local HTTP control API (`GET /status`, `POST /toggle`) for the Nextcloud app. Not authenticated - bind only to an address reachable from the Nextcloud container/host (e.g. the Docker bridge gateway), never a public interface. |
| `BRIDGE_STATE_DIR` | `/var/lib/talk-sip-bridge` | Directory for small per-line state files (currently just whether a line's registration should be on), so a crash-triggered restart resumes automatically instead of silently leaving the line deregistered until someone notices. `deploy/talk-sip-bridge.service` provisions this path via systemd's `StateDirectory=`. Empty disables persistence (registration always starts off, as before this existed). |
| `BRIDGE_AUTO_ANSWER` | `false` | Whether an incoming call is answered automatically. When `false` (the default), incoming calls just keep ringing - there is no native ringing/accept-decline exchange in the signaling protocol to gate this on, so answering must be opted into explicitly. |
| `BRIDGE_DIALOUT_NUMBER_ALLOWLIST` | (empty) | Optional regex a dialout number must fully match, in addition to the fixed character allowlist in `sip_messages.py` (always enforced, rejects anything that isn't digits/`+*#.-`). Applied after `BRIDGE_DIALOUT_STRIP_PREFIX`. Empty means no additional restriction. |
| `BRIDGE_DIALOUT_STRIP_PREFIX` | (empty) | Prefix stripped from the start of a dialout number before dialing - the gateway's dial plan expects short internal extensions bare, without it. Nextcloud validates any number entered in its call-a-number UI as a real phone number (`libphonenumber`, not configurable) before it ever reaches this bridge, so a bare 3-digit extension is rejected by Nextcloud itself; a real, syntactically valid area code prefix like `+4930` (Berlin) followed by any 3-digit extension passes that validation while never being an assigned, reachable number - safe to use here since this bridge intercepts and redirects it to the internal extension before anything would reach a real trunk line. |
| `BRIDGE_PHONE_PARTICIPANT` | `phone` | How the caller appears in the room. `phone`: a virtual session flagged as a phone, showing the number - but Talk's clients build no peer for a participant without audio, and their "waiting for someone" sound then repeats every 15s for as long as the call lasts (measured; the web client stops after three, Android does not). `audio`: the same session flagged as carrying audio, which makes clients look for a stream a virtual session cannot have. `none`: no virtual session at all - the bridge's own publishing session carries the call under the caller's name, and there is no phone tile. |
| `BRIDGE_INBAND_DTMF` | `true` | Also read key presses out of the audio. Needed on a gateway that agrees to RFC 4733 events and then plays the tones instead, which this deployment's does; harmless elsewhere, since a press reported twice is collapsed. |
| `BRIDGE_DIALOUT_INTERNAL_DIAL_PREFIX` | (empty) | Prepended to a number that matched `BRIDGE_DIALOUT_NUMBER_ALLOWLIST` (i.e. was recognized as an internal extension) before dialing - the gateway's own notation for reaching a physical device by extension, e.g. `**` on a FritzBox. A bare extension may be accepted at the SIP signaling level without this but never actually alert the device. |
| `BRIDGE_AGC_ENABLED` | `true` | Automatic gain control on audio coming from the phone side before it's published into Talk (see `agc.py`). Some handsets (e.g. a DECT cordless) have a much quieter microphone than a laptop/headset, with no way to adjust that from this end of the call, and how quiet it sounds also varies with distance to the handset - AGC adapts continuously rather than needing one fixed multiplier. |
| `BRIDGE_AGC_TARGET_PEAK` | 10000 | Peak amplitude (out of a max of 32767) the AGC aims for. |
| `BRIDGE_AGC_MAX_GAIN` | 8.0 | Upper bound on how far the AGC will amplify a quiet signal. Measured on a DECT handset, speech peaks at 2000-4000 need a gain of 2.5-5 to reach the target, so this leaves headroom without letting quiet passages run away. |
| `BRIDGE_AGC_SILENCE_THRESHOLD` | 500 | Peak below which the AGC stops adapting. It has to clear the line's own noise floor - measured at 150-250 on the same handset - or the gain rides the noise up between words and drops again on the next syllable, heard as a bubbling background. |
| `BRIDGE_NOTIFY_USER` | (empty) | Per-line. Nextcloud account the bridge signs into to ring a call on this line's behalf, via Talk's own OCS call API - see "Ring notification" above. Empty leaves the line ringing with no Talk-side signal, as before this existed. |
| `BRIDGE_NOTIFY_APP_PASSWORD` | (empty) | Per-line. App password for `BRIDGE_NOTIFY_USER`'s account. |

## Media relay (only if the gateway can't reach this host directly)

Leave unset if the phone gateway can deliver SIP and RTP directly to
`BRIDGE_LOCAL_IP`. Set all four if a relay is needed - e.g. a FritzBox
reachable only through a VPN, with a relay host on its LAN forwarding
SIP/RTP to this host over the tunnel:

| Variable | Meaning |
|---|---|
| `BRIDGE_RELAY_LAN_HOST` / `BRIDGE_RELAY_LAN_PORT` | The relay's address reachable from the gateway's own LAN - advertised in our SIP Contact header and SDP. |
| `BRIDGE_RELAY_OVERLAY_HOST` / `BRIDGE_RELAY_OVERLAY_PORT` | The relay's address reachable from this host - where our own SIP/RTP sockets actually send to. |
