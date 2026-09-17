#!/usr/bin/env python3
"""Production daemon: for each configured line (see config.py's Config.lines
- one by default, more if BRIDGE_LINES is set), connects to Talk's signaling
server as a dedicated dialout-capable internal client and registers with
that line's own gateway. Exposes a local HTTP control API (GET /status,
POST /toggle) for the Nextcloud app to enable/disable registration. A
brand new deployment starts with registration off, nothing calls a gateway
until toggled on; a line that was on when the process last stopped resumes
automatically (see sip_core.SipRegistrar's state-file persistence), so a
crash-triggered restart doesn't silently leave the phone line dead until
someone notices. See docs/CONCEPT.md for the architecture and
docs/CONFIG.md for the required environment variables.

Each line gets its own SipTransport/SipRegistrar/CallManager and its own
TalkClient (hence its own WebSocket connection to the signaling server) -
necessary, not just simpler: an internal client loses its dialout
eligibility for as long as it's joined to a room publishing a call (see
docs/CONCEPT.md point 3), so lines sharing one Talk connection would make
each other's dialout unavailable while either has a call in progress.
"""
import signal
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
        if registrar.was_registered_before_restart():
            # Resume automatically rather than leaving the line silently
            # deregistered until someone notices and toggles it back on by
            # hand - the common case this matters for is systemd's
            # Restart=on-failure bringing the process back up after a
            # crash, not a deliberate stop (an admin explicitly toggling
            # off, or `systemctl stop`, clears the persisted state first).
            ok = registrar.turn_on()
            print(f"[daemon] Line {line.id} ({line.sip_user}@{line.gateway_host}) "
                  f"was registered before restart - resumed: {ok} (last_error={registrar.last_error})")
        else:
            print(f"[daemon] Line {line.id} ({line.sip_user}@{line.gateway_host}) ready - "
                  f"not yet registered, toggle via the control API.")

    control_api.start_in_background(config.control_bind, config.control_port, lines)

    def shut_down(signum=None, frame=None):
        """Hang up before going away. A gateway that never receives a BYE
        keeps the call - and keeps streaming its audio at this host
        indefinitely, where it mixes into every later call through the same
        media path. systemd stops this service with SIGTERM, so without
        handling it every restart during a call leaves such a stream
        behind."""
        for registrar, call_manager in lines.values():
            try:
                call_manager.hangup()
            except Exception as e:
                print(f"[daemon] Could not hang up on shutdown: {e!r}")
        for registrar, _ in lines.values():
            # Deregister, but keep the stored state: this line is meant to
            # be registered, and a restart has to bring it back up.
            registrar.turn_off(persist=False)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shut_down)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        shut_down()


if __name__ == "__main__":
    main()
