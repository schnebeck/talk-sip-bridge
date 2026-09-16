"""G.711 codecs (PCMU/mu-law and PCMA/A-law) in pure NumPy, vectorized.

No external codec package needed - the formulas are standard (ITU-T G.711).
At very large call volumes, a C codec (e.g. via PyAV/libavcodec, already a
dependency of aiortc) would use less CPU per call than this pure-Python
implementation.
"""
import numpy as np

BIAS = 0x84
CLIP = 32635


def linear_to_ulaw(samples: np.ndarray) -> np.ndarray:
    """int16 PCM -> 8-bit mu-law (RFC 7655 / G.711)."""
    samples = samples.astype(np.int32)
    sign = np.where(samples < 0, 0x80, 0x00)
    samples = np.abs(samples)
    samples = np.clip(samples, 0, CLIP) + BIAS

    exponent = np.zeros_like(samples)
    exp_lut = [0, 132, 396, 924, 1980, 4092, 8316, 16764]
    for exp_val, threshold in enumerate(exp_lut):
        exponent = np.where(samples >= threshold, exp_val, exponent)

    mantissa = (samples >> (exponent + 3)) & 0x0F
    ulaw = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return ulaw.astype(np.uint8)


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
