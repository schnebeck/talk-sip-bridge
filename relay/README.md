# RTP relay

Runs on a host with a foot in two networks - the phone gateway's LAN and
the network the bridge runs in - and forwards media packets between them.

The gateway only ever addresses hosts on its own LAN. When the bridge lives
elsewhere (in this deployment: across an OpenVPN overlay, where the gateway
is reachable only through this same host), the gateway cannot send RTP to
the bridge directly. SIP signaling has the same problem and is solved by a
SIP proxy on this host; this relay is the equivalent for the media.

Packets are forwarded verbatim. Each side's endpoint is learned from the
first packet seen from that side, so no SDP is parsed and no call state is
tracked.

## Pipes, and why their number is the limit on concurrent calls

A *pipe* is a pair of ports, one on each side, and holds exactly one
endpoint per side. Two calls sharing a pipe would overwrite each other's
endpoint, and each side would receive the other call's audio. One pipe
therefore carries one call: configure as many as should be possible at
once, and give each SIP line its own.

A pipe forgets its endpoints after `RTP_RELAY_IDLE_SECONDS` without
traffic, which is what frees it for the next call.

Which pipe a line uses is decided on the bridge side: a line's
`RELAY_LAN_HOST`/`RELAY_LAN_PORT` is the LAN end of its pipe (this is what
goes into the SDP the gateway sees), and `RELAY_OVERLAY_HOST`/
`RELAY_OVERLAY_PORT` is the other end (where that line's RTP is sent).

## Configuration

Environment file, see `env.example`:

| Variable | Meaning |
|---|---|
| `RTP_RELAY_LAN_IP` | Address in the gateway's network to listen on. |
| `RTP_RELAY_OVERLAY_IP` | Address in the bridge's network to listen on. |
| `RTP_RELAY_PIPES` | `lanPort:overlayPort` pairs, comma separated - one per concurrent call. |
| `RTP_RELAY_LAN_PEERS` | Addresses allowed to send on the LAN side, comma separated. Empty means any host that can reach the port may redirect a call's media to itself by sending one packet. |
| `RTP_RELAY_OVERLAY_PEERS` | The same for the overlay side. |
| `RTP_RELAY_IDLE_SECONDS` | How long a pipe keeps learned endpoints before it is free again. |

## Install

```
install -d /opt/rtp-relay /etc/rtp-relay
install -m 0644 rtp_relay.py /opt/rtp-relay/
install -m 0600 env.example /etc/rtp-relay/env      # then edit
install -m 0644 rtp-relay.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now rtp-relay
```

The unit runs under `DynamicUser` with no capabilities and a read-only
system; it only needs UDP sockets. Without it running, calls still ring and
are answered - they are simply silent in both directions, with nothing in
the bridge's own log to say why, which is why it belongs in a service unit
rather than being started by hand.
