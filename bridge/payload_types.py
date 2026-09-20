"""RTP payload type numbers from the static table in RFC 3551.

Separate from rtp.py so that code which only needs the numbers - SDP
building, for instance - does not pull in the codec implementations and
their media-library dependency with them.
"""
PT_PCMU = 0
PT_PCMA = 8
PT_G722 = 9

# Dynamic by definition - this is the number this bridge offers for
# telephone-event (RFC 4733 DTMF); what a peer offers is read from its
# SDP, never assumed.
PT_TELEPHONE_EVENT = 101
