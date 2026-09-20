"""Audio between the phone call's RTP session and Talk's WebRTC side.

One direction is a track aiortc pulls frames from; the other is plain
resampling, applied to frames the subscriber hands over. Both ends run at
whatever rate the SIP call negotiated, and Talk always at 48kHz.
"""
import asyncio
import fractions
import time

import numpy as np
from aiortc.mediastreams import AudioStreamTrack
from av import AudioFrame

from agc import Agc
from config import config

AUDIO_SAMPLE_RATE = 48000
RTP_QUEUE_POLL_INTERVAL = 0.02  # one 20ms RTP packet - how long a frame waits for late audio
MAX_QUEUED_PACKETS = 5  # 100ms of jitter cushion; older packets are only latency


def resample_linear(pcm: np.ndarray, in_rate: int, out_rate: int) -> np.ndarray:
    """Linear interpolation, not sample repetition/decimation - repetition
    produces a staircase waveform (harsh, aliased); this is a large audible
    improvement for a small amount of code, without adding a dependency for
    full sinc-based resampling. Used in both directions: SIP audio (8/16kHz)
    up to Talk's 48kHz, and Talk's audio back down to the SIP call's rate."""
    if in_rate == out_rate:
        return pcm
    n_in = len(pcm)
    if n_in == 0:
        return pcm
    n_out = max(1, round(n_in * out_rate / in_rate))
    x_out = np.linspace(0, n_in - 1, n_out)
    return np.interp(x_out, np.arange(n_in), pcm).astype(np.int16)


class StreamResampler:
    """Resamples a stream that arrives in pieces, as a call's audio does.

    resample_linear() converts one array on its own, which is right for a
    recording and wrong for a stream: it maps each piece onto the whole
    output, so the interpolation restarts at every boundary. On 20ms
    packets that is a phase step 50 times a second, which lands on a tone
    as sidebands at +-50Hz and its multiples - measurably, at about -35dB,
    and audibly as a low ringing on anything steady. Carrying the last
    sample and the fractional position across the boundary removes it.
    """

    def __init__(self, in_rate: int, out_rate: int):
        self.in_rate = in_rate
        self.out_rate = out_rate
        self.step = in_rate / out_rate   # input samples per output sample
        self.previous = None             # last input sample of the piece before
        self.position = 0.0              # next output sample, in input samples

    def process(self, pcm: np.ndarray) -> np.ndarray:
        if self.in_rate == self.out_rate or len(pcm) == 0:
            return pcm
        if self.previous is None:
            known = pcm.astype(np.float64)
        else:
            known = np.concatenate(([self.previous], pcm)).astype(np.float64)
        last = len(known) - 1
        count = int(np.floor((last - self.position) / self.step)) + 1
        self.previous = pcm[-1]
        if count <= 0:
            self.position -= last
            return np.zeros(0, dtype=np.int16)
        positions = self.position + np.arange(count) * self.step
        out = np.interp(positions, np.arange(len(known)), known)
        # The next piece starts where this one ended: its first sample is
        # the one just kept, so positions carry over relative to it.
        self.position = positions[-1] + self.step - last
        return out.astype(np.int16)


def frame_to_mono_pcm(frame: AudioFrame) -> np.ndarray:
    """aiortc audio frames may be stereo - the SIP side only ever carries
    mono, so any inbound-from-Talk audio is downmixed here before it can be
    resampled/sent."""
    arr = frame.to_ndarray()
    channels = len(frame.layout.channels) if frame.layout else 1
    if arr.ndim == 2 and arr.shape[0] > 1:
        mono = arr.astype(np.float64).mean(axis=0)
    else:
        row = arr.flatten()
        mono = row.reshape(-1, channels).astype(np.float64).mean(axis=1) if channels > 1 else row.astype(np.float64)
    return np.clip(mono, -32768, 32767).astype(np.int16)


class SipAudioTrack(AudioStreamTrack):
    """Reads decoded PCM (mono, at the RtpSession's negotiated sample rate -
    8kHz for PCMU, 16kHz for G.722) from an RtpSession's receive queue,
    upsampled to AUDIO_SAMPLE_RATE."""

    def __init__(self, rtp_session):
        super().__init__()
        self.rtp_session = rtp_session
        self._pts = 0
        self._next_frame_at = None
        self._stats = {"from_phone": 0, "silence": 0, "dropped": 0, "peak": 0, "since": None}
        self._silence = np.zeros(rtp_session.samples_per_packet, dtype=np.int16)
        self._agc = Agc(target_peak=config.agc_target_peak, max_gain=config.agc_max_gain,
                        silence_threshold=config.agc_silence_threshold) if config.agc_enabled else None
        self._resampler = StreamResampler(rtp_session.sample_rate, AUDIO_SAMPLE_RATE)

    async def recv(self):
        """Hands out exactly one packet per packet interval of wall clock.
        The base class paces its frames that way and this override has to do
        the same: taking the timing from the receive queue instead means a
        queued packet returns instantly while an empty queue costs a full
        poll interval, so the track runs faster than real time and pads the
        timeline with inserted silence - heard as badly distorted audio."""
        loop = asyncio.get_running_loop()
        rtp = self.rtp_session
        frame_duration = rtp.samples_per_packet / rtp.sample_rate

        # Once frames are paced, a backlog is pure added latency, so keep
        # only a small jitter cushion and drop what is older than that.
        while rtp.recv_queue.qsize() > MAX_QUEUED_PACKETS:
            try:
                rtp.recv_queue.get_nowait()
                self._stats["dropped"] += 1
            except Exception:
                break

        try:
            pcm_in = await asyncio.to_thread(rtp.recv_queue.get, True, RTP_QUEUE_POLL_INTERVAL)
            self._stats["from_phone"] += 1
            self._stats["peak"] = max(self._stats["peak"], int(np.abs(pcm_in.astype(np.int32)).max()) if len(pcm_in) else 0)
        except Exception:
            pcm_in = self._silence
            self._stats["silence"] += 1

        # A second's worth of "what actually arrived from the phone" - the
        # difference between audio that is missing and audio that is
        # mangled is not audible from the far end, but it is visible here.
        if self._stats["since"] is None:
            self._stats["since"] = loop.time()
        elif loop.time() - self._stats["since"] >= 1.0:
            s = self._stats
            gain = f", agc gain {self._agc.gain:.1f}x" if self._agc is not None else ""
            # Dropped packets are the ones that arrived faster than real
            # time and had to go to keep latency down. They are not
            # missing audio the way silence-filled is - they are audio
            # thrown away - and they sound like chopping, so they are
            # worth their own number rather than being invisible.
            print(f"[talk] Phone audio: {s['from_phone']} packets, {s['silence']} silence-filled, "
                  f"{s['dropped']} dropped, peak {s['peak']} (before agc){gain}")
            self._stats = {"from_phone": 0, "silence": 0, "dropped": 0, "peak": 0, "since": loop.time()}

        now = loop.time()
        if self._next_frame_at is None or self._next_frame_at < now - frame_duration:
            self._next_frame_at = now  # first frame, or lost the thread of real time
        elif self._next_frame_at > now:
            await asyncio.sleep(self._next_frame_at - now)
        self._next_frame_at += frame_duration

        if self._agc is not None:
            pcm_in = self._agc.process(pcm_in)
        pcm_48k = self._resampler.process(pcm_in)
        frame = AudioFrame.from_ndarray(pcm_48k.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = AUDIO_SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, AUDIO_SAMPLE_RATE)
        self._pts += len(pcm_48k)
        return frame
