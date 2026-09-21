# talk-sip-bridge - bridge/mix_pump.py
# The one place a call's mixed audio is clocked out to the telephone.
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
"""The one place a call's mixed audio is clocked out to the telephone.

The mixer knows nothing about time; it hands out a block whenever asked.
Something has to ask at the right rate, and this is it: one task per
call, one block every 20ms, resampled once from Talk's 48kHz to whatever
the call negotiated and handed to the RTP session.

**Paced against a fixed schedule**, not by sleeping 20ms after each
send. Sleeping adds each send's own duration to the interval, and the
stream then runs slow by however long the work took - measured
elsewhere in this project at 20.77ms per 20ms packet, which a gateway
re-clocks and a listener hears as chopping.

This is where the clock lives, deliberately and in one place. The mixer
stays free of it and therefore testable; everything that has to happen
in real time happens here.

An empty mixer yields silence rather than nothing. A telephone call is
owed a continuous stream: gaps in it are not silence to a gateway, they
are a stalled sender, and some of them fill the hole with whatever was
in the buffer last.
"""
import asyncio

import numpy as np

from media import StreamResampler
from mixer import SAMPLE_RATE


async def pump(mixer, rtp_session, should_run, on_error=None):
    """Clocks `mixer` into `rtp_session` for as long as `should_run()`.

    Runs until cancelled or until should_run() turns false. Never raises
    into its caller: a fault here would silence the call in one
    direction and nothing else would notice, so it is reported and the
    loop ends.
    """
    loop = asyncio.get_running_loop()
    interval = mixer.block / SAMPLE_RATE
    resampler = StreamResampler(SAMPLE_RATE, rtp_session.sample_rate)
    packet = rtp_session.samples_per_packet
    pending = np.zeros(0, dtype=np.int16)
    due = loop.time()
    try:
        while should_run():
            block = mixer.read()
            # Whole packets only: send_pcm pads a short one with silence,
            # and a resampler that carries its phase hands out 159
            # samples as readily as 160 - padding each of those would put
            # back the very artefact resampling removes.
            pending = np.concatenate((pending, resampler.process(block)))
            whole = len(pending) - len(pending) % packet
            if whole:
                await asyncio.to_thread(rtp_session.send_pcm, pending[:whole])
                pending = pending[whole:]
            due += interval
            delay = due - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                # Behind schedule. Catching up by sending faster would
                # only move the backlog to the gateway, so the schedule
                # is reset and the lost time is lost.
                due = loop.time()
    except asyncio.CancelledError:
        raise
    except Exception as e:                                     # noqa: BLE001
        if on_error is not None:
            on_error(e)
