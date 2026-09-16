"""Configuration, loaded entirely from environment variables - see
docs/CONFIG.md for the full list.

The bridge can run several SIP lines side by side (each its own registered
account, ports, gateway/registrar and dial-out number handling) rather than
being tied to a single phone line - see LineConfig and Config.lines. Set
BRIDGE_LINES to a comma-separated list of line ids to configure more than
one; leave it unset for a single line configured via the flat BRIDGE_*
variables (unchanged from a single-line deployment).
"""
import os
import re

_LINE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


class LineConfig:
    """One registered SIP account/number: which gateway it registers with,
    which local ports it uses, and how dial-out numbers are mapped for it.
    Independent of every other line - different lines may point at entirely
    different registrars."""

    def __init__(self, line_id: str, local_ip: str, *, env_prefix: str,
                 default_local_sip_port: str = None, default_local_rtp_port: str = None):
        self.id = line_id
        self.local_ip = local_ip

        def env(suffix, default=None):
            return os.environ.get(env_prefix + suffix, default)

        def env_required(suffix):
            value = env(suffix)
            if not value:
                raise RuntimeError(f"Missing required environment variable: {env_prefix}{suffix}")
            return value

        self.sip_user = env_required("SIP_USER")
        self.sip_pass = env_required("SIP_PASS")
        self.gateway_host = env_required("GATEWAY_HOST")  # e.g. the FritzBox, or any other SIP registrar
        self.proxy_host = env("PROXY_HOST", self.gateway_host)
        self.proxy_port = int(env("PROXY_PORT", "5060"))
        self.local_sip_port = int(env("LOCAL_SIP_PORT", default_local_sip_port) if default_local_sip_port else env_required("LOCAL_SIP_PORT"))
        self.local_rtp_port = int(env("LOCAL_RTP_PORT", default_local_rtp_port) if default_local_rtp_port else env_required("LOCAL_RTP_PORT"))
        # Address/port to advertise in the SIP Contact header - where the
        # gateway sends calls and other requests for this line's
        # registration. Only needs to differ from local_ip/local_sip_port
        # when a SIP proxy/relay sits between this host and the gateway.
        self.contact_host = env("CONTACT_HOST", "")
        self.contact_port = int(env("CONTACT_PORT", "0") or 0)
        # Which Talk room an inbound call on this line is bridged into.
        self.default_room_token = env("DEFAULT_ROOM", "")
        # Optional regex restricting which numbers may be dialed out through
        # this line, and how a dialed number is mapped to this gateway's own
        # dial plan - see docs/CONFIG.md. With more than one line, a
        # dial-out number is routed to whichever line's allowlist matches it
        # (see talk_client.py's line selection) - a line with an empty
        # allowlist is only used as a catch-all after every line with a
        # specific allowlist has been tried.
        self.dialout_number_allowlist = env("DIALOUT_NUMBER_ALLOWLIST", "")
        self.dialout_strip_prefix = env("DIALOUT_STRIP_PREFIX", "")
        self.dialout_internal_dial_prefix = env("DIALOUT_INTERNAL_DIAL_PREFIX", "")

        # Optional media relay for this line (only needed if its gateway
        # can't reach this host's own address directly for RTP).
        self.relay_lan_host = env("RELAY_LAN_HOST", "")
        self.relay_lan_port = int(env("RELAY_LAN_PORT", "0") or 0)
        self.relay_overlay_host = env("RELAY_OVERLAY_HOST", "")
        self.relay_overlay_port = int(env("RELAY_OVERLAY_PORT", "0") or 0)

    @property
    def media_relay_enabled(self) -> bool:
        return bool(self.relay_lan_host and self.relay_overlay_host)

    def __repr__(self):
        return f"LineConfig({self.id!r}, {self.sip_user}@{self.gateway_host})"


class Config:
    def __init__(self):
        self.local_ip = _require("BRIDGE_LOCAL_IP")

        line_ids_raw = os.environ.get("BRIDGE_LINES", "").strip()
        if line_ids_raw:
            ids = [x.strip() for x in line_ids_raw.split(",") if x.strip()]
            self.lines = []
            for line_id in ids:
                if not _LINE_ID_PATTERN.match(line_id):
                    raise RuntimeError(
                        f"Invalid BRIDGE_LINES entry {line_id!r} - only letters, digits and underscore allowed")
                self.lines.append(LineConfig(line_id, self.local_ip, env_prefix=f"BRIDGE_LINE_{line_id}_"))
        else:
            # Single-line fallback: one implicit line built from the flat
            # BRIDGE_* variables, exactly as a single-line deployment always
            # configured itself before BRIDGE_LINES existed.
            self.lines = [LineConfig(
                "default", self.local_ip, env_prefix="BRIDGE_",
                default_local_sip_port="5060", default_local_rtp_port="40000",
            )]

        self.register_expires = int(os.environ.get("BRIDGE_REGISTER_EXPIRES", "600"))
        self.sip_response_timeout = float(os.environ.get("BRIDGE_SIP_RESPONSE_TIMEOUT", "6"))
        self.outbound_call_timeout = float(os.environ.get("BRIDGE_OUTBOUND_CALL_TIMEOUT", "30"))
        # Safety net against a call that never gets a BYE (e.g. the gateway
        # silently drops it) - without this, a stuck call blocks that line
        # indefinitely. Four hours comfortably exceeds any real call.
        self.max_call_duration = float(os.environ.get("BRIDGE_MAX_CALL_DURATION", str(4 * 3600)))
        # Off by default: an incoming call is left ringing (never answered)
        # unless explicitly enabled. Must be set to "true" to answer inbound
        # calls automatically.
        self.auto_answer_calls = os.environ.get("BRIDGE_AUTO_ANSWER", "false").strip().lower() == "true"

        # Automatic gain control for audio coming from the phone side before
        # it is published into Talk - see agc.py. On by default: some
        # handsets (e.g. a DECT cordless) have a much quieter microphone
        # than a laptop/headset, with no way to adjust that from this end of
        # the call, and a fixed multiplier would either clip on loud moments
        # or stay too quiet on soft ones.
        self.agc_enabled = os.environ.get("BRIDGE_AGC_ENABLED", "true").strip().lower() == "true"
        self.agc_target_peak = int(os.environ.get("BRIDGE_AGC_TARGET_PEAK", "10000"))
        self.agc_max_gain = float(os.environ.get("BRIDGE_AGC_MAX_GAIN", "20.0"))

        # Talk/signaling side.
        self.ws_url = _require("BRIDGE_WS_URL")  # standalone signaling server, e.g. ws://127.0.0.1:8080/spreed
        self.internal_secret = _require("BRIDGE_INTERNAL_SECRET")
        self.backend_url = _require("BRIDGE_BACKEND_URL")  # e.g. https://nextcloud.example

        # Local HTTP control API (status/toggle) for the Nextcloud app to
        # call. Bind to an address reachable from the Nextcloud container
        # (e.g. the Docker bridge gateway), never 0.0.0.0.
        self.control_bind = os.environ.get("BRIDGE_CONTROL_BIND", "127.0.0.1")
        self.control_port = int(os.environ.get("BRIDGE_CONTROL_PORT", "8765"))


config = Config()
