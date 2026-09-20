#!/usr/bin/env python3
"""Measures the media relay on its own: what goes in on one side, and
how it comes out on the other.

Every other audio test here ends at the bridge's own socket, so the one
hop between the bridge and the phone that this deployment adds - the
relay that carries RTP across the VPN - was never in the measurement. A
periodic click that the bridge's output does not have has to come from
somewhere, and this is the only piece of the path we own.

Two ends, started separately, because they sit on different hosts:

    # on the relay host, LAN side
    relay_probe.py receive --ip 192.168.1.10 --port 40020 \
        --relay 192.168.1.10:40010 --seconds 25 --report /tmp/relay.json

    # on the bridge host, overlay side
    relay_probe.py send --to 10.1.1.5:40011 --seconds 20

The receiver primes the relay first: a pipe forwards to whoever last
sent from the other side, so nothing arrives until this end has been
heard from once.

Payload is PCMU at 50 packets a second, the same shape as a call, with
a continuous sine so that a gap is visible in the samples as well as in
the arrival times.
"""
import argparse
import json
import math
import socket
import struct
import sys
import time

SAMPLE_RATE = 8000
SAMPLES_PER_PACKET = 160
PACKET_SECONDS = SAMPLES_PER_PACKET / SAMPLE_RATE
TONE_HZ = 660.0
PAYLOAD_TYPE = 0


def ulaw(samples):
    """Plain mu-law, so the probe needs nothing from the bridge."""
    out = bytearray()
    for value in samples:
        sign = 0x80 if value < 0 else 0
        magnitude = min(abs(int(value)), 32635) + 132
        exponent = max(magnitude.bit_length() - 8, 0)
        mantissa = (magnitude >> (exponent + 3)) & 0x0F
        out.append(~(sign | (exponent << 4) | mantissa) & 0xFF)
    return bytes(out)


def send(target: tuple, seconds: float, source_port: int):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", source_port))
    ssrc = 0x51A7E5
    sent = 0
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        phase = [2 * math.pi * TONE_HZ * (sent * SAMPLES_PER_PACKET + i) / SAMPLE_RATE
                 for i in range(SAMPLES_PER_PACKET)]
        payload = ulaw([math.sin(p) * 8000 for p in phase])
        header = struct.pack("!BBHII", 0x80, PAYLOAD_TYPE, sent & 0xFFFF,
                             (sent * SAMPLES_PER_PACKET) & 0xFFFFFFFF, ssrc)
        sock.sendto(header + payload, target)
        sent += 1
        # Paced against a fixed schedule, not by sleeping a packet's
        # worth after each send: the send itself takes time, and that
        # error accumulates into a stream that is slower than real time.
        due = started + sent * PACKET_SECONDS
        remaining = due - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
    sock.close()
    print(json.dumps({"sent": sent, "seconds": round(time.monotonic() - started, 2)}))


def receive(ip: str, port: int, relay: tuple, seconds: float, report: str):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((ip, port))
    sock.settimeout(0.5)
    # The relay forwards to whoever last sent from this side, so make
    # itself known before waiting for anything.
    for _ in range(3):
        sock.sendto(b"\x80\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00", relay)
        time.sleep(0.05)

    arrivals, sequences = [], []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            data, _ = sock.recvfrom(2048)
        except socket.timeout:
            continue
        if len(data) < 12:
            continue
        arrivals.append(time.monotonic())
        sequences.append(struct.unpack("!H", data[2:4])[0])
    sock.close()

    gaps = [b - a for a, b in zip(arrivals, arrivals[1:])]
    lost = 0
    for previous, current in zip(sequences, sequences[1:]):
        step = (current - previous) & 0xFFFF
        if step > 1:
            lost += step - 1
    result = {
        "packets": len(arrivals),
        "lost": lost,
        "seconds": round(arrivals[-1] - arrivals[0], 2) if len(arrivals) > 1 else 0,
        "median_ms": round(sorted(gaps)[len(gaps) // 2] * 1000, 2) if gaps else None,
        "worst_ms": round(max(gaps) * 1000, 1) if gaps else None,
        "over_30ms": sum(1 for g in gaps if g > 1.5 * PACKET_SECONDS),
        "over_60ms": sum(1 for g in gaps if g > 3 * PACKET_SECONDS),
    }
    late = [arrivals[i + 1] for i, g in enumerate(gaps) if g > 1.5 * PACKET_SECONDS]
    if len(late) > 1:
        spacing = [b - a for a, b in zip(late, late[1:])]
        result["long_gaps_every_s"] = round(sum(spacing) / len(spacing), 2)
    if report:
        with open(report, "w") as f:
            json.dump(result, f)
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["send", "receive"])
    parser.add_argument("--to", help="host:port to send to (the relay's overlay side)")
    parser.add_argument("--ip", default="0.0.0.0", help="address to receive on")
    parser.add_argument("--port", type=int, default=40020)
    parser.add_argument("--relay", help="host:port of the relay's own side, to prime it")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--report", default="")
    args = parser.parse_args()

    if args.mode == "send":
        host, port = args.to.split(":")
        send((host, int(port)), args.seconds, args.port)
    else:
        host, port = args.relay.split(":")
        receive(args.ip, args.port, (host, int(port)), args.seconds, args.report)


if __name__ == "__main__":
    sys.exit(main())
