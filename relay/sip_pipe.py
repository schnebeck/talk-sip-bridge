#!/usr/bin/env python3
# talk-sip-bridge - relay/sip_pipe.py
# Forwards SIP over TCP between two networks, without understanding it.
#
#   Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
#   Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
#   Written by Anthropic Claude Opus 5 - AI generated content.
#
#   Free software under the GNU General Public License, version 3 or later.
#   There is no warranty, to the extent permitted by law. The full text is
#   in LICENSES/GPL-3.0-or-later.txt.
#
# SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
# SPDX-FileContributor: Anthropic Claude Opus 5 (AI generated content)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Forwards SIP over TCP between two networks, without understanding it.

A stream keeps the message boundaries a datagram-to-stream conversion has
to reconstruct, so when both ends speak TCP nothing here needs to parse
SIP, rewrite Via, or track a transaction: bytes in one side come out the
other. That is the whole difference to a SIP proxy, and the reason this
is eighty lines instead of a server.

One route per direction, because the two are not symmetric:

  - outbound: the bridge connects here, this connects to the gateway
  - inbound:  the gateway connects here - to the address in the bridge's
              Contact header - and this connects to the bridge

A gateway delivers a call by opening a connection to that Contact rather
than answering on the registration's connection, so the inbound route is
not optional. One route pair per line, which is also what keeps several
lines apart: each has its own ports rather than sharing one static rule.
"""
import os
import socket
import sys
import threading

BUFFER = 65536


def parse_routes(raw: str) -> list:
    """"listen_ip:port->target_ip:port,..." into (listen, target) pairs."""
    routes = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        listen, _, target = entry.partition("->")
        listen_ip, _, listen_port = listen.strip().rpartition(":")
        target_ip, _, target_port = target.strip().rpartition(":")
        if not (listen_ip and listen_port and target_ip and target_port):
            raise SystemExit(f"Malformed route {entry!r}, expected listen_ip:port->target_ip:port")
        routes.append(((listen_ip, int(listen_port)), (target_ip, int(target_port))))
    return routes


def pump(source, sink, label):
    """One direction of one connection, until either end closes."""
    try:
        while True:
            data = source.recv(BUFFER)
            if not data:
                break
            sink.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (source, sink):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


def serve(listen, target, allowed):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(listen)
    listener.listen(16)
    label = f"{listen[0]}:{listen[1]} -> {target[0]}:{target[1]}"
    print(f"[sip-pipe] {label}" + (f"  (from {', '.join(allowed)})" if allowed else "  (from anyone)"), flush=True)

    while True:
        client, peer = listener.accept()
        if allowed and peer[0] not in allowed:
            print(f"[sip-pipe] {label}: refusing {peer[0]}", flush=True)
            client.close()
            continue
        try:
            upstream = socket.create_connection(target, timeout=10)
            upstream.settimeout(None)
        except OSError as e:
            print(f"[sip-pipe] {label}: cannot reach target ({e!r}), dropping {peer[0]}", flush=True)
            client.close()
            continue
        print(f"[sip-pipe] {label}: {peer[0]}:{peer[1]} connected", flush=True)
        threading.Thread(target=pump, args=(client, upstream, label), daemon=True).start()
        threading.Thread(target=pump, args=(upstream, client, label), daemon=True).start()


def main():
    raw = os.environ.get("SIP_PIPE_ROUTES", "").strip()
    if not raw:
        raise SystemExit("SIP_PIPE_ROUTES is required, e.g. 10.1.1.5:5070->192.168.1.1:5060")
    allowed = [a.strip() for a in os.environ.get("SIP_PIPE_PEERS", "").split(",") if a.strip()]

    threads = []
    for listen, target in parse_routes(raw):
        thread = threading.Thread(target=serve, args=(listen, target, allowed), daemon=True)
        thread.start()
        threads.append(thread)
    if not threads:
        raise SystemExit("no routes configured")
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    sys.exit(main())
