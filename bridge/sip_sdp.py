"""SDP for the two codecs this bridge implements: building an offer or an
answer, and reading back what the far end offered or answered.

G.722 is preferred over PCMU wherever there is a choice - real 16kHz audio,
despite SDP historically labeling it G722/8000.
"""
from payload_types import PT_G722, PT_PCMU


def _sdp_host_port(line, local_port: int) -> tuple[str, int]:
    """Advertises the relay's LAN-reachable address if a media relay is
    configured for this line (see LineConfig.media_relay_enabled) - the
    gateway can only deliver RTP to an address on its own LAN, same
    constraint as the SIP Contact header."""
    host = line.relay_lan_host if line.media_relay_enabled else line.local_ip
    port = line.relay_lan_port if line.media_relay_enabled else local_port
    return host, port


def offer_sdp(line, local_port: int) -> tuple[str, str, int]:
    """Returns (sdp, advertised_host, advertised_port). Offers G.722
    ("HD-Telefonie") first, PCMU as a fallback for gateways that don't
    support it - the far end picks one in its answer."""
    host, port = _sdp_host_port(line, local_port)
    sdp = (
        f"v=0\r\no=- 0 0 IN IP4 {host}\r\ns=-\r\n"
        f"c=IN IP4 {host}\r\nt=0 0\r\nm=audio {port} RTP/AVP {PT_G722} {PT_PCMU}\r\n"
        f"a=rtpmap:{PT_G722} G722/8000\r\na=rtpmap:{PT_PCMU} PCMU/8000\r\n"
    )
    return sdp, host, port


def answer_sdp(line, local_port: int, payload_type: int) -> tuple[str, str, int]:
    """Returns (sdp, advertised_host, advertised_port) for a single, already
    chosen codec - used when this line is the UAS answering an INVITE."""
    host, port = _sdp_host_port(line, local_port)
    name = "G722" if payload_type == PT_G722 else "PCMU"
    sdp = (
        f"v=0\r\no=- 0 0 IN IP4 {host}\r\ns=-\r\n"
        f"c=IN IP4 {host}\r\nt=0 0\r\nm=audio {port} RTP/AVP {payload_type}\r\n"
        f"a=rtpmap:{payload_type} {name}/8000\r\n"
    )
    return sdp, host, port


def extract_sip_body(text: str) -> str:
    parts = text.split("\r\n\r\n", 1)
    return parts[1] if len(parts) > 1 else ""


def parse_offered_payload_types(sdp_body: str) -> list[int]:
    for line in sdp_body.splitlines():
        line = line.strip()
        if line.startswith("m=audio"):
            fields = line.split()
            return [int(p) for p in fields[3:] if p.isdigit()]
    return []


def parse_sdp_media_address(sdp_body: str):
    """Where the peer wants its RTP sent, from an SDP offer or answer:
    ("c=IN IP4 <host>", "m=audio <port> ..."). Returns None if either is
    missing. Only needed on a line without a media relay - with one, RTP
    always goes to the relay regardless of what the peer advertises."""
    host = None
    port = None
    for line in sdp_body.splitlines():
        line = line.strip()
        if line.startswith("c=IN IP4 ") and host is None:
            host = line[len("c=IN IP4 "):].split("/")[0].strip()
        elif line.startswith("m=audio"):
            fields = line.split()
            if len(fields) > 1 and fields[1].isdigit():
                port = int(fields[1])
    return (host, port) if host and port else None


def choose_payload_type(offered: list[int]) -> int:
    """We're the UAS (answering an INVITE) - pick G.722 if the caller
    offered it, otherwise fall back to PCMU regardless of what else was
    offered (the only two codecs this bridge implements)."""
    return PT_G722 if PT_G722 in offered else PT_PCMU
