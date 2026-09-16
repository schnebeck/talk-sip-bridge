#!/usr/bin/env python3
"""Production daemon: connects to Talk's signaling server as a
dialout-capable internal client and exposes a local HTTP control API
(GET /status, POST /toggle) for the Nextcloud app to enable/disable gateway
registration. Registration starts off; nothing calls the gateway until
toggled on. See docs/CONCEPT.md for the architecture and docs/CONFIG.md for
the required environment variables.
"""
import time

from config import config
import control_api
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

    control_api.start_in_background(config.control_bind, config.control_port, registrar, call_manager)
    print(f"[daemon] Ready ({config.sip_user} not yet registered - toggle via the control API).")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        registrar.turn_off()


if __name__ == "__main__":
    main()
