#!/usr/bin/env python3
"""Verifies TalkClient's audio-publish path (room join, self-addressed
WebRTC offer, addsession) without any SIP call or gateway involved: a local
RTP loopback feeds a test tone into _publish_call_audio(), and a second
internal client in the same process subscribes and checks it via FFT. Both
run in the same process with no gap between publish and subscribe - Janus
closes an unsubscribed publisher's connection after a short idle period, so
driving this as two separately started scripts gives a false negative.

Usage: python3 test_publish_and_verify.py <roomid> [tone-hz] [duration-s]
"""
import asyncio
import hashlib
import hmac
import json
import secrets
import sys
import threading
import time

import numpy as np
import websockets
from aiortc import RTCIceCandidate, RTCPeerConnection, RTCSessionDescription

from config import config
from rtp import RtpSession
from talk_client import TalkClient

LOOPBACK_IP = "127.0.0.1"
SENDER_PORT = 45200
RECEIVER_PORT = 45201
REQUEST_RETRY_INTERVAL = 3


def send_tone_loop(sender, freq, duration):
    sample_rate = 8000
    t0 = time.time()
    sent_samples = 0
    while time.time() - t0 < duration:
        n = 160
        t = (np.arange(n) + sent_samples) / sample_rate
        wave = (np.sin(2 * np.pi * freq * t) * 20000).astype(np.int16)
        sender.send_pcm(wave)
        sent_samples += n
        time.sleep(n / sample_rate)


def parse_candidate(cand_str, sdpMid=None, sdpMLineIndex=0):
    parts = cand_str.replace("candidate:", "").split()
    return RTCIceCandidate(
        component=int(parts[1]), foundation=parts[0], ip=parts[4], port=int(parts[5]),
        priority=int(parts[3]), protocol=parts[2], type=parts[7],
        sdpMid=sdpMid, sdpMLineIndex=sdpMLineIndex,
    )


def to_mono(frame):
    arr = frame.to_ndarray()
    channels = len(frame.layout.channels) if frame.layout else 1
    if arr.ndim == 2 and arr.shape[0] > 1:
        return arr.astype(np.float64).mean(axis=0)
    row = arr.flatten()
    if channels > 1:
        return row.reshape(-1, channels).astype(np.float64).mean(axis=1)
    return row.astype(np.float64)


async def internal_hello(ws, secret, backend_url):
    random_str = secrets.token_hex(32)
    token = hmac.new(secret.encode(), random_str.encode(), hashlib.sha256).hexdigest()
    await ws.send(json.dumps({
        "id": "verify-hello", "type": "hello",
        "hello": {"version": "1.0", "auth": {"type": "internal", "params": {"random": random_str, "token": token, "backend": backend_url}}},
    }))
    await ws.recv()
    resp = json.loads(await ws.recv())
    return resp["hello"]["sessionid"]


async def verify(publisher_sessionid, expected_freq, wait_seconds, result_holder):
    async with websockets.connect(config.ws_url) as ws:
        await internal_hello(ws, config.internal_secret, config.backend_url)
        await ws.send(json.dumps({
            "id": "verify-reqoffer", "type": "message",
            "message": {"recipient": {"type": "session", "sessionid": publisher_sessionid},
                        "data": {"type": "requestoffer", "roomType": "video"}},
        }))
        pc = RTCPeerConnection()
        samples = []
        got_audio = asyncio.Event()
        offer_sid = None

        @pc.on("track")
        def on_track(track):
            print(f"[verify] Track received: {track.kind}")
            if track.kind == "audio":
                asyncio.ensure_future(collect_audio(track))

        @pc.on("iceconnectionstatechange")
        async def on_ice():
            print(f"[verify] ICE state: {pc.iceConnectionState}")

        actual_sample_rate = [48000]

        async def collect_audio(track):
            try:
                first = True
                while sum(len(s) for s in samples) < 48000 * 2:
                    frame = await track.recv()
                    if first:
                        actual_sample_rate[0] = frame.sample_rate
                        first = False
                    samples.append(to_mono(frame))
                got_audio.set()
            except Exception as e:
                print(f"[verify] Audio receive ended ({e!r})")
                got_audio.set()

        deadline = asyncio.get_event_loop().time() + wait_seconds
        next_request = asyncio.get_event_loop().time() + REQUEST_RETRY_INTERVAL
        while asyncio.get_event_loop().time() < deadline and not got_audio.is_set():
            # The publisher does not exist yet while the bridge is still
            # gathering ICE, and the signaling server rejects a request for
            # a missing publisher instead of queuing it - so keep asking
            # until an offer actually arrives.
            if offer_sid is None and asyncio.get_event_loop().time() >= next_request:
                await ws.send(json.dumps({
                    "id": "verify-reqoffer", "type": "message",
                    "message": {"recipient": {"type": "session", "sessionid": publisher_sessionid},
                                "data": {"type": "requestoffer", "roomType": "video"}},
                }))
                print("[verify] No offer yet, requesting again.")
                next_request = asyncio.get_event_loop().time() + REQUEST_RETRY_INTERVAL
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("type") != "message":
                continue
            data = msg.get("message", {}).get("data", {})
            if data.get("type") == "offer" and data.get("from") == publisher_sessionid:
                offer_sid = data.get("sid")
                print(f"[verify] Offer received (sid={offer_sid}).")
                await pc.setRemoteDescription(RTCSessionDescription(sdp=data["payload"]["sdp"], type="offer"))
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send(json.dumps({
                    "id": "verify-answer", "type": "message",
                    "message": {"recipient": {"type": "session", "sessionid": publisher_sessionid},
                                "data": {"to": publisher_sessionid, "type": "answer", "sid": offer_sid, "roomType": "video",
                                         "payload": {"type": "answer", "sdp": pc.localDescription.sdp}}},
                }))
                print("[verify] Answer sent.")
            elif data.get("type") == "candidate" and data.get("from") == publisher_sessionid:
                cand_data = data.get("payload", {}).get("candidate", {})
                cand_str = cand_data.get("candidate", "")
                if cand_str:
                    try:
                        await pc.addIceCandidate(parse_candidate(cand_str, sdpMid=cand_data.get("sdpMid"), sdpMLineIndex=cand_data.get("sdpMLineIndex", 0)))
                    except Exception as e:
                        print(f"[verify] Could not add candidate: {e!r}")

        if not offer_sid or not got_audio.is_set() or not samples:
            print(f"[verify] FAILED: offer_sid={offer_sid}, got_audio={got_audio.is_set()}, samples={len(samples)}")
            await pc.close()
            result_holder["success"] = False
            return

        await pc.close()
        pcm = np.concatenate(samples).astype(np.float64)
        sample_rate = actual_sample_rate[0]
        windowed = pcm * np.hanning(len(pcm))
        spectrum = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(len(windowed), d=1.0 / sample_rate)
        peak_freq = freqs[np.argmax(spectrum)]
        print(f"[verify] Dominant frequency: {peak_freq:.1f} Hz (expected {expected_freq} Hz)")
        result_holder["success"] = abs(peak_freq - expected_freq) < 15


async def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <roomid> [tone-hz] [duration-s]")
        sys.exit(1)
    roomid = sys.argv[1]
    freq = float(sys.argv[2]) if len(sys.argv) > 2 else 660.0
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0
    if duration < 15:
        # Subscribing can need a retry before the publisher exists, and two
        # seconds of audio have to be collected after that - a shorter run
        # reports a failure that is only impatience.
        print(f"Raising duration from {duration}s to the 15s this test needs.")
        duration = 15.0

    receiver = RtpSession(LOOPBACK_IP, RECEIVER_PORT, LOOPBACK_IP, SENDER_PORT)
    sender = RtpSession(LOOPBACK_IP, SENDER_PORT, LOOPBACK_IP, RECEIVER_PORT)
    sender_thread = threading.Thread(target=send_tone_loop, args=(sender, freq, duration), daemon=True)
    sender_thread.start()

    client = TalkClient(call_manager=None)
    async with websockets.connect(config.ws_url) as ws:
        client.ws = ws
        client.loop = asyncio.get_event_loop()
        await client._hello()
        message_loop_task = asyncio.ensure_future(client._message_loop())

        # Register the call the way on_incoming_call does: publishing stops
        # as soon as this entry is gone, which is how a torn-down call
        # aborts a publish that is still gathering ICE.
        client._call_sessions["test-call-1"] = {"kind": "test", "number": "loopback-test"}
        publish_task = asyncio.ensure_future(client._publish_call_audio("test-call-1", receiver, roomid, "loopback-test"))
        await asyncio.sleep(1.5)  # a head start; verify() keeps re-requesting until the publisher exists

        result = {"success": False}
        await verify(client.own_sessionid, freq, duration, result)

        await client._teardown_call("test-call-1", roomid)
        message_loop_task.cancel()

    sender.close()
    receiver.close()
    print("RESULT:", "SUCCESS" if result["success"] else "FAILED")
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
