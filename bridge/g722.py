# talk-sip-bridge - bridge/g722.py
# G.722 via PyAV/libavcodec - stateful across frames, unlike G.711.
#
#   Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
#   Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
#   Written by Anthropic Claude Opus 5 - AI generated content.
#
#   Free software under the GNU General Public License, version 3 or later.
#   There is no warranty, to the extent permitted by law. The full text is
#   in LICENSES/GPL-3.0-or-later.txt.
#
# SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
# SPDX-FileContributor: Anthropic Claude Opus 5 (AI generated content)
# SPDX-License-Identifier: GPL-3.0-or-later

"""G.722 via PyAV/libavcodec - stateful across frames, unlike G.711.

libavcodec is already a dependency of aiortc, so this costs nothing. The
state is the point: G.722's sub-band ADPCM coding carries across frames,
so unlike g711.py this needs a persistent encoder/decoder object per call
rather than a stateless function.

Audio is 16kHz despite the RTP payload advertising "G722/8000" in SDP - a
historical quirk of RFC 3551, where the clock rate in the codec's rtpmap
name is fixed at the value assigned when G.722 was originally registered,
not its actual sample rate. RTP timestamps for this payload therefore still
advance at 8000Hz even though twice that many samples are carried per
packet - see rtp.py's RTP_CLOCK_INCREMENT.
"""
import av
import numpy as np

SAMPLE_RATE = 16000


class G722Encoder:
    def __init__(self):
        self._ctx = av.CodecContext.create("g722", "w")
        self._ctx.sample_rate = SAMPLE_RATE
        self._ctx.format = "s16"
        self._ctx.layout = "mono"

    def encode(self, pcm: np.ndarray) -> bytes:
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        return b"".join(bytes(packet) for packet in self._ctx.encode(frame))


class G722Decoder:
    def __init__(self):
        self._ctx = av.CodecContext.create("g722", "r")
        self._ctx.sample_rate = SAMPLE_RATE
        self._ctx.format = "s16"
        self._ctx.layout = "mono"

    def decode(self, payload: bytes) -> np.ndarray:
        frames = self._ctx.decode(av.Packet(payload))
        if not frames:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate([f.to_ndarray().flatten() for f in frames]).astype(np.int16)


if __name__ == "__main__":
    # Quick self-test: encode/decode a sine tone through a fresh
    # encoder/decoder pair (mirrors a real call's persistent state), check
    # for distortion.
    freq = 440
    t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
    tone = (np.sin(2 * np.pi * freq * t) * 20000).astype(np.int16)

    encoder = G722Encoder()
    decoder = G722Decoder()
    decoded_chunks = []
    frame_size = 320  # 20ms at 16kHz
    for i in range(0, len(tone), frame_size):
        chunk = tone[i:i + frame_size]
        if len(chunk) < frame_size:
            chunk = np.pad(chunk, (0, frame_size - len(chunk)))
        encoded = encoder.encode(chunk)
        decoded_chunks.append(decoder.decode(encoded))
    decoded = np.concatenate(decoded_chunks)

    spectrum = np.abs(np.fft.rfft(decoded.astype(np.float64) * np.hanning(len(decoded))))
    freqs = np.fft.rfftfreq(len(decoded), d=1.0 / SAMPLE_RATE)
    peak = freqs[np.argmax(spectrum)]
    print(f"Input tone: {freq} Hz, detected after G.722 round-trip: {peak:.1f} Hz")
    assert abs(peak - freq) < 5, "G.722 codec self-test failed!"
    print("G.722 codec self-test OK.")
