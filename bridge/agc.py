"""Simple peak-following automatic gain control for phone-side audio.

Some handsets have a much quieter microphone than a laptop/headset, with no
way to adjust that from this end of the call, and how quiet it sounds also
varies with how close the speaker is to the handset - a single fixed gain
either clips on loud moments or stays too quiet on soft ones. This tracks a
target peak level and adapts: gain rises slowly (avoids audible "pumping"
level jumps) and falls quickly (avoids clipping), the standard shape for a
mic AGC. Frozen during near-silence so it doesn't ramp up to max_gain
between words and then blast the next syllable.
"""
import numpy as np


class Agc:
    def __init__(self, target_peak: int = 10000, max_gain: float = 20.0, min_gain: float = 0.2,
                 rise_coeff: float = 0.03, fall_coeff: float = 0.3, silence_threshold: int = 50):
        self.target_peak = target_peak
        self.max_gain = max_gain
        self.min_gain = min_gain
        self.rise_coeff = rise_coeff
        self.fall_coeff = fall_coeff
        self.silence_threshold = silence_threshold
        self.gain = 1.0

    def process(self, pcm: np.ndarray) -> np.ndarray:
        if len(pcm) == 0:
            return pcm
        peak = float(np.max(np.abs(pcm)))
        if peak > self.silence_threshold:
            desired = min(self.max_gain, max(self.min_gain, self.target_peak / peak))
            coeff = self.fall_coeff if desired < self.gain else self.rise_coeff
            self.gain += (desired - self.gain) * coeff
        return np.clip(pcm.astype(np.float32) * self.gain, -32768, 32767).astype(np.int16)
