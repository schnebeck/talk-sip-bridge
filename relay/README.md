# Relay host

Needed only when the phone gateway cannot reach the bridge directly — for
example a FritzBox behind CGNAT, reachable from the bridge host only across a
VPN overlay. The relay host has a foot in both networks: the gateway's LAN and
the network the bridge runs in. If the gateway is directly reachable, none of
this is needed and the bridge registers against it without a second host.

Two independent components run here, one per plane:

| Plane | Component | Why |
|---|---|---|
| Signaling | `sip_pipe.py`, or Kamailio | Bridges the two networks; Kamailio additionally converts UDP to TCP |
| Media | `rtp_relay.py` + `rtp-relay.service` | Forwards RTP; the gateway only ever addresses hosts on its own LAN |

Which signaling component depends on the bridge. A line with
`BRIDGE_SIP_TRANSPORT=tcp` speaks the gateway's own transport, and then
nothing has to be converted: `sip_pipe.py` forwards bytes and is done. A
line on UDP needs Kamailio, which terminates the UDP leg and re-originates
on TCP.

On the bridge side this is pure configuration: `BRIDGE_PROXY_HOST`/`_PORT` point
at Kamailio's overlay socket, `BRIDGE_CONTACT_HOST`/`_PORT` at its LAN socket
(the address the gateway sends calls to), and `BRIDGE_RELAY_*` name the media
pipe. See `../docs/CONFIG.md`.

## Gateway behavior this accounts for

Observed on the FritzBox this is deployed against, and both failure modes are
**silent** — no response, no SIP error code:

- **The internal SIP registrar accepts TCP only.** UDP requests are dropped.
- **Registration requires the SIP username** (e.g. `sip-phone`) in the
  To/From/Request-URI, not the internal extension number (e.g. `621`).

Whether that conversion is needed depends on the bridge: with
`BRIDGE_SIP_TRANSPORT=tcp` it speaks TCP itself, and nothing here has to
translate. Kamailio would then be replaceable by a plain TCP pipe - one
that forwards bytes without knowing SIP, because a stream keeps the
message boundaries a datagram-to-stream conversion has to reconstruct.

What does not go away either way: the gateway delivers a call by opening a
connection to the address in the Contact header, so something has to be
listening in its LAN. Measured against this deployment's FritzBox - it does
not answer on the connection the registration arrived on.

## sip_pipe.py: SIP across the networks, nothing else

Two routes, because the directions are not symmetric: the bridge connects
outwards, the gateway connects inwards to whatever the Contact header
names. One pair of ports per line, which is also what keeps lines apart -
each has its own, rather than sharing one rule that can only point at one
of them.

```
SIP_PIPE_ROUTES=10.1.1.5:5070->192.168.1.1:5060,192.168.1.10:5070->10.1.1.1:5091
SIP_PIPE_PEERS=10.1.1.1,192.168.1.1
```

Config: [`sip-pipe.env.example`](./sip-pipe.env.example) →
`/etc/sip-pipe/env`, [`sip_pipe.py`](./sip_pipe.py) → `/opt/sip-pipe/`,
[`sip-pipe.service`](./sip-pipe.service) → `/etc/systemd/system/`. Like the
RTP relay it runs under `DynamicUser` with no capabilities.

It forwards and nothing more - no Via rewriting, no transaction state. That
works because the gateway answers on the connection a request arrived on
rather than at the address in `Via`; measured against this deployment's
FritzBox, over the pipe, with a registration and a call.

## Kamailio: SIP across the networks, UDP to TCP

Stock Debian `kamailio.service`, no automation of its own. Config:
[`kamailio.cfg.example`](./kamailio.cfg.example) → `/etc/kamailio/kamailio.cfg`
(addresses in it are this deployment's; adjust for another).

It is a stateful proxy, not a packet forwarder: it terminates the UDP leg and
re-originates on TCP, stacking its own `Via` so responses find their way back.
Requests from the gateway go to the bridge, everything else to the gateway.

Three details that are load-bearing:

- **The LAN-side `listen=tcp:<lan ip>:5070` socket is required.** Without it
  Kamailio has no matching socket for the outbound TCP connection and the
  request is lost silently.
- **The `pv` module must be loaded.** `$du`/`$dst_uri` are core pseudo-variables
  but ship as a separate module in Kamailio 6.x, and are unusable without it.
- **The return route is static** (`$du = "sip:<bridge>:<port>"`), so exactly one
  bridge line can receive inbound calls through this relay. Serving several
  lines needs routing by request URI or by target port instead.

## RTP relay: media across the networks

Packets are forwarded verbatim. Each side's endpoint is learned from the first
packet seen from that side, so no SDP is parsed and no call state is tracked.

### Pipes, and why their number is the limit on concurrent calls

A *pipe* is a pair of ports, one on each side, and holds exactly one endpoint
per side. Two calls sharing a pipe would overwrite each other's endpoint, and
each side would receive the other call's audio. One pipe therefore carries one
call: configure as many as should be possible at once, and give each SIP line
its own.

A pipe forgets its endpoints after `RTP_RELAY_IDLE_SECONDS` without traffic,
which is what frees it for the next call.

Which pipe a line uses is decided on the bridge side: a line's
`RELAY_LAN_HOST`/`RELAY_LAN_PORT` is the LAN end of its pipe (this is what goes
into the SDP the gateway sees), and `RELAY_OVERLAY_HOST`/`RELAY_OVERLAY_PORT`
is the other end (where that line's RTP is sent).

### Configuration

Environment file, see `env.example`:

| Variable | Meaning |
|---|---|
| `RTP_RELAY_LAN_IP` | Address in the gateway's network to listen on. |
| `RTP_RELAY_OVERLAY_IP` | Address in the bridge's network to listen on. |
| `RTP_RELAY_PIPES` | `lanPort:overlayPort` pairs, comma separated - one per concurrent call. |
| `RTP_RELAY_LAN_PEERS` | Addresses allowed to send on the LAN side, comma separated. Empty means any host that can reach the port may redirect a call's media to itself by sending one packet. |
| `RTP_RELAY_OVERLAY_PEERS` | The same for the overlay side. |
| `RTP_RELAY_IDLE_SECONDS` | How long a pipe keeps learned endpoints before it is free again. |

## Routing prerequisites

IP reachability between the bridge host and the gateway is not part of either
component:

- A host route to the gateway via this relay on the bridge host, persisted with
  the VPN configuration (an OpenVPN `route` directive, for instance).
- On an OpenVPN server, the relay client's `client-config-dir` entry needs a
  matching `iroute` — without it OpenVPN does not know the subnet is reachable
  through that client even when the kernel route is correct.

## Install

```
install -d /opt/rtp-relay /etc/rtp-relay
install -m 0644 rtp_relay.py /opt/rtp-relay/
install -m 0600 env.example /etc/rtp-relay/env      # then edit
install -m 0644 rtp-relay.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now rtp-relay

apt install kamailio
install -m 0644 kamailio.cfg.example /etc/kamailio/kamailio.cfg   # then edit
systemctl enable --now kamailio
```

The RTP relay's unit runs under `DynamicUser` with no capabilities and a
read-only system; it only needs UDP sockets. Without it running, calls still
ring and are answered - they are simply silent in both directions, with nothing
in the bridge's own log to say why, which is why it belongs in a service unit
rather than being started by hand.
