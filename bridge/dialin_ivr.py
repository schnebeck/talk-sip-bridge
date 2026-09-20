"""Asking a caller which conversation they want.

A number that belongs to the bridge but to no one person is a conference
line: the call is answered, the caller keys in the meeting id Nextcloud
shows for the conversation (its token, all digits wherever SIP is
configured), a PIN if that conversation wants one, and the bridge puts
them in. Talk's own SIP bridge holds the same dialogue.

The dialogue is tones rather than speech, and each one means one thing:

    ask        two rising beeps      key it in, end with #
    ask a PIN  three short beeps     that conversation wants a PIN
    accepted   a rising pair         you are being connected
    rejected   two low beeps         that is not a conversation you can enter

A deployment that wants words instead points BRIDGE_IVR_PROMPT_WAV at a
recording, which is played in place of the "ask" beeps.

Everything here runs on the caller's own RTP session, before any of this
call reaches Talk: nothing is published until a conversation is known,
because there is no room to publish into.
"""
import queue
import threading
import time
import wave

import numpy as np

import talk_sip_bridge
from config import config

# How long a caller gets, and how long a prompt is given before the next
# language is played: config.ivr_* (see docs/CONFIG.md), because how long
# a caller needs depends on what they are calling from.
ATTEMPTS = 3
MAX_DIGITS = 32
TERMINATOR = "#"
CLEAR = "*"


def tone(frequency: float, seconds: float, rate: int, amplitude: int = 7000) -> np.ndarray:
    """One beep, faded in and out - a tone that starts at full amplitude
    clicks, and a click is indistinguishable from a bad line."""
    samples = np.arange(int(rate * seconds)) / rate
    fade = np.minimum(1.0, np.minimum(samples, seconds - samples) * 200)
    return (np.sin(2 * np.pi * frequency * samples) * fade * amplitude).astype(np.int16)


def silence(seconds: float, rate: int) -> np.ndarray:
    return np.zeros(int(rate * seconds), dtype=np.int16)


def phrase(parts, rate: int) -> np.ndarray:
    return np.concatenate([p for p in parts] or [silence(0, rate)])


def ask_tones(rate: int) -> np.ndarray:
    return phrase([tone(660, 0.18, rate), silence(0.08, rate), tone(880, 0.18, rate)], rate)


def ask_pin_tones(rate: int) -> np.ndarray:
    return phrase([tone(1320, 0.1, rate), silence(0.06, rate),
                   tone(1320, 0.1, rate), silence(0.06, rate),
                   tone(1320, 0.1, rate)], rate)


def accepted_tones(rate: int) -> np.ndarray:
    return phrase([tone(880, 0.15, rate), tone(1320, 0.25, rate)], rate)


def rejected_tones(rate: int) -> np.ndarray:
    return phrase([tone(330, 0.2, rate), silence(0.1, rate), tone(330, 0.2, rate)], rate)


def load_prompts(paths, rate: int) -> list:
    """Recorded prompts in the order they should be played - typically the
    same sentence in two languages, the second one reached only by a
    caller who did nothing after the first.

    Accepts a comma-separated string or a list. Files that cannot be used
    are left out rather than fatal: a deployment with a broken prompt
    should still answer its telephone."""
    if isinstance(paths, str):
        paths = [p.strip() for p in paths.split(",")]
    loaded = [load_prompt(path, rate) for path in paths if path]
    return [p for p in loaded if p is not None]


def load_prompt(path: str, rate: int):
    """One recorded prompt, resampled to the call's rate, or None if it
    cannot be used."""
    if not path:
        return None
    try:
        with wave.open(path) as recording:
            if recording.getsampwidth() != 2:
                raise ValueError(f"{recording.getsampwidth() * 8} bit, not 16")
            channels = recording.getnchannels()
            pcm = np.frombuffer(recording.readframes(recording.getnframes()), dtype=np.int16)
            if channels > 1:
                pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
            source_rate = recording.getframerate()
    except Exception as e:
        print(f"[ivr] Cannot use the prompt {path!r} ({e}) - using tones instead")
        return None
    if source_rate != rate:
        length = int(len(pcm) * rate / source_rate)
        pcm = np.interp(np.arange(length) * source_rate / rate,
                        np.arange(len(pcm)), pcm).astype(np.int16)
    return pcm


class DialInIvr:
    """One caller, one dialogue. `press` is called from whichever thread
    reads the audio; `run` blocks until the caller is in a conversation,
    gives up, or hangs up, and belongs in a worker thread."""

    def __init__(self, rtp, resolve=None, *, prompt_wav="", pin_prompt_wav="",
                 attempts: int = ATTEMPTS, first_digit_timeout: float = None,
                 next_digit_timeout: float = None, prompt_gap: float = None):
        self.rtp = rtp
        self.resolve = resolve or talk_sip_bridge.join_by_meeting_id
        self.attempts = attempts
        self.first_digit_timeout = (first_digit_timeout if first_digit_timeout is not None
                                    else config.ivr_first_digit_timeout)
        self.next_digit_timeout = (next_digit_timeout if next_digit_timeout is not None
                                   else config.ivr_next_digit_timeout)
        self.prompt_gap = prompt_gap if prompt_gap is not None else config.ivr_prompt_gap
        self.digits = queue.Queue()
        self.stopped = threading.Event()
        rate = rtp.sample_rate
        self.prompts = load_prompts(prompt_wav, rate) or [ask_tones(rate)]
        self.pin_prompts = load_prompts(pin_prompt_wav, rate) or [ask_pin_tones(rate)]
        self.accepted = accepted_tones(rate)
        self.rejected = rejected_tones(rate)

    def press(self, digit: str):
        self.digits.put(digit)

    def stop(self):
        """Ends the dialogue from outside - the caller hung up."""
        self.stopped.set()
        self.digits.put(None)

    # -- audio ------------------------------------------------------------

    def play(self, samples):
        """Plays into the call, paced against a fixed schedule, and stops
        the moment a key is pressed: a caller who already knows the number
        should not have to listen to the prompt first."""
        spp = self.rtp.samples_per_packet
        interval = spp / self.rtp.sample_rate
        due = time.monotonic()
        for start in range(0, len(samples), spp):
            if self.stopped.is_set() or not self.digits.empty():
                return
            chunk = samples[start:start + spp]
            if len(chunk) < spp:
                chunk = np.concatenate([chunk, np.zeros(spp - len(chunk), dtype=np.int16)])
            self.rtp.send_pcm(chunk)
            due += interval
            remaining = due - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)

    def announce(self, prompts):
        """Plays prompts one after another, stopping at the one the
        caller answers. The second is normally the same sentence in
        another language: a caller who understood the first is already
        keying in, and a caller who did not is waiting for words they
        know."""
        for index, prompt in enumerate(prompts):
            if self.stopped.is_set() or not self.digits.empty():
                return
            self.play(prompt)
            if index + 1 < len(prompts) and not self._waits_for_a_key(self.prompt_gap):
                continue
            return

    def _waits_for_a_key(self, seconds: float) -> bool:
        """Whether a key arrives within `seconds`, without taking it out
        of the queue - what was pressed still belongs to the number being
        keyed in."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.stopped.is_set():
            if not self.digits.empty():
                return True
            time.sleep(0.05)
        return not self.digits.empty()

    # -- keys -------------------------------------------------------------

    def collect(self) -> str:
        """What the caller keys in, up to # or a silence long enough to
        mean they are done. Returns "" if they pressed nothing at all, or
        cleared what they had with *."""
        typed = ""
        while not self.stopped.is_set():
            timeout = self.next_digit_timeout if typed else self.first_digit_timeout
            try:
                digit = self.digits.get(timeout=timeout)
            except queue.Empty:
                return typed
            if digit is None:            # stop() was called
                return ""
            if digit == TERMINATOR:
                if not typed:
                    # A hash with nothing in front of it says nothing.
                    # It is also what arrives when a caller finishes one
                    # attempt just as the next prompt starts - counting
                    # it as an empty answer would spend an attempt on it.
                    continue
                return typed
            if digit == CLEAR:
                typed = ""
                continue
            typed += digit
            if len(typed) >= MAX_DIGITS:
                return typed
        return ""

    # -- the dialogue ------------------------------------------------------

    def run(self):
        """The conversation the caller ends up in, or None.

        None means the caller could not be placed: they never keyed
        anything in, they got it wrong `attempts` times, or this bridge is
        not allowed to ask at all. The caller hears why, as far as tones
        can say it, before the call is ended."""
        for attempt in range(self.attempts):
            self.announce(self.prompts)
            token = self.collect()
            if self.stopped.is_set():
                return None
            if not token:
                print("[ivr] Nothing was keyed in")
                self.play(self.rejected)
                continue

            outcome, room = self.resolve(token)
            if outcome == talk_sip_bridge.NEEDS_PIN:
                print(f"[ivr] Meeting {token} wants a PIN")
                self.announce(self.pin_prompts)
                pin = self.collect()
                if self.stopped.is_set():
                    return None
                outcome, room = self.resolve(token, pin) if pin else (talk_sip_bridge.UNKNOWN, None)

            if outcome == talk_sip_bridge.OK:
                print(f"[ivr] Meeting {token} accepted the caller into {room['token']}")
                self.play(self.accepted)
                return room
            if outcome == talk_sip_bridge.REFUSED:
                # Not the caller's mistake and not one more attempt will
                # fix it: this bridge cannot ask Nextcloud anything.
                self.play(self.rejected)
                return None
            print(f"[ivr] Meeting {token} was not one the caller may enter "
                  f"(attempt {attempt + 1} of {self.attempts})")
            self.play(self.rejected)
        return None
