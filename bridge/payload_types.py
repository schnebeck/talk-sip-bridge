"""RTP payload type numbers from the static table in RFC 3551.

Separate from rtp.py so that code which only needs the numbers - SDP
building, for instance - does not pull in the codec implementations and
their media-library dependency with them.
"""
PT_PCMU = 0
PT_G722 = 9
