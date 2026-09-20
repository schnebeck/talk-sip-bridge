#!/usr/bin/env python3
"""Calls a person in Talk and measures both directions of the call.

Every other script here either answers its own call or measures a signal
it generated itself. This one puts a person in the middle, which is what
a deployment is actually for.

The second SIP account calls the bridge's own extension through the
gateway, so both directions run the deployed path. Three measurements come
out of one call:

  phone -> Talk   tones are played down the SIP leg and recorded again as
                  a Talk client hears them, by subscribing to what the
                  bridge publishes - frequency and distortion, not just
                  "something arrived"
  Talk -> phone   the person speaks, and what reaches the SIP leg is
                  measured here
  by ear          the person also hears the tones, which is the one part
                  no measurement covers: everything after Janus

The calling line needs a SIP pipe and a media relay pipe of its own - the
bridge's line is holding the other two for this very call. On the relay:

    SIP_PIPE_ROUTES=10.1.1.5:5170->192.168.1.1:5060,192.168.1.10:5170->10.1.1.1:5093 \
        python3 -u /opt/sip-pipe/sip_pipe.py

and RTP_RELAY_PIPES must contain 40010:40011, which it does.

Both recordings are written out as WAV files (OUT_WAV, TALK_WAV) - a
measurement says a tone arrived, the file says what it sounds like.

Usage: SIP_PHONE2_PASS=... test_human_call.py [extension]
"""

import os
import pathlib
import sys

# The daemon's modules are installed separately from these scripts.
sys.path.insert(0, os.environ.get("BRIDGE_CODE")
                or str(pathlib.Path(__file__).resolve().parent.parent.parent / "bridge"))

import asyncio
import threading
import time
import wave

import numpy as np

ANSWER_TIMEOUT = 120      # a person needs longer than a script

# What actually cuts a ring short is the outbound call's own timeout, not
# how long this script is willing to wait - and its default is made for a
# machine answering. Raised before config reads it, or the gateway gets a
# CANCEL while the person is still reaching for the browser.
os.environ.setdefault("BRIDGE_OUTBOUND_CALL_TIMEOUT", str(ANSWER_TIMEOUT + 20))

from config import config, LineConfig
from sip_call import CallManager
from sip_registrar import SipRegistrar
from sip_transport import SipTransport
# Subscribing to a publisher and finding the bridge's own session are the
# same job here as in the scripts that already do them.
from test_audio_quality import SIDEBAND_LIMIT, subscribe_and_record
from test_audio_over_sip import find_bridge_session

EXTENSION = sys.argv[1] if len(sys.argv) > 1 else "**621"
OUT_WAV = os.environ.get("OUT_WAV", f"/tmp/human-call-{int(time.time())}.wav")
TALK_WAV = os.environ.get("TALK_WAV", OUT_WAV.replace(".wav", "-as-talk-hears-it.wav"))

# The calling line's own half of the relay, alongside the deployed line's.
CALLER_SIP_PORT = 5093
CALLER_RTP_PORT = 41000
CALLER_PROXY_PORT = 5170
CALLER_CONTACT_PORT = 5170
CALLER_RELAY_LAN_PORT = 40010
CALLER_RELAY_OVERLAY_PORT = 40011

# Long enough that the steady part dominates: the gain control needs a
# couple of hundred milliseconds to settle on each new level, and a tone
# short enough to be mostly that transient measures the settling, not the
# path. Same amplitude for all three, so it has less to settle for.
TONE_SECONDS = 1.2
TONE_SETTLING = 0.35      # skipped before measuring a tone
TONE_GAP = 0.3
TONE_ROUNDS = 3
TONES = (440, 660, 880)
LISTEN_SECONDS = 8        # how long the far end is recorded afterwards

# Subscribing to the bridge's publisher takes a few seconds - the offer is
# requested again until the publisher exists - so the tones start after a
# head start and the recording runs past their end.
TALK_HEAD_START = 5
TALK_RECORD_SECONDS = 18


def build_caller() -> LineConfig:
    """Built from the environment like any other line, rather than by
    filling in attributes by hand: a line assembled field by field is one
    field behind the next time LineConfig grows one."""
    password = os.environ.get("SIP_PHONE2_PASS")
    if not password:
        raise SystemExit("SIP_PHONE2_PASS is required - the second account's password.")
    deployed = config.lines[0]
    env = {
        "SIP_USER": "sip-phone2",
        "SIP_PASS": password,
        "GATEWAY_HOST": deployed.gateway_host,
        "PROXY_HOST": deployed.proxy_host,
        "PROXY_PORT": str(CALLER_PROXY_PORT),
        "SIP_TRANSPORT": deployed.sip_transport,
        "CONTACT_HOST": deployed.contact_host,
        "CONTACT_PORT": str(CALLER_CONTACT_PORT),
        "CONTACT_TRANSPORT": deployed.contact_transport,
        "LOCAL_SIP_PORT": str(CALLER_SIP_PORT),
        "LOCAL_RTP_PORT": str(CALLER_RTP_PORT),
        "RELAY_LAN_HOST": deployed.relay_lan_host,
        "RELAY_LAN_PORT": str(CALLER_RELAY_LAN_PORT),
        "RELAY_OVERLAY_HOST": deployed.relay_overlay_host,
        "RELAY_OVERLAY_PORT": str(CALLER_RELAY_OVERLAY_PORT),
    }
    for key, value in env.items():
        os.environ["TESTPEER_" + key] = value
    return LineConfig("test_caller", deployed.local_ip, env_prefix="TESTPEER_")


connected = threading.Event()
rtp_holder = {}


def on_connected(*, call_id, direction, rtp):
    print(f"[test] answered - codec payload type {rtp.payload_type} at {rtp.sample_rate} Hz", flush=True)
    rtp_holder["rtp"] = rtp
    connected.set()


def tone_sequence(rate: int) -> np.ndarray:
    """A rising three-tone figure, repeated. Recognisable by ear even
    through a codec, and unlike speech it cannot be mistaken for echo of
    the listener's own room."""
    parts = []
    gap = np.zeros(int(rate * TONE_GAP), dtype=np.int16)
    for _ in range(TONE_ROUNDS):
        for frequency in TONES:
            t = np.arange(int(rate * TONE_SECONDS)) / rate
            envelope = np.minimum(1.0, np.minimum(t, TONE_SECONDS - t) * 50)  # no clicks
            parts.append((0.3 * envelope * np.sin(2 * np.pi * frequency * t) * 32767).astype(np.int16))
            parts.append(gap)
    return np.concatenate(parts)


def play(rtp, signal):
    """Paced against a fixed schedule, not by sleeping between packets.

    Sleeping for one packet interval after each send adds the time the
    send itself took, which measured 20.77ms per 20ms packet - 3.8% slow.
    A gateway re-clocks the stream to exactly 50 packets a second and has
    to fill in the difference, and that filling is heard as chopping. A
    real handset has a hardware clock and this problem does not exist;
    a test that creates it measures itself."""
    spp = rtp.samples_per_packet
    interval = spp / rtp.sample_rate
    due = time.monotonic()
    for i in range(0, len(signal) - spp, spp):
        rtp.send_pcm(signal[i:i + spp])
        due += interval
        remaining = due - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)


def record(rtp, seconds: float) -> np.ndarray:
    """Whatever Talk sends back, as the caller's own RTP session decodes it."""
    chunks = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            chunks.append(rtp.recv_queue.get(timeout=0.5))
        except Exception:
            continue
    return np.concatenate(chunks) if chunks else np.array([], dtype=np.int16)


def sidebands(window: np.ndarray, rate: int, offset: float = 50.0) -> float:
    """Level at f0 +- offset relative to f0, the signature of amplitude
    modulation. 50 Hz is the packet rate: anything that happens once per
    packet - a resampler restarting, a gain step, a padded packet - shows
    up here as a matched pair, and is heard as a low ringing on a tone."""
    spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window))))
    freqs = np.fft.rfftfreq(len(window), 1 / rate)
    f0 = freqs[np.argmax(spectrum)]
    pair = []
    for side in (-offset, offset):
        band = np.abs(freqs - (f0 + side)) < 8
        pair.append(20 * np.log10(spectrum[band].max() / spectrum.max()))
    return max(pair)


def dominant(window: np.ndarray, rate: int) -> tuple:
    """The strongest frequency in a window, and how much energy sits
    anywhere else. A tone that arrives clean measures well under -20 dB
    here; anything above that is roughness a listener can hear."""
    spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window))))
    freqs = np.fft.rfftfreq(len(window), 1 / rate)
    peak = freqs[np.argmax(spectrum)]
    band = np.abs(freqs - peak) < 25
    fundamental = np.sqrt(np.sum(spectrum[band] ** 2))
    rest = np.sqrt(np.sum(spectrum[~band] ** 2))
    return peak, 20 * np.log10(rest / fundamental) if fundamental > 0 else 0.0


def steady_parts(pcm: np.ndarray, rate: int):
    """The settled middle of each tone. Found from the recording's own
    envelope rather than from a fixed grid: a window laid over a tone's
    edge measures the edge. The first TONE_SETTLING of each tone is left
    out for the same reason - that stretch is the gain control finding its
    level, not the path's doing."""
    block = int(rate * 0.02)
    envelope = np.sqrt(np.convolve(pcm.astype(np.float64) ** 2,
                                   np.ones(block) / block, mode="same"))
    loud = envelope > envelope.max() * 0.25
    edges = np.diff(loud.astype(int))
    starts = list(np.where(edges == 1)[0])
    ends = list(np.where(edges == -1)[0])
    if loud[0]:
        starts.insert(0, 0)
    if loud[-1]:
        ends.append(len(loud) - 1)
    for start, end in zip(starts, ends):
        core = pcm[start + int(rate * TONE_SETTLING):end - int(rate * 0.05)]
        if core.size > rate * 0.2:
            yield core.astype(np.float64), envelope[start:end]


def report_tones(pcm: np.ndarray, rate: int) -> bool:
    """What a Talk client actually received, tone by tone. The ear says
    whether audio arrives; this says at which frequency, how clean, and
    how steady."""
    if pcm.size == 0:
        print("\n[test] FAIL  the Talk side received nothing at all")
        return False
    print(f"\n[test] a Talk client received {pcm.size / rate:.1f}s at {rate} Hz")
    found = {}
    for core, envelope in steady_parts(pcm, rate):
        peak, distortion = dominant(core, rate)
        steady = envelope[int(rate * TONE_SETTLING):]
        swing = (20 * np.log10(steady.max() / max(steady.min(), 1e-9))
                 if steady.size else 0.0)
        for expected in TONES:
            if abs(peak - expected) < 25:
                found.setdefault(expected, []).append(
                    (peak, distortion, swing, sidebands(core, rate)))
    worst_sideband = -99.0
    for expected in TONES:
        hits = found.get(expected, [])
        if not hits:
            print(f"       {expected:4d} Hz  not found in what Talk received")
            continue
        peaks = [p for p, _, _, _ in hits]
        sideband = max(s for _, _, _, s in hits)
        worst_sideband = max(worst_sideband, sideband)
        print(f"       {expected:4d} Hz  arrived as {np.mean(peaks):6.1f} Hz in "
              f"{len(hits)} tones, worst distortion {max(d for _, d, _, _ in hits):5.1f} dB, "
              f"level moves up to {max(s for _, _, s, _ in hits):4.1f} dB while steady, "
              f"50 Hz sidebands {sideband:5.1f} dB")
    if len(found) != len(TONES):
        print(f"[test] FAIL  only {len(found)} of {len(TONES)} tones reached the Talk side")
        return False
    print("[test] PASS  every tone reached the Talk side at its own frequency")
    if worst_sideband > SIDEBAND_LIMIT:
        print(f"[test] FAIL  amplitude modulation at the packet rate: sidebands at "
              f"{worst_sideband:.1f} dB, where a clean stream has none")
        return False
    print("[test] PASS  no modulation at the packet rate")
    return True


def report(pcm: np.ndarray, rate: int) -> bool:
    if pcm.size == 0:
        print("\n[test] FAIL  nothing came back from Talk at all")
        return False
    seconds = pcm.size / rate
    print(f"\n[test] {seconds:.1f}s of audio came back from Talk ({pcm.size} samples at {rate} Hz)")
    print("[test] level per second:")
    loud = 0
    for second in range(int(seconds)):
        window = pcm[second * rate:(second + 1) * rate].astype(np.float64)
        rms = np.sqrt(np.mean(window ** 2)) if window.size else 0.0
        dbfs = 20 * np.log10(rms / 32768) if rms > 0 else -99
        bar = "#" * max(0, int((dbfs + 60) / 2))
        print(f"       {second:2d}s  {dbfs:6.1f} dBFS  {bar}")
        if dbfs > -45:
            loud += 1
    print(f"[test] {loud} of {int(seconds)} seconds carry signal above -45 dBFS")
    if loud == 0:
        print("[test] FAIL  the return direction is silent - Talk's audio is not reaching the phone")
        return False
    print("[test] PASS  audio is flowing from Talk back to the phone")
    return True


def write_wav(path: str, pcm: np.ndarray, rate: int, what: str):
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(np.asarray(pcm, dtype=np.int16).tobytes())
    print(f"[test] {what} is saved at {path}")


async def play_tones_and_listen_as_talk(rtp) -> tuple:
    """Plays the tones down the SIP leg while subscribing to what the
    bridge publishes, so the same tones are measured where a Talk client
    would hear them. Subscribing first: the recording has to be running
    before there is anything to record."""
    publisher = find_bridge_session()
    if publisher is None:
        print("[test] could not identify the bridge's publishing session - "
              "playing the tones anyway, measuring only the far end by ear")
    signal = tone_sequence(rtp.sample_rate)

    async def play_after_head_start():
        await asyncio.sleep(TALK_HEAD_START)
        print(f"[test] playing {TONE_ROUNDS} rounds of {TONES} Hz - you should hear them in Talk",
              flush=True)
        await asyncio.to_thread(play, rtp, signal)

    player = asyncio.ensure_future(play_after_head_start())
    if publisher is None:
        await player
        return np.array([], dtype=np.int16), 48000
    print(f"[test] subscribing to the bridge's publisher {publisher[:12]}... as a Talk client would",
          flush=True)
    pcm, rate = await subscribe_and_record(publisher, TALK_RECORD_SECONDS)
    await player
    return np.asarray(pcm), rate


async def main() -> int:
    line = build_caller()
    manager = CallManager(line, on_call_connected=on_connected)
    holder = {}
    registrar = SipRegistrar(lambda: holder["t"], line)
    holder["t"] = SipTransport(manager, line)
    manager.transport = holder["t"]

    try:
        if not registrar.turn_on():
            print("[test] could not register the calling account")
            return 1
        print(f"[test] registered as {line.sip_user}, calling {EXTENSION}", flush=True)
        result = manager.dial(EXTENSION)
        if result.get("error"):
            print(f"[test] {result['error']}")
            return 1

        print(f"[test] Talk should be ringing now - answer it IN A BROWSER, not in the "
              f"Android app, which joins the room but never gets a publisher "
              f"(waiting up to {ANSWER_TIMEOUT}s)", flush=True)
        if not await asyncio.to_thread(connected.wait, ANSWER_TIMEOUT):
            print("[test] nobody answered")
            return 1

        rtp = rtp_holder["rtp"]
        await asyncio.sleep(1)
        talk_pcm, talk_rate = await play_tones_and_listen_as_talk(rtp)
        tones_ok = report_tones(talk_pcm, talk_rate)
        if talk_pcm.size:
            write_wav(TALK_WAV, talk_pcm, talk_rate, "what a Talk client received")

        while not rtp.recv_queue.empty():   # drop the tones' own echo period
            rtp.recv_queue.get()
        print(f"\n[test] now say something into Talk - recording {LISTEN_SECONDS}s", flush=True)
        pcm = await asyncio.to_thread(record, rtp, LISTEN_SECONDS)
        back_ok = report(pcm, rtp.sample_rate)
        if pcm.size:
            write_wav(OUT_WAV, pcm, rtp.sample_rate, "what reached the phone")
        return 0 if (tones_ok and back_ok) else 1
    finally:
        try:
            manager.hangup()
        except Exception:
            pass
        registrar.turn_off()
        holder["t"].close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
