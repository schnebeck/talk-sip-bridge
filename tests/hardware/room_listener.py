#!/usr/bin/env python3
# talk-sip-bridge - tests/hardware/room_listener.py
# A second internal connection that sits in a room and reports what it is
# told.
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

"""A second internal connection that sits in a room and reports what it
is told.

Answers one question the source can only half answer: whether splitting
this bridge's two jobs across two connections works. One connection
takes dialout requests and must therefore never enter a room
(`hub.go`: "An internal session in a room can not be used for dialout" -
and a session is only put back into that pool by a fresh `hello`). The
other enters the room, publishes the call's audio and hears what
happens there. Today both jobs share one connection, so the bridge is
only ever in the room after a call is answered - and "end meeting for
everyone" during the ringing reaches nobody.

Two things have to hold for the split to be sound, and neither is
documented:

1. The *other* connection keeps its dialout eligibility while this one
   is in a room. Press "call" in Talk and watch the daemon's journal.
2. This one is told when the call ends for everyone. Press "end meeting
   for everyone" and watch for
   `participants/update` with `all: true, incall: 0` below.

Run it on the Nextcloud host, with the daemon's own environment:

    set -a; . /etc/talk-sip-bridge/env; set +a
    BRIDGE_CODE=<where the daemon is installed> room_listener.py \\
        --room <token> --seconds 180

It publishes nothing and answers nothing, so it cannot disturb a call
in progress; it only joins and listens.
"""
import argparse
import asyncio
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, os.environ.get("BRIDGE_CODE")
                or str(pathlib.Path(__file__).resolve().parent.parent.parent / "bridge"))

import websockets                                          # noqa: E402

import talk_messages                                       # noqa: E402

# What the room-side role would declare: it never takes a dialout, and
# it owns its own in-call flags because it publishes the call's audio.
FEATURES = ["internal-incall"]


def summarise(message: dict) -> str:
    """The one line that says what happened, with the detail that
    matters for the question being asked."""
    kind = message.get("type", "?")
    if kind == "event":
        event = message.get("event", {})
        target, etype = event.get("target"), event.get("type")
        if target == "participants" and etype == "update":
            update = event.get("update", {})
            if update.get("all"):
                return (f"participants/update ALL incall={update.get('incall')}"
                        "   <- this is 'end meeting for everyone'")
            users = update.get("users") or []
            who = ", ".join(
                f"{u.get('actorId') or ('virtual' if u.get('virtual') else 'internal' if u.get('internal') else '?')}"
                f":{u.get('inCall')}" for u in users)
            return f"participants/update [{who}]"
        return f"event/{target}/{etype}"
    if kind == "room":
        return f"room -> {message.get('room', {}).get('roomid')}"
    if kind == "error":
        return f"error/{message.get('error', {}).get('code')}: {message.get('error', {}).get('message')}"
    if kind == "control":
        return f"control/{(message.get('control', {}).get('data') or {}).get('type')}"
    return kind


async def listen(url: str, secret: str, backend: str, roomid: str, seconds: float,
                 verbose: bool):
    async with websockets.connect(url) as ws:
        hello = talk_messages.hello(secret, backend)
        hello["hello"]["features"] = list(FEATURES)
        await ws.send(json.dumps(hello))
        await ws.recv()                                    # welcome banner
        answer = json.loads(await ws.recv())
        if answer.get("type") != "hello":
            raise SystemExit(f"not authenticated: {json.dumps(answer)[:300]}")
        print(f"connected as {answer['hello']['sessionid']}, features={FEATURES}")

        await ws.send(json.dumps(talk_messages.join_room(roomid)))
        print(f"joining {roomid} - from here on this connection can take no dialout")

        started = time.monotonic()
        while time.monotonic() - started < seconds:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=seconds)
            except asyncio.TimeoutError:
                break
            message = json.loads(raw)
            print(f"{time.monotonic() - started:7.1f} {summarise(message)}")
            if verbose:
                print(f"{' ' * 8}{raw[:1500]}")
        print("done listening")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--room", required=True, help="the conversation token")
    parser.add_argument("--seconds", type=float, default=180.0)
    parser.add_argument("--verbose", action="store_true", help="print each message whole")
    parser.add_argument("--url", default=os.environ.get("BRIDGE_WS_URL", ""))
    parser.add_argument("--secret", default=os.environ.get("BRIDGE_INTERNAL_SECRET", ""))
    parser.add_argument("--backend", default=os.environ.get("BRIDGE_BACKEND_URL", ""))
    args = parser.parse_args()
    if not (args.url and args.secret and args.backend):
        raise SystemExit("need BRIDGE_WS_URL, BRIDGE_INTERNAL_SECRET and "
                         "BRIDGE_BACKEND_URL (source the daemon's env file)")

    asyncio.run(listen(args.url, args.secret, args.backend, args.room,
                       args.seconds, args.verbose))


if __name__ == "__main__":
    sys.exit(main())
