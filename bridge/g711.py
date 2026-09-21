# talk-sip-bridge - bridge/g711.py
# G.711 codecs (PCMU/mu-law and PCMA/A-law) in pure NumPy, vectorized.
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

"""G.711 codecs (PCMU/mu-law and PCMA/A-law) in pure NumPy, vectorized.

No external codec package needed - the formulas are standard (ITU-T G.711).
At very large call volumes, a C codec (e.g. via PyAV/libavcodec, already a
dependency of aiortc) would use less CPU per call than this pure-Python
implementation.
"""
import numpy as np

BIAS = 0x84
CLIP = 32635


ULAW_SEGMENT_ENDS = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF])


def linear_to_ulaw(samples: np.ndarray) -> np.ndarray:
    """int16 PCM -> 8-bit mu-law (G.711).

    On the 14-bit value with the bias scaled to match, which is what the
    standard says and what every other implementation produces. Encoding
    the 16-bit value against a hand-built threshold table is close enough
    to sound like speech and wrong enough to add half again as much
    quantisation noise - and it turns digital silence into a constant 260.
    """
    values = samples.astype(np.int32) >> 2          # mu-law works on 14 bits
    negative = values < 0
    mask = np.where(negative, 0x7F, 0xFF)
    values = np.clip(np.abs(values), 0, CLIP >> 2) + (BIAS >> 2)

    segment = np.zeros_like(values)
    for index, end in enumerate(ULAW_SEGMENT_ENDS):
        segment = np.where(values > end, index + 1, segment)
    segment = np.clip(segment, 0, 8)

    encoded = (segment << 4) | ((values >> (segment + 1)) & 0x0F)
    encoded = np.where(segment >= 8, 0x7F, encoded)
    return ((encoded ^ mask) & 0xFF).astype(np.uint8)


def ulaw_to_linear(ulaw: np.ndarray) -> np.ndarray:
    """8-bit mu-law -> int16 PCM."""
    ulaw = ~ulaw.astype(np.int32) & 0xFF
    sign = ulaw & 0x80
    exponent = (ulaw >> 4) & 0x07
    mantissa = ulaw & 0x0F
    sample = ((mantissa << 3) + BIAS) << exponent
    sample = sample - BIAS
    sample = np.where(sign != 0, -sample, sample)
    return np.clip(sample, -32768, 32767).astype(np.int16)


ALAW_SEGMENT_ENDS = np.array([0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF])


def linear_to_alaw(samples: np.ndarray) -> np.ndarray:
    """int16 PCM -> 8-bit A-law (G.711). The European half of G.711, and
    what most SIP providers outside North America offer - several offer
    nothing else."""
    values = samples.astype(np.int32) >> 3          # A-law works on 13 bits
    negative = values < 0
    mask = np.where(negative, 0x55, 0xD5)
    values = np.where(negative, -values - 1, values)

    segment = np.zeros_like(values)
    for index, end in enumerate(ALAW_SEGMENT_ENDS):
        segment = np.where(values > end, index + 1, segment)
    segment = np.clip(segment, 0, 8)

    shift = np.where(segment < 2, 1, segment)
    encoded = (segment << 4) | ((values >> shift) & 0x0F)
    encoded = np.where(segment >= 8, 0x7F, encoded)
    return ((encoded ^ mask) & 0xFF).astype(np.uint8)


def alaw_to_linear(alaw: np.ndarray) -> np.ndarray:
    """8-bit A-law -> int16 PCM."""
    values = (alaw.astype(np.int32) ^ 0x55) & 0xFF
    segment = (values & 0x70) >> 4
    sample = (values & 0x0F) << 4
    sample = np.where(segment == 0, sample + 8, sample + 0x108)
    sample = np.where(segment > 1, sample << (segment - 1), sample)
    sample = np.where((values & 0x80) != 0, sample, -sample)
    return np.clip(sample, -32768, 32767).astype(np.int16)


if __name__ == "__main__":
    # Quick self-test: encode/decode a sine tone, check for distortion.
    sample_rate = 8000
    freq = 440
    t = np.arange(sample_rate) / sample_rate
    tone = (np.sin(2 * np.pi * freq * t) * 20000).astype(np.int16)

    encoded = linear_to_ulaw(tone)
    decoded = ulaw_to_linear(encoded)

    spectrum = np.abs(np.fft.rfft(decoded.astype(np.float64) * np.hanning(len(decoded))))
    freqs = np.fft.rfftfreq(len(decoded), d=1.0 / sample_rate)
    peak = freqs[np.argmax(spectrum)]
    print(f"Input tone: {freq} Hz, detected after mu-law round-trip: {peak:.1f} Hz")
    assert abs(peak - freq) < 5, "G.711 codec self-test failed!"
    print("G.711 codec self-test OK.")
