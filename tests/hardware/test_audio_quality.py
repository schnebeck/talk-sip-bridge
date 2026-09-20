#!/usr/bin/env python3
"""Measures what the publish path does to phone-side audio, objectively.

test_publish_and_verify.py answers "does audio arrive at all" with a loud
steady tone. That says nothing about intelligibility, because the two things
that ruin a real call - gain pumping between words, and distortion of a
quiet signal - need a signal shaped like speech to show up: bursts with
pauses, at the levels a real handset actually delivers.

The signal is a multi-tone burst (a few voice-band frequencies at once)
alternating with a low noise floor, fed through a real RtpSession in the
negotiated codec, through SipAudioTrack (AGC, resampling) and the WebRTC
publish, and recorded back from a subscriber. Reported per burst:

  level      peak of the burst as published - should be the same for every
             burst; a spread means the gain is pumping
  in-band    share of energy at the signal's own frequencies - the rest is
             distortion the chain added
  noise gap  level published during the pauses - a chain that amplifies the
             noise floor lifts this towards the bursts, which is what a
             listener hears as a bubbling background

Usage: test_audio_quality.py <roomid> [g722|pcmu] [bursts]
"""

import os
import pathlib
import sys

# The daemon's modules are installed separately from these scripts.
sys.path.insert(0, os.environ.get("BRIDGE_CODE")
                or str(pathlib.Path(__file__).resolve().parent.parent.parent / "bridge"))

from call import INBOUND, Call
import asyncio
import json
import sys
import threading
import time
import wave

import numpy as np
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription

from config import config
from rtp import RtpSession, PT_G722, PT_PCMU
from talk_client import TalkClient
from test_publish_and_verify import internal_hello, parse_candidate, to_mono

LOOPBACK_IP = "127.0.0.1"
SENDER_PORT = 45210
RECEIVER_PORT = 45211

# Levels measured on a real DECT call through this bridge: speech peaked
# between 2000 and 4000, the line's own noise floor sat at 150-250.
BURST_PEAK = 3000
NOISE_PEAK = 200
BURST_SECONDS = 0.6
PAUSE_SECONDS = 0.4
VOICE_TONES = (350, 700, 1400, 2100)

# One RTP packet every 20ms, in both codecs this bridge negotiates. Any
# per-packet step in the chain modulates the audio at this rate.
PACKET_RATE = 50
# Where "audible" sits: sidebands at -30dB are about 6% modulation and
# plainly heard on a steady tone, -40dB about 2% and not. The chain's own
# floor, measured over the loopback with everything working, is near
# -48dB; per-packet resampling put it at -22 to -33dB.
SIDEBAND_LIMIT = -40.0


def build_signal(sample_rate: int, bursts: int) -> np.ndarray:
    """Bursts of stacked voice-band tones separated by a noise floor."""
    out = []
    rng = np.random.default_rng(1)
    for _ in range(bursts):
        n = int(sample_rate * BURST_SECONDS)
        t = np.arange(n) / sample_rate
        wave_sum = sum(np.sin(2 * np.pi * f * t) for f in VOICE_TONES)
        wave_sum = wave_sum / np.abs(wave_sum).max() * BURST_PEAK
        out.append(wave_sum.astype(np.int16))
        pause = rng.normal(0, NOISE_PEAK / 3, int(sample_rate * PAUSE_SECONDS))
        out.append(np.clip(pause, -NOISE_PEAK, NOISE_PEAK).astype(np.int16))
    return np.concatenate(out)


def send_signal(sender: RtpSession, signal: np.ndarray):
    """Paced in real time, like a phone would deliver it."""
    spp = sender.samples_per_packet
    for i in range(0, len(signal) - spp, spp):
        sender.send_pcm(signal[i:i + spp])
        time.sleep(spp / sender.sample_rate)


async def subscribe_and_record(publisher_sessionid: str, seconds: float) -> tuple:
    async with websockets.connect(config.ws_url) as ws:
        await internal_hello(ws, config.internal_secret, config.backend_url)
        pc = RTCPeerConnection()
        frames = []
        rate = [48000]
        done = asyncio.Event()

        @pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                asyncio.ensure_future(collect(track))

        async def collect(track):
            try:
                while sum(len(f) for f in frames) < rate[0] * seconds:
                    frame = await track.recv()
                    rate[0] = frame.sample_rate
                    frames.append(to_mono(frame))
            except Exception:
                pass
            done.set()

        deadline = asyncio.get_event_loop().time() + seconds + 25
        offer_seen = False
        next_request = 0.0
        while asyncio.get_event_loop().time() < deadline and not done.is_set():
            if not offer_seen and asyncio.get_event_loop().time() >= next_request:
                await ws.send(json.dumps({
                    "id": "quality-reqoffer", "type": "message",
                    "message": {"recipient": {"type": "session", "sessionid": publisher_sessionid},
                                "data": {"type": "requestoffer", "roomType": "video"}}}))
                next_request = asyncio.get_event_loop().time() + 3
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("type") != "message":
                continue
            data = msg.get("message", {}).get("data", {})
            if data.get("from") != publisher_sessionid:
                continue
            if data.get("type") == "offer" and not offer_seen:
                offer_seen = True
                await pc.setRemoteDescription(RTCSessionDescription(sdp=data["payload"]["sdp"], type="offer"))
                await pc.setLocalDescription(await pc.createAnswer())
                await ws.send(json.dumps({
                    "id": "quality-answer", "type": "message",
                    "message": {"recipient": {"type": "session", "sessionid": publisher_sessionid},
                                "data": {"to": publisher_sessionid, "type": "answer", "sid": data.get("sid"),
                                         "roomType": "video",
                                         "payload": {"type": "answer", "sdp": pc.localDescription.sdp}}}}))
            elif data.get("type") == "candidate":
                cand = data.get("payload", {}).get("candidate", {})
                if cand.get("candidate"):
                    try:
                        await pc.addIceCandidate(parse_candidate(
                            cand["candidate"], sdpMid=cand.get("sdpMid"),
                            sdpMLineIndex=cand.get("sdpMLineIndex", 0)))
                    except Exception:
                        pass
        await pc.close()
        return (np.concatenate(frames) if frames else np.array([])), rate[0]


def longest_burst(pcm: np.ndarray, envelope: np.ndarray, window: int) -> np.ndarray:
    """The longest stretch of signal, without its edges. Sidebands are
    measured inside one burst rather than across the recording: the gaps
    between bursts are themselves a modulation, and would be measured
    instead of the one being looked for."""
    loud = envelope > envelope.max() * 0.35
    best_start = best_length = run_start = run = 0
    for i, is_loud in enumerate(loud):
        if is_loud:
            if run == 0:
                run_start = i
            run += 1
            if run > best_length:
                best_start, best_length = run_start, run
        else:
            run = 0
    if best_length < 4:
        return np.array([])
    return pcm[(best_start + 1) * window:(best_start + best_length - 1) * window]


def packet_rate_sidebands(pcm: np.ndarray, sample_rate: int) -> float:
    """The worst sideband at +-PACKET_RATE around any of the signal's own
    tones, in dB relative to that tone.

    This is the measurement that catches anything happening once per
    packet: a resampler that restarts at every packet boundary, a gain
    step, a packet padded out with silence. All of them put a matched
    pair of sidebands at the packet rate on every steady tone, heard as a
    low ringing, and none of them move the level, the in-band share or the
    noise floor enough for the other numbers here to notice."""
    if pcm.size < sample_rate * 0.2:
        return -99.0
    spectrum = np.abs(np.fft.rfft(pcm * np.hanning(len(pcm))))
    freqs = np.fft.rfftfreq(len(pcm), 1.0 / sample_rate)

    def level(centre):
        band = np.abs(freqs - centre) < 8
        return spectrum[band].max() if band.any() else 0.0

    worst = -99.0
    for tone in VOICE_TONES:
        carrier = level(tone)
        if carrier <= 0:
            continue
        for offset in (-PACKET_RATE, PACKET_RATE, -2 * PACKET_RATE, 2 * PACKET_RATE):
            sideband = level(tone + offset)
            if sideband > 0:
                worst = max(worst, 20 * np.log10(sideband / carrier))
    return worst


def report(pcm: np.ndarray, sample_rate: int):
    if len(pcm) == 0:
        print("RESULT: FAILED - nothing recorded")
        return False
    window = int(sample_rate * 0.05)
    envelope = np.array([np.abs(pcm[i:i + window]).max()
                         for i in range(0, len(pcm) - window, window)])
    loud = envelope[envelope > envelope.max() * 0.35]
    quiet = envelope[envelope < envelope.max() * 0.15]

    spec = np.abs(np.fft.rfft(pcm * np.hanning(len(pcm))))
    freqs = np.fft.rfftfreq(len(pcm), 1.0 / sample_rate)
    in_band = 0.0
    for f in VOICE_TONES:
        sel = np.abs(freqs - f) < 25
        in_band += float((spec[sel] ** 2).sum())
    voice_range = (freqs > 100) & (freqs < 4000)
    total = float((spec[voice_range] ** 2).sum())

    print(f"recorded {len(pcm) / sample_rate:.1f}s at {sample_rate} Hz")
    print(f"burst level   : mean {loud.mean():7.0f}  spread {loud.std() / max(loud.mean(), 1) * 100:5.1f}%"
          f"   (spread over ~15% means the gain is pumping)")
    print(f"pause level   : mean {quiet.mean():7.0f}"
          f"   ({quiet.mean() / max(loud.mean(), 1) * 100:4.1f}% of burst level - higher means"
          f" the noise floor is being amplified)")
    print(f"in-band energy: {in_band / max(total, 1) * 100:5.1f}% of the voice band"
          f"   (the rest is distortion this chain added)")
    sidebands = packet_rate_sidebands(longest_burst(pcm, envelope, window), sample_rate)
    print(f"packet-rate AM: {sidebands:5.1f} dB at +-{PACKET_RATE} Hz around the tones"
          f"   (above {SIDEBAND_LIMIT} dB is a ringing a listener hears)")
    return sidebands <= SIDEBAND_LIMIT


async def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <roomid> [g722|pcmu] [bursts]")
        sys.exit(1)
    roomid = sys.argv[1]
    codec = PT_G722 if (len(sys.argv) < 3 or sys.argv[2].lower() == "g722") else PT_PCMU
    bursts = int(sys.argv[3]) if len(sys.argv) > 3 else 8

    receiver = RtpSession(LOOPBACK_IP, RECEIVER_PORT, LOOPBACK_IP, SENDER_PORT, payload_type=codec)
    sender = RtpSession(LOOPBACK_IP, SENDER_PORT, LOOPBACK_IP, RECEIVER_PORT, payload_type=codec)
    signal = build_signal(sender.sample_rate, bursts)
    duration = len(signal) / sender.sample_rate
    print(f"codec {'G.722' if codec == PT_G722 else 'PCMU'} at {sender.sample_rate} Hz, "
          f"{bursts} bursts, {duration:.1f}s, burst peak {BURST_PEAK}, noise floor {NOISE_PEAK}")

    client = TalkClient(call_manager=None)
    async with websockets.connect(config.ws_url) as ws:
        client.ws = ws
        client.loop = asyncio.get_event_loop()
        await client._hello()
        loop_task = asyncio.ensure_future(client._message_loop())
        client._call_sessions["quality-test"] = Call(sip_call_id="quality-test", kind=INBOUND, number="quality-test")
        publish = asyncio.ensure_future(
            client._publish_call_audio("quality-test", receiver, roomid, "quality-test"))
        await asyncio.sleep(2)
        threading.Thread(target=send_signal, args=(sender, signal), daemon=True).start()
        pcm, rate = await subscribe_and_record(client.own_sessionid, duration - 2)
        ok = report(pcm, rate)
        if len(pcm):
            with wave.open("/tmp/bridge_published_audio.wav", "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(np.clip(pcm, -32768, 32767).astype(np.int16).tobytes())
            print("recording written to /tmp/bridge_published_audio.wav")
        await client._teardown_call("quality-test", roomid)
        loop_task.cancel()
        publish.cancel()
    sender.close()
    receiver.close()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
