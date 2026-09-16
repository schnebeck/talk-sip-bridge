"""Configuration, loaded entirely from environment variables - see
docs/CONFIG.md for the full list.

The connectivity mode (direct vs. relayed) is a config choice: PROXY_HOST/
PROXY_PORT point at whatever the SIP gateway is reachable through - a relay,
or the phone gateway directly if it's reachable from this host.
"""
import os


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


class Config:
    def __init__(self):
        # SIP side: the phone gateway account and how to reach it.
        self.sip_user = _require("BRIDGE_SIP_USER")
        self.sip_pass = _require("BRIDGE_SIP_PASS")
        self.gateway_host = _require("BRIDGE_GATEWAY_HOST")  # e.g. the FritzBox
        self.proxy_host = os.environ.get("BRIDGE_PROXY_HOST", self.gateway_host)
        self.proxy_port = int(os.environ.get("BRIDGE_PROXY_PORT", "5060"))
        self.local_ip = _require("BRIDGE_LOCAL_IP")
        self.local_sip_port = int(os.environ.get("BRIDGE_LOCAL_SIP_PORT", "5060"))
        self.local_rtp_port = int(os.environ.get("BRIDGE_LOCAL_RTP_PORT", "40000"))
        self.register_expires = int(os.environ.get("BRIDGE_REGISTER_EXPIRES", "600"))
        self.sip_response_timeout = float(os.environ.get("BRIDGE_SIP_RESPONSE_TIMEOUT", "6"))
        self.outbound_call_timeout = float(os.environ.get("BRIDGE_OUTBOUND_CALL_TIMEOUT", "30"))
        # Off by default: an incoming call is left ringing (never answered)
        # unless explicitly enabled. Must be set to "true" to answer inbound
        # calls automatically.
        self.auto_answer_calls = os.environ.get("BRIDGE_AUTO_ANSWER", "false").strip().lower() == "true"
        # Optional regex restricting which numbers may be dialed (e.g. an
        # internal-extension-only pattern during testing). Empty means no
        # restriction beyond the fixed character allowlist in sip_core.py.
        self.dialout_number_allowlist = os.environ.get("BRIDGE_DIALOUT_NUMBER_ALLOWLIST", "")

        # Optional media relay (only needed if the gateway can't reach this
        # host's own address directly for RTP - see docs/CONFIG.md).
        self.relay_lan_host = os.environ.get("BRIDGE_RELAY_LAN_HOST", "")
        self.relay_lan_port = int(os.environ.get("BRIDGE_RELAY_LAN_PORT", "0") or 0)
        self.relay_overlay_host = os.environ.get("BRIDGE_RELAY_OVERLAY_HOST", "")
        self.relay_overlay_port = int(os.environ.get("BRIDGE_RELAY_OVERLAY_PORT", "0") or 0)

        # Talk/signaling side.
        self.ws_url = _require("BRIDGE_WS_URL")  # standalone signaling server, e.g. ws://127.0.0.1:8080/spreed
        self.internal_secret = _require("BRIDGE_INTERNAL_SECRET")
        self.backend_url = _require("BRIDGE_BACKEND_URL")  # e.g. https://nextcloud.example
        self.default_room_token = os.environ.get("BRIDGE_DEFAULT_ROOM", "")

    @property
    def media_relay_enabled(self) -> bool:
        return bool(self.relay_lan_host and self.relay_overlay_host)


config = Config()
