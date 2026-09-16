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
        # Address/port to advertise in the SIP Contact header - where the
        # gateway sends calls and other requests for our registration. Only
        # needs to differ from local_ip/local_sip_port when a SIP proxy/relay
        # sits between this host and the gateway (see docs/CONFIG.md).
        self.contact_host = os.environ.get("BRIDGE_CONTACT_HOST", "")
        self.contact_port = int(os.environ.get("BRIDGE_CONTACT_PORT", "0") or 0)
        self.register_expires = int(os.environ.get("BRIDGE_REGISTER_EXPIRES", "600"))
        self.sip_response_timeout = float(os.environ.get("BRIDGE_SIP_RESPONSE_TIMEOUT", "6"))
        self.outbound_call_timeout = float(os.environ.get("BRIDGE_OUTBOUND_CALL_TIMEOUT", "30"))
        # Safety net against a call that never gets a BYE (e.g. the gateway
        # silently drops it) - without this, a single stuck call blocks
        # every other call indefinitely, since only one is ever handled at
        # a time. Four hours comfortably exceeds any real call.
        self.max_call_duration = float(os.environ.get("BRIDGE_MAX_CALL_DURATION", str(4 * 3600)))
        # Off by default: an incoming call is left ringing (never answered)
        # unless explicitly enabled. Must be set to "true" to answer inbound
        # calls automatically.
        self.auto_answer_calls = os.environ.get("BRIDGE_AUTO_ANSWER", "false").strip().lower() == "true"
        # Optional regex restricting which numbers may be dialed (e.g. an
        # internal-extension-only pattern during testing). Empty means no
        # restriction beyond the fixed character allowlist in sip_core.py.
        self.dialout_number_allowlist = os.environ.get("BRIDGE_DIALOUT_NUMBER_ALLOWLIST", "")
        # Talk formats any number a user enters into E.164 (a country-code
        # prefix, e.g. "+49", regardless of whether it's actually a national
        # phone number or a short internal extension). The gateway's own
        # dial plan expects internal extensions bare, without that prefix -
        # this strips it (once, from the start of the number) before dialing
        # if present. Empty means no stripping.
        self.dialout_strip_prefix = os.environ.get("BRIDGE_DIALOUT_STRIP_PREFIX", "")
        # Prepended to a number after BRIDGE_DIALOUT_STRIP_PREFIX is removed,
        # i.e. only for numbers recognized as internal extensions - the
        # gateway's own notation for reaching a physical device by extension
        # (e.g. "**" on a FritzBox). Only applied when stripping happened, so
        # a real external number dialed without that prefix is left alone.
        self.dialout_internal_dial_prefix = os.environ.get("BRIDGE_DIALOUT_INTERNAL_DIAL_PREFIX", "")

        # Automatic gain control for audio coming from the phone side before
        # it is published into Talk - see agc.py. On by default: some
        # handsets (e.g. a DECT cordless) have a much quieter microphone
        # than a laptop/headset, with no way to adjust that from this end of
        # the call, and a fixed multiplier would either clip on loud moments
        # or stay too quiet on soft ones.
        self.agc_enabled = os.environ.get("BRIDGE_AGC_ENABLED", "true").strip().lower() == "true"
        self.agc_target_peak = int(os.environ.get("BRIDGE_AGC_TARGET_PEAK", "10000"))
        self.agc_max_gain = float(os.environ.get("BRIDGE_AGC_MAX_GAIN", "20.0"))

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

        # Local HTTP control API (status/toggle) for the Nextcloud app to
        # call. Bind to an address reachable from the Nextcloud container
        # (e.g. the Docker bridge gateway), never 0.0.0.0.
        self.control_bind = os.environ.get("BRIDGE_CONTROL_BIND", "127.0.0.1")
        self.control_port = int(os.environ.get("BRIDGE_CONTROL_PORT", "8765"))

    @property
    def media_relay_enabled(self) -> bool:
        return bool(self.relay_lan_host and self.relay_overlay_host)


config = Config()
