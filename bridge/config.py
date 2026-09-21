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


def _number_map(raw: str, name: str) -> dict:
    """Parses "a=b,c=d" into {"a": "b", "c": "d"}.

    A malformed entry is refused rather than skipped: a mapping silently
    one entry short means calls to that number quietly take the wrong
    path, and nothing about the running bridge would say so."""
    mapping = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        left, sep, right = entry.partition("=")
        if not (sep and left.strip() and right.strip()):
            raise RuntimeError(f"{name} entry {entry!r} is not <dialled>=<number>")
        mapping[left.strip()] = right.strip()
    return mapping


def _seconds(name: str, default: float) -> float:
    """A duration from the environment. A value that is not a positive
    number is refused at startup rather than turning into a dialogue that
    never waits or never gives up."""
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a number of seconds, not {raw!r}")
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero, not {value}")
    return value


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
        # Which transport this line's SIP runs over. Gateways differ: some
        # registrars accept UDP, others only TCP and drop UDP without a
        # word. It is a per-line setting because a deployment can hold
        # lines at different gateways.
        self.sip_transport = (env("SIP_TRANSPORT", "udp") or "udp").strip().lower()
        if self.sip_transport not in ("udp", "tcp"):
            raise RuntimeError(f"{env_prefix}SIP_TRANSPORT must be udp or tcp, not {self.sip_transport!r}")
        self.local_sip_port = int(env("LOCAL_SIP_PORT", default_local_sip_port) if default_local_sip_port else env_required("LOCAL_SIP_PORT"))
        self.local_rtp_port = int(env("LOCAL_RTP_PORT", default_local_rtp_port) if default_local_rtp_port else env_required("LOCAL_RTP_PORT"))
        # Address/port to advertise in the SIP Contact header - where the
        # gateway sends calls and other requests for this line's
        # registration. Only needs to differ from local_ip/local_sip_port
        # when a SIP proxy/relay sits between this host and the gateway.
        self.contact_host = env("CONTACT_HOST", "")
        self.contact_port = int(env("CONTACT_PORT", "0") or 0)
        # Which transport the gateway should use towards that contact.
        # Defaults to this line's own; it differs when a relay changes
        # transport on the way, which is the case this deployment runs:
        # the bridge speaks UDP to the relay, the relay TCP to the gateway,
        # and the gateway must be told TCP.
        self.contact_transport = (env("CONTACT_TRANSPORT", "") or "").strip().lower()
        if self.contact_transport not in ("", "udp", "tcp"):
            raise RuntimeError(f"{env_prefix}CONTACT_TRANSPORT must be udp or tcp, not {self.contact_transport!r}")
        # Which Talk room an inbound call on this line is bridged into.
        self.default_room_token = env("DEFAULT_ROOM", "")
        # Optional regex restricting which numbers may be dialed out through
        # this line, and how a dialed number is mapped to this gateway's own
        # dial plan - see docs/CONFIG.md. With more than one line, which
        # line actually places a given dial-out call is not determined by
        # this allowlist - see docs/CONFIG.md "Single line vs. multiple
        # lines" for the current limitation there.
        self.dialout_number_allowlist = env("DIALOUT_NUMBER_ALLOWLIST", "")
        self.dialout_strip_prefix = env("DIALOUT_STRIP_PREFIX", "")
        self.dialout_internal_dial_prefix = env("DIALOUT_INTERNAL_DIAL_PREFIX", "")
        # Nextcloud account the bridge signs in as (via Talk's own OCS call
        # API, see talk_client.py's _talk_ring_start_sync) to make a call
        # ring on this line's behalf - lets a human "win" against a
        # FritzBox-side parallel ring group by joining the call in Talk
        # before another device answers it. Both empty (the default)
        # disables this entirely for the line - it then just rings unnoticed
        # by Talk, as before this existed. notify_app_password is an app
        # password for that account (Settings -> Security -> "Create new
        # app password"), not its real login password.
        self.notify_user = env("NOTIFY_USER", "")
        self.notify_app_password = env("NOTIFY_APP_PASSWORD", "")

        # Which of the numbers reaching this line are the bridge's own,
        # as "<what the INVITE says>=<the number Nextcloud has in
        # talk_phone_numbers>", comma separated. Both halves are needed
        # because a gateway announces an internal extension ("**622") in
        # the INVITE, which is no phone number to anyone but itself.
        #
        # A call to a mapped number is a call to the bridge: Nextcloud
        # creates the conversation for it and the bridge answers it (see
        # talk_sip_bridge.direct_dial_in). A call to any other number
        # rings as it always did - which is what lets one line carry both,
        # and keeps a line whose number is also a person's own phone from
        # ever being answered by a machine.
        self.dialin_numbers = _number_map(env("DIALIN_NUMBERS", ""), env_prefix + "DIALIN_NUMBERS")

        # Numbers on this line that are a conference line rather than one
        # person's: comma separated, as the INVITE announces them. A call
        # to one of these is answered and asked which conversation it
        # wants (see dialin_ivr.py), instead of Nextcloud deciding from
        # the number who is being called.
        self.conference_numbers = [n.strip() for n in (env("CONFERENCE_NUMBERS", "") or "").split(",")
                                   if n.strip()]
        # Who may reach the conference numbers: a regex the caller's own
        # number must fully match, empty for anybody. On a line whose
        # number also rings a person's phone this is what keeps the two
        # apart - "^\*\*[0-9]+$" admits the gateway's own extensions and
        # leaves every external call ringing as before.
        self.conference_callers = env("CONFERENCE_CALLERS", "")
        try:
            re.compile(self.conference_callers)
        except re.error as e:
            raise RuntimeError(f"{env_prefix}CONFERENCE_CALLERS is not a valid regex: {e}")

        both = sorted(set(self.conference_numbers) & set(self.dialin_numbers))
        if both:
            raise RuntimeError(f"{env_prefix}CONFERENCE_NUMBERS and {env_prefix}DIALIN_NUMBERS "
                               f"both claim {', '.join(both)} - a number is one or the other")

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
        # Measured on a DECT handset: speech peaks around 2000-4000 and the
        # line's own noise floor sits at 150-250. A gain ceiling of 20 lets
        # the AGC ride that noise floor up between words, which is heard as
        # a bubbling background; 8 is more than enough to bring real speech
        # to the target. The silence threshold has to clear the noise floor
        # for the same reason - below it, the AGC keeps adapting to noise.
        self.agc_max_gain = float(os.environ.get("BRIDGE_AGC_MAX_GAIN", "8.0"))
        self.agc_silence_threshold = int(os.environ.get("BRIDGE_AGC_SILENCE_THRESHOLD", "500"))

        # Talk/signaling side.
        self.ws_url = _require("BRIDGE_WS_URL")  # standalone signaling server, e.g. ws://127.0.0.1:8080/spreed
        self.internal_secret = _require("BRIDGE_INTERNAL_SECRET")
        self.backend_url = _require("BRIDGE_BACKEND_URL")  # e.g. https://nextcloud.example

        # Local HTTP control API (status/toggle) for the Nextcloud app to
        # call. Bind to an address reachable from the Nextcloud container
        # (e.g. the Docker bridge gateway), never 0.0.0.0.
        self.control_bind = os.environ.get("BRIDGE_CONTROL_BIND", "127.0.0.1")
        self.control_port = int(os.environ.get("BRIDGE_CONTROL_PORT", "8765"))

        # Directory for small per-line state files (currently just whether
        # a line's registration should be on) - lets the daemon resume
        # automatically after a crash/restart instead of silently staying
        # deregistered until someone notices and toggles it back on by
        # hand. Empty disables persistence entirely (falls back to today's
        # behavior: registration always starts off).
        # deploy/talk-sip-bridge.service provisions this via systemd's
        # StateDirectory=.
        # Talk's own door for telephony backends (its sip_bridge_shared_secret).
        # With it the bridge can ask Nextcloud to create a conversation for an
        # incoming call, in which the caller is a real participant - see
        # docs/SIGNALING-API.md, "Direct dial-in". Empty disables that and every
        # inbound call goes to the room its line names.
        self.sip_shared_secret = os.environ.get("BRIDGE_SIP_SHARED_SECRET", "")

        # How the phone shows up in the room, and what it costs:
        #
        #   phone  a virtual session flagged as a phone, without audio.
        #          Talk shows the caller's number, but its clients build no
        #          peer for it and their "waiting for someone" sound never
        #          stops - measured, every 15s, indefinitely on Android.
        #   audio  the same, flagged as carrying audio. Clients then look
        #          for a stream the session cannot have.
        #   none   no virtual session. Measured not to work: Talk's
        #          clients then have nothing to answer and refuse with "a
        #          call to yourself cannot be answered", so the caller
        #          rings until the gateway gives up. Kept because it is
        #          one line and says what was tried.
        self.phone_participant = os.environ.get("BRIDGE_PHONE_PARTICIPANT", "phone").strip().lower()
        if self.phone_participant not in ("phone", "audio", "none"):
            raise RuntimeError("BRIDGE_PHONE_PARTICIPANT must be phone, audio or none, "
                               f"not {self.phone_participant!r}")

        # Read key presses out of the audio as well as from RFC 4733
        # events. Needed wherever a gateway plays the tones instead of
        # passing the events on, which this deployment's does; harmless
        # where it does not, since a press reported twice is collapsed.
        self.inband_dtmf = os.environ.get("BRIDGE_INBAND_DTMF", "true").lower() != "false"
        # Log every key press at the point it is read, with the RTP
        # timestamp that decides whether it is a new press: which of the
        # three roads a gateway uses, and which presses never arrive, is
        # otherwise invisible - the call just misbehaves.
        self.dtmf_debug = os.environ.get("BRIDGE_DTMF_DEBUG", "false").strip().lower() == "true"
        # Log every signaling message this bridge does not act on. A room
        # in a call produces a steady stream of them - mute, unmute and
        # nick changes from every client, addressed to the phone - so
        # this is off in normal operation and buries the journal when on.
        self.signaling_debug = (
            os.environ.get("BRIDGE_SIGNALING_DEBUG", "false").strip().lower() == "true")

        # A recording played to a caller on a conference number instead of
        # the beeps that otherwise ask for a meeting id - 16-bit WAV, any
        # sample rate, mono or stereo. Words say what beeps cannot ("enter
        # the meeting ID, then hash"), and which words depends on the
        # deployment's language, so there is nothing sensible to ship.
        self.ivr_prompt_wav = os.environ.get("BRIDGE_IVR_PROMPT_WAV", "")
        self.ivr_pin_prompt_wav = os.environ.get("BRIDGE_IVR_PIN_PROMPT_WAV", "")
        # How patient the dialogue is, in seconds.
        #
        # Before the first key a caller is reading a number off an email,
        # and on a mobile client they first have to switch the speaker on
        # and open the keypad - measured against that, 25s is not
        # generous. Between keys it only has to outlast looking back at
        # the email mid-number. The gap is how long each prompt is given
        # before the next language is played.
        self.ivr_first_digit_timeout = _seconds("BRIDGE_IVR_FIRST_DIGIT_TIMEOUT", 25.0)
        self.ivr_next_digit_timeout = _seconds("BRIDGE_IVR_NEXT_DIGIT_TIMEOUT", 6.0)
        self.ivr_prompt_gap = _seconds("BRIDGE_IVR_PROMPT_GAP", 5.0)
        self.state_dir = os.environ.get("BRIDGE_STATE_DIR", "/var/lib/talk-sip-bridge")


config = Config()
