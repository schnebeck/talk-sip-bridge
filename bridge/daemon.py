#!/usr/bin/env python3
"""Production daemon: for each configured line (see config.py's Config.lines
- one by default, more if BRIDGE_LINES is set), connects to Talk's signaling
server as a dedicated dialout-capable internal client and registers with
that line's own gateway. Exposes a local HTTP control API (GET /status,
POST /toggle) for the Nextcloud app to enable/disable registration.
Registration starts off; nothing calls a gateway until toggled on. See
docs/CONCEPT.md for the architecture and docs/CONFIG.md for the required
environment variables.

Each line gets its own SipTransport/SipRegistrar/CallManager and its own
TalkClient (hence its own WebSocket connection to the signaling server) -
necessary, not just simpler: an internal client loses its dialout
eligibility for as long as it's joined to a room publishing a call (see
docs/CONCEPT.md point 3), so lines sharing one Talk connection would make
each other's dialout unavailable while either has a call in progress.
"""
import time

from config import config, LineConfig
import control_api
import sip_core
import talk_client


def _start_line(line: LineConfig):
    call_manager = sip_core.CallManager(line)
    client = talk_client.start_in_background(call_manager)
    call_manager.on_incoming_call = client.on_incoming_call
    call_manager.on_call_connected = client.on_call_connected
    call_manager.on_call_ended = client.on_call_ended
    call_manager.on_call_failed = client.on_call_failed

    transport_holder = {}

    def get_transport():
        return transport_holder["transport"]

    registrar = sip_core.SipRegistrar(get_transport, line)
    transport_holder["transport"] = sip_core.SipTransport(call_manager, line)
    call_manager.transport = transport_holder["transport"]
    return registrar, call_manager


def main():
    lines = {}
    for line in config.lines:
        registrar, call_manager = _start_line(line)
        lines[line.id] = (registrar, call_manager)
        print(f"[daemon] Line {line.id} ({line.sip_user}@{line.gateway_host}) ready - "
              f"not yet registered, toggle via the control API.")

    control_api.start_in_background(config.control_bind, config.control_port, lines)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        for registrar, _ in lines.values():
            registrar.turn_off()


if __name__ == "__main__":
    main()
