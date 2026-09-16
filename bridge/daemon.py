#!/usr/bin/env python3
"""Production daemon: registers with the phone gateway, connects to Talk's
signaling server as a dialout-capable internal client, and bridges real
audio between the two for both inbound and outbound calls. See
docs/CONCEPT.md for the architecture and docs/CONFIG.md for the required
environment variables.
"""
import sys
import time

from config import config
import sip_core
import talk_client


def main():
    call_manager = sip_core.CallManager()
    client = talk_client.start_in_background(call_manager)
    call_manager.on_incoming_call = client.on_incoming_call
    call_manager.on_call_connected = client.on_call_connected
    call_manager.on_call_ended = client.on_call_ended
    call_manager.on_call_failed = client.on_call_failed

    transport_holder = {}

    def get_transport():
        return transport_holder["transport"]

    registrar = sip_core.SipRegistrar(get_transport)
    transport_holder["transport"] = sip_core.SipTransport(call_manager)
    call_manager.transport = transport_holder["transport"]

    print(f"[daemon] Registering {config.sip_user} against {config.proxy_host}:{config.proxy_port} ...")
    if not registrar.turn_on():
        print(f"[daemon] Registration failed: {registrar.last_error}", file=sys.stderr)
        sys.exit(1)
    print("[daemon] Registered. Waiting for calls / dialout requests ...")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        registrar.turn_off()


if __name__ == "__main__":
    main()
