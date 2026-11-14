# talk-sip-bridge - bridge/signaling.py
# Staying connected to the signaling server.
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

"""Staying connected to the signaling server.

One connection, kept up for the life of the process, reading one
message at a time. Nothing here knows what the messages mean - it is
handed a handler and a place to put the connection once it is up.

Both promises it makes exist because of what breaks without them. A
message that cannot be handled must cost only itself: letting it reach
the connection reconnects, and a reconnect during a call takes that
call's signaling with it. And a loop that stops must be started again,
because a bridge whose connection is gone keeps answering the phone and
reaches nobody, with nothing in the journal to say so.
"""
import asyncio
import json
import traceback

import websockets

import talk_messages
from config import config


async def supervise(role: str, serve):
    """Keeps one connection's loop alive whatever it does.

    `_serve` loops forever, so nothing should arrive here - but a
    connection that quietly stops is the worst failure this bridge
    has: calls keep being answered and none of them reaches Talk,
    with nothing in the journal to say why. Restarting is always
    better than the two of them ending together, which is what a
    bare `gather` would do."""
    while True:
        try:
            await serve()
            print(f"[talk] ERROR the {role} connection loop ended by itself - restarting")
        except Exception as e:
            print(f"[talk] ERROR the {role} connection loop failed: {e!r} - restarting")
            traceback.print_exc()
        await asyncio.sleep(config.sip_response_timeout)

# Longest pause between reconnect attempts while the signaling server
# stays away (e.g. Nextcloud AIO stops its containers for the nightly
# backup): long enough to keep the journal readable, short enough that
# the bridge is back within seconds of Talk.
RECONNECT_MAX_DELAY = 30.0

async def serve(role: str, features: list, settled, handle):
    failures = 0
    while True:
        try:
            async with websockets.connect(config.ws_url) as ws:
                settled(ws, await hello(ws, role, features))
                if failures > 1:
                    print(f"[talk] {role} connection back after {failures} failed attempts")
                failures = 0
                await read(role, ws, handle)
        except Exception as e:
            failures += 1
            # the first loss and then every 20th attempt, not every few seconds
            if failures == 1 or failures % 20 == 0:
                again = f" (attempt {failures})" if failures > 1 else ""
                print(f"[talk] {role} connection lost ({e!r}), reconnecting{again} ...")
        settled(None, None)
        # first retry as before, then doubling up to RECONNECT_MAX_DELAY
        delay = config.sip_response_timeout * 2 ** max(failures - 1, 0)
        await asyncio.sleep(min(delay, max(RECONNECT_MAX_DELAY, config.sip_response_timeout)))

async def read(role: str, ws, handle):
    """One message at a time, each costing only itself.

    A message that cannot be handled must not reach the connection:
    letting it through would reconnect, and a reconnect during a call
    takes that call's signaling down over a message that had nothing
    to do with it."""
    async for raw in ws:
        try:
            await handle(json.loads(raw))
        except Exception as e:
            print(f"[talk] ERROR handling a {role} message: {e!r}")
            traceback.print_exc()

async def hello(ws, role: str, features: list) -> str:
    await ws.send(json.dumps(
        talk_messages.hello(config.internal_secret, config.backend_url, features)))
    await ws.recv()  # welcome banner
    resp = json.loads(await ws.recv())
    sessionid = resp["hello"]["sessionid"]
    print(f"[talk] {role} connection up as internal client, session {sessionid}")
    return sessionid
