#!/usr/bin/env python3
"""Plain-RTP relay between a phone gateway's LAN and the bridge host.

The gateway only knows addresses on its own LAN, so it cannot send media to
the bridge directly when the bridge lives elsewhere (here: across an
OpenVPN overlay). This relay sits on a host with a foot in both networks:
it listens on a LAN address the gateway can reach, and forwards the packets
verbatim to the bridge over the overlay, and back.

It carries one "pipe" per concurrent call - a pair of ports, one on each
side. A pipe learns both endpoints from the first packet it sees from each
side, so no SDP has to be parsed; it forgets them again after
RTP_RELAY_IDLE_SECONDS without traffic, which is what lets the same pipe
serve the next call. One pipe can only ever carry one call: both sides of a
pipe are a single learned address, so two calls sharing a pipe would
overwrite each other's endpoint and send each side the other call's audio.
Configure as many pipes as calls should be possible at once, and give each
line its own.

Configuration (environment, see relay/README.md):

  RTP_RELAY_LAN_IP           address on the gateway's network to listen on
  RTP_RELAY_OVERLAY_IP       address on the bridge's network to listen on
  RTP_RELAY_PIPES            "lanPort:overlayPort,..." - one pair per pipe
  RTP_RELAY_LAN_PEERS        optional: addresses allowed to send on the LAN
                             side, comma separated. Without this, any host
                             that can reach the port redirects the call's
                             media to itself simply by sending a packet.
  RTP_RELAY_OVERLAY_PEERS    the same for the overlay side
  RTP_RELAY_IDLE_SECONDS     how long a pipe keeps its learned endpoints
"""
import os
import selectors
import socket
import sys
import time


def _env_list(name: str) -> set:
    return {entry.strip() for entry in os.environ.get(name, "").split(",") if entry.strip()}


def _parse_pipes(raw: str) -> list:
    pipes = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        lan_port, _, overlay_port = entry.partition(":")
        if not overlay_port.isdigit() or not lan_port.isdigit():
            raise SystemExit(f"RTP_RELAY_PIPES entry {entry!r} is not <lanPort>:<overlayPort>")
        pipes.append((int(lan_port), int(overlay_port)))
    if not pipes:
        raise SystemExit("RTP_RELAY_PIPES is empty - nothing to relay")
    return pipes


class Pipe:
    """One media path: a LAN port the gateway sends to, an overlay port the
    bridge sends to, and the endpoint most recently seen on each side."""

    def __init__(self, lan_ip: str, lan_port: int, overlay_ip: str, overlay_port: int):
        self.name = f"{lan_port}<->{overlay_port}"
        self.socks = {
            "lan": self._bind(lan_ip, lan_port),
            "overlay": self._bind(overlay_ip, overlay_port),
        }
        self.peers = {"lan": None, "overlay": None}
        self.contention = {"lan": 0, "overlay": 0}
        self.last_packet = 0.0

    @staticmethod
    def _bind(ip: str, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((ip, port))
        return sock

    def expire(self, idle_seconds: float):
        if self.last_packet and time.monotonic() - self.last_packet > idle_seconds:
            if any(self.peers.values()):
                contended = ", ".join(f"{side} changed {count}x" for side, count in self.contention.items() if count)
                print(f"[relay] {self.name}: idle, forgetting both endpoints"
                      + (f" ({contended} - more than one call shared this pipe)" if contended else ""),
                      flush=True)
            self.peers = {"lan": None, "overlay": None}
            self.contention = {"lan": 0, "overlay": 0}
            self.last_packet = 0.0

    def forward(self, side: str, allowed: dict):
        other = "overlay" if side == "lan" else "lan"
        try:
            data, addr = self.socks[side].recvfrom(2048)
        except OSError:
            return
        if allowed[side] and addr[0] not in allowed[side]:
            return  # not a party to this call - see RTP_RELAY_*_PEERS
        if self.peers[side] != addr:
            if self.peers[side] is None:
                print(f"[relay] {self.name}: {side} endpoint is {addr[0]}:{addr[1]}", flush=True)
            else:
                # Two calls in one pipe, or a stray sender. Counted rather
                # than logged per packet: under contention this changes with
                # every packet and would be the only thing in the journal.
                self.contention[side] += 1
            self.peers[side] = addr
        self.last_packet = time.monotonic()
        target = self.peers[other]
        if target is not None:
            self.socks[other].sendto(data, target)


def main():
    lan_ip = os.environ.get("RTP_RELAY_LAN_IP")
    overlay_ip = os.environ.get("RTP_RELAY_OVERLAY_IP")
    if not lan_ip or not overlay_ip:
        raise SystemExit("RTP_RELAY_LAN_IP and RTP_RELAY_OVERLAY_IP are required")
    idle_seconds = float(os.environ.get("RTP_RELAY_IDLE_SECONDS", "30"))
    allowed = {"lan": _env_list("RTP_RELAY_LAN_PEERS"),
               "overlay": _env_list("RTP_RELAY_OVERLAY_PEERS")}

    pipes = [Pipe(lan_ip, lan_port, overlay_ip, overlay_port)
             for lan_port, overlay_port in _parse_pipes(os.environ.get("RTP_RELAY_PIPES", ""))]

    selector = selectors.DefaultSelector()
    for pipe in pipes:
        for side, sock in pipe.socks.items():
            selector.register(sock, selectors.EVENT_READ, (pipe, side))

    print(f"[relay] {len(pipes)} pipe(s) on LAN {lan_ip} / overlay {overlay_ip}: "
          f"{', '.join(p.name for p in pipes)}", flush=True)
    for side in ("lan", "overlay"):
        print(f"[relay] {side} senders: {', '.join(sorted(allowed[side])) or 'any (unrestricted)'}",
              flush=True)

    while True:
        for key, _ in selector.select(timeout=1):
            pipe, side = key.data
            pipe.forward(side, allowed)
        for pipe in pipes:
            pipe.expire(idle_seconds)


if __name__ == "__main__":
    sys.exit(main())
