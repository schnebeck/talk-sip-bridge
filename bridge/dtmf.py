"""Key presses carried as RTP events (RFC 4733, "telephone-event").

A phone does not send DTMF as audio to a gateway that negotiated it as
events: it sends a small packet every 20ms for as long as the key is held,
all of them carrying the same RTP timestamp, and repeats the last one with
an end marker. One key press is therefore dozens of packets, and the
timestamp is what tells a second press of the same key from a repeat of the
first.

Nothing here decodes audio, so it stays free of the media stack: the number
this arrives under is negotiated per call (see sip_sdp), and what to do
with a digit belongs to whoever is listening.
"""
import struct

# RFC 4733 section 3.2: events 0-9 are the digits, then star and hash, then
# the four tones no modern handset has a key for.
EVENT_DIGITS = "0123456789*#ABCD"


def parse_event(payload: bytes):
    """(digit, end_of_event, duration) from one telephone-event payload, or
    None if it is not one. Four bytes: event, then a flag byte whose top
    bit marks the end, then the duration so far."""
    if len(payload) < 4:
        return None
    event, flags, duration = struct.unpack("!BBH", payload[:4])
    if event >= len(EVENT_DIGITS):
        return None
    return EVENT_DIGITS[event], bool(flags & 0x80), duration


class DtmfEvents:
    """Turns that packet storm into one digit per key press.

    Keyed on the RTP timestamp, which stays put for the whole press: the
    same digit pressed twice arrives under two timestamps and counts twice,
    while forty packets of one press count once. Reporting on the first
    packet rather than on the end marker means a digit is known while the
    key is still down - the end marker can also be lost, and waiting for it
    would drop the press entirely.
    """

    def __init__(self):
        self._reported = None

    def feed(self, rtp_timestamp: int, payload: bytes):
        """The digit if this packet starts a new key press, else None."""
        parsed = parse_event(payload)
        if parsed is None:
            return None
        digit, _end, _duration = parsed
        if self._reported == rtp_timestamp:
            return None
        self._reported = rtp_timestamp
        return digit
