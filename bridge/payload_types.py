# talk-sip-bridge - bridge/payload_types.py
# RTP payload type numbers from the static table in RFC 3551.
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
