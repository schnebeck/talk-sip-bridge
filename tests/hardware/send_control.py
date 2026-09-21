#!/usr/bin/env python3
# talk-sip-bridge - tests/hardware/send_control.py
# Sends one control message to a phone session, the way Talk's own UI does.
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

"""Sends one control message to a phone session, the way Talk's own UI
does.

Talk's participant list hangs up a phone by sending `control` with
`{"type": "hangup"}` addressed to that phone's signaling session. The
server hands it to whoever owns the virtual session - this bridge - and
that is the only way an outbound call that is still ringing can be
stopped from the Nextcloud side.

This is the other end of that path, without a browser. When a hangup in
Talk does nothing, the question is whether the browser never sent it or
this bridge never acted on it, and one log on either side cannot answer
that. Sending the identical message from here does: if the call ends,
everything from the server inwards works.

Run it on the Nextcloud host, with the daemon's own environment:

    set -a; . /etc/talk-sip-bridge/env; set +a
    BRIDGE_CODE=<where the daemon is installed> send_control.py \
        --session <public-session-id> --type hangup

The session id is the phone's *public* one, which is also what Nextcloud
stores for the phone attendee (`oc_talk_sessions.session_id`) and what
the browser addresses - not the `phone-...` name this bridge picked.
Internal clients may send any control message (`isAllowedToControl`), so
this needs no moderator and no room membership.
"""
import argparse
import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, os.environ.get("BRIDGE_CODE")
                or str(pathlib.Path(__file__).resolve().parent.parent.parent / "bridge"))

import websockets                                          # noqa: E402

import talk_messages                                       # noqa: E402

PAYLOADS = {
    "hangup": {"type": "hangup"},
    "mute": {"type": "mute", "audio": 1},
    "unmute": {"type": "mute", "audio": 0},
}


async def send(url: str, secret: str, backend: str, session: str, payload: dict,
               listen: float):
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps(talk_messages.hello(secret, backend)))
        await ws.recv()                                    # welcome banner
        hello = json.loads(await ws.recv())
        if hello.get("type") != "hello":
            raise SystemExit(f"not authenticated: {json.dumps(hello)[:300]}")
        print(f"connected as {hello['hello']['sessionid']}")

        message = {
            "id": "probe-control",
            "type": "control",
            "control": {
                "recipient": {"type": "session", "sessionid": session},
                "data": payload,
            },
        }
        await ws.send(json.dumps(message))
        print(f"sent {payload['type']} -> {session}")

        # Nothing is acknowledged, so anything coming back is a refusal
        # worth reading - an unknown recipient is silently dropped.
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=listen)
                print(f"  <- {raw[:400]}")
        except asyncio.TimeoutError:
            print(f"nothing said in {listen:.0f}s (no news is the normal case)")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", required=True, help="the phone's public session id")
    parser.add_argument("--type", default="hangup", choices=sorted(PAYLOADS))
    parser.add_argument("--listen", type=float, default=3.0,
                        help="seconds to wait for anything the server says back")
    parser.add_argument("--url", default=os.environ.get("BRIDGE_WS_URL", ""))
    parser.add_argument("--secret", default=os.environ.get("BRIDGE_INTERNAL_SECRET", ""))
    parser.add_argument("--backend", default=os.environ.get("BRIDGE_BACKEND_URL", ""))
    args = parser.parse_args()
    if not (args.url and args.secret and args.backend):
        raise SystemExit("need BRIDGE_WS_URL, BRIDGE_INTERNAL_SECRET and "
                         "BRIDGE_BACKEND_URL (source /etc/talk-sip-bridge/env)")

    asyncio.run(send(args.url, args.secret, args.backend, args.session,
                     PAYLOADS[args.type], args.listen))


if __name__ == "__main__":
    sys.exit(main())
