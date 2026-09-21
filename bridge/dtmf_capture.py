"""Keeping what the far end actually sent, when a key press is in
dispute.

Switched on with BRIDGE_DTMF_DEBUG and off the rest of the time,
because it records call audio. It answers the one question the logs
cannot: whether a press the bridge never reported was missed by the
detector or never sent at all - which is the difference between a bug
here and a gateway that turns key presses into sound.

Bounded on purpose. A capture is for the seconds around a dialogue, not
for a call, so it stops at MAX_BLOCKS and the rest of the call is
carried as if nothing were recording.
"""
import wave

import numpy as np

from config import config

# Roughly a minute at one block per 20 ms - long enough for a caller to
# find the keypad and type a meeting id, short enough not to hold a
# call's worth of audio in memory.
MAX_BLOCKS = 3000


class DtmfCapture:
    """What the far end sent, for one call."""

    def __init__(self, call_id: str, sample_rate: int):
        self.blocks = [] if config.dtmf_debug else None
        self.name = f"{call_id or 'call'}"
        self.sample_rate = sample_rate

    def add(self, pcm):
        if self.blocks is not None and len(self.blocks) < MAX_BLOCKS:
            self.blocks.append(pcm)

    def write(self):
        """Writes the capture out and forgets it, so closing a session
        twice does not write it twice."""
        if not self.blocks:
            return
        path = f"{config.state_dir or '/tmp'}/dtmf-{self.name.replace('@', '-')}.wav"
        try:
            with wave.open(path, "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(self.sample_rate)
                out.writeframes(np.concatenate(self.blocks).astype(np.int16).tobytes())
            print(f"[rtp] What the far end sent: {path}")
        except Exception as e:
            print(f"[rtp] Could not write the capture: {e!r}")
        self.blocks = []
