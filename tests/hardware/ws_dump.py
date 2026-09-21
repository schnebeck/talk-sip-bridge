#!/usr/bin/env python3
"""Reads the signaling traffic off a packet capture, as messages.

Both sides of every signaling conversation cross one loopback port on
the Nextcloud host in plain text: the browser's WSS connection is
decrypted by the reverse proxy before it reaches the signaling server,
and this bridge connects to the same port. A capture there is therefore
the only place that shows what one participant sent and what another
one was handed - the two questions a log on either end alone cannot
separate.

    # on the Nextcloud host, while the thing under test happens
    tcpdump -i lo -s 0 -w /tmp/signaling.pcap port 8080

    # here
    ws_dump.py /tmp/signaling.pcap --filter control

Client-to-server frames are masked (RFC 6455 requires it), which is why
`-A` on tcpdump shows nothing readable in that direction; this unmasks
them. Streams are reassembled by sequence number, so a message split
across packets comes out whole.

Deliberately narrow: IPv4 TCP, no retransmission handling beyond
dropping duplicate sequence numbers, no permessage-deflate. That is
what loopback capture of one connection needs, and anything more
elaborate would be a packet analyser rather than a reading aid.
"""
import argparse
import collections
import json
import struct
import sys

# Classic libpcap, both byte orders. tcpdump writes pcapng only when
# asked to; -w gives this.
MAGIC_LE = 0xA1B2C3D4
MAGIC_LE_NANO = 0xA1B23C4D

LINKTYPE_ETHERNET = 1
LINKTYPE_LINUX_SLL = 113
LINKTYPE_NULL = 0


def packets(path):
    """Yields (timestamp, ip_payload) for every captured IPv4 TCP packet."""
    with open(path, "rb") as f:
        header = f.read(24)
        if len(header) < 24:
            raise SystemExit(f"{path}: too short to be a capture")
        magic = struct.unpack("<I", header[:4])[0]
        if magic in (MAGIC_LE, MAGIC_LE_NANO):
            endian, nanos = "<", magic == MAGIC_LE_NANO
        else:
            magic = struct.unpack(">I", header[:4])[0]
            if magic not in (MAGIC_LE, MAGIC_LE_NANO):
                raise SystemExit(f"{path}: not a libpcap file (pcapng? use -w, not -W)")
            endian, nanos = ">", magic == MAGIC_LE_NANO
        linktype = struct.unpack(endian + "I", header[20:24])[0]

        while True:
            record = f.read(16)
            if len(record) < 16:
                return
            seconds, fraction, captured, _original = struct.unpack(endian + "IIII", record)
            data = f.read(captured)
            if len(data) < captured:
                return
            yield seconds + fraction / (1e9 if nanos else 1e6), strip_link(linktype, data)


def strip_link(linktype: int, data: bytes) -> bytes:
    """The IP packet inside one link-layer frame."""
    if linktype == LINKTYPE_ETHERNET:
        return data[14:] if len(data) > 14 and data[12:14] == b"\x08\x00" else b""
    if linktype == LINKTYPE_LINUX_SLL:
        return data[16:] if len(data) > 16 and data[14:16] == b"\x08\x00" else b""
    if linktype == LINKTYPE_NULL:
        # BSD loopback: a 4-byte address family, 2 for IPv4.
        return data[4:] if len(data) > 4 and data[:4] in (b"\x02\x00\x00\x00",
                                                          b"\x00\x00\x00\x02") else b""
    return b""


def segments(path):
    """Yields (timestamp, flow, seq, payload) for every TCP segment that
    carries data. `flow` is "a.b.c.d:p>e.f.g.h:q", the direction included,
    because the two directions are separate streams."""
    for timestamp, ip in packets(path):
        if len(ip) < 20 or ip[0] >> 4 != 4 or ip[9] != 6:   # IPv4, TCP
            continue
        ihl = (ip[0] & 0x0F) * 4
        total = struct.unpack("!H", ip[2:4])[0]
        source = ".".join(str(b) for b in ip[12:16])
        destination = ".".join(str(b) for b in ip[16:20])
        tcp = ip[ihl:total]
        if len(tcp) < 20:
            continue
        sport, dport = struct.unpack("!HH", tcp[:4])
        seq = struct.unpack("!I", tcp[4:8])[0]
        offset = (tcp[12] >> 4) * 4
        payload = tcp[offset:]
        if payload:
            yield (timestamp, f"{source}:{sport}>{destination}:{dport}", seq, payload)


class Stream:
    """One direction of one connection, reassembled and read as frames.

    Kept as a buffer that is consumed a whole frame at a time: a message
    can arrive split across packets, and two can arrive in one.
    """

    def __init__(self):
        self.buffer = bytearray()
        self.seen = set()
        self.handshake_done = False

    def add(self, seq: int, payload: bytes):
        if seq in self.seen:      # a retransmission, or the capture saw it twice
            return
        self.seen.add(seq)
        self.buffer += payload

    def messages(self):
        """Every complete message in the buffer, consuming it."""
        if not self.handshake_done:
            if not self.buffer:
                return
            # A capture started before the connection begins with the HTTP
            # upgrade; one started in the middle is frames from the first
            # byte, and waiting for headers that will never come would
            # swallow the whole stream. The first byte tells them apart:
            # a frame's is FIN plus an opcode, never a printable letter.
            if self.buffer[0] in (ord("G"), ord("H")):
                end = self.buffer.find(b"\r\n\r\n")
                if end < 0:
                    return          # the headers are still arriving
                del self.buffer[:end + 4]
            self.handshake_done = True

        while True:
            frame = self._take_frame()
            if frame is None:
                return
            opcode, data = frame
            if opcode in (0x1, 0x2) and data:
                yield data

    def _take_frame(self):
        buffer = self.buffer
        if len(buffer) < 2:
            return None
        opcode = buffer[0] & 0x0F
        masked = bool(buffer[1] & 0x80)
        length = buffer[1] & 0x7F
        at = 2
        if length == 126:
            if len(buffer) < at + 2:
                return None
            length = struct.unpack("!H", buffer[at:at + 2])[0]
            at += 2
        elif length == 127:
            if len(buffer) < at + 8:
                return None
            length = struct.unpack("!Q", buffer[at:at + 8])[0]
            at += 8
        key = b""
        if masked:
            if len(buffer) < at + 4:
                return None
            key = bytes(buffer[at:at + 4])
            at += 4
        if len(buffer) < at + length:
            return None
        data = bytes(buffer[at:at + length])
        del buffer[:at + length]
        if masked:
            data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
        return opcode, data


def describe(message: dict) -> str:
    """The one line that says what a signaling message is, so a capture
    of a busy room can be skimmed."""
    kind = message.get("type", "?")
    if kind == "internal":
        return f"internal/{message.get('internal', {}).get('type', '?')}"
    if kind == "control":
        data = message.get("control", {}).get("data") or {}
        recipient = (message.get("control", {}).get("recipient") or {}).get("sessionid", "")
        return f"control/{data.get('type', '?')} -> {recipient[:16]}"
    if kind == "message":
        data = message.get("message", {}).get("data") or {}
        recipient = (message.get("message", {}).get("recipient") or {}).get("sessionid", "")
        return f"message/{data.get('type', '?')} -> {recipient[:16]}"
    if kind == "event":
        event = message.get("event", {})
        return f"event/{event.get('target', '?')}/{event.get('type', '?')}"
    if kind == "error":
        return f"error/{message.get('error', {}).get('code', '?')}"
    return kind


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("capture", help="the .pcap written by tcpdump -w")
    parser.add_argument("--filter", default="",
                        help="only messages whose text contains this")
    parser.add_argument("--full", action="store_true",
                        help="print the whole message, not just what it is")
    parser.add_argument("--start", type=float, default=0.0,
                        help="skip the first N seconds of the capture")
    args = parser.parse_args()

    streams = collections.defaultdict(Stream)
    first = None
    shown = 0
    for timestamp, flow, seq, payload in segments(args.capture):
        if first is None:
            first = timestamp
        stream = streams[flow]
        stream.add(seq, payload)
        for data in stream.messages():
            offset = timestamp - first
            if offset < args.start:
                continue
            text = data.decode("utf-8", "replace")
            if args.filter and args.filter not in text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                print(f"{offset:8.3f} {flow}  <not json> {text[:200]}")
                shown += 1
                continue
            print(f"{offset:8.3f} {flow}  {describe(message)}")
            if args.full:
                print(f"{' ' * 9}{json.dumps(message)[:4000]}")
            shown += 1

    print(f"\n{shown} message(s) from {len(streams)} stream(s)", file=sys.stderr)
    if not shown and streams:
        print("Streams carried data but no complete frame was read - was the "
              "capture truncated (tcpdump without -s 0)?", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
