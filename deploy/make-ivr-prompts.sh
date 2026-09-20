#!/bin/sh
# Builds the spoken prompts a conference number greets callers with.
#
# What a caller hears is a deployment's own words, in its own languages, so
# no recording ships with the bridge - this generates them from text with
# SVOX pico, whose telephone-band output is far more natural than espeak's
# and costs one small package:
#
#     apt-get install -y libttspico-utils        # pico2wave
#     deploy/make-ivr-prompts.sh /etc/talk-sip-bridge/prompts
#     apt-get purge -y libttspico-utils          # the WAVs are what is needed
#
# Then, per line, in the daemon's environment - several files separated by
# commas are played one after another, the next one only reached by a
# caller who has not keyed anything in (see bridge/dialin_ivr.py):
#
#     BRIDGE_IVR_PROMPT_WAV=<dir>/meeting-id-de.wav,<dir>/meeting-id-en.wav
#     BRIDGE_IVR_PIN_PROMPT_WAV=<dir>/pin-de.wav,<dir>/pin-en.wav
#
# pico2wave writes 16 kHz mono; the bridge resamples to whatever the call
# negotiated.
set -eu

OUT="${1:-/etc/talk-sip-bridge/prompts}"

MEETING_DE="${MEETING_DE:-Bitte geben Sie Ihre zehnstellige Meeting-ID über die Tastatur Ihres Telefons ein. Schließen Sie mit der Raute-Taste ab.}"
MEETING_EN="${MEETING_EN:-Please enter your ten digit meeting I D on your telephone keypad, and finish with the hash key.}"
PIN_DE="${PIN_DE:-Bitte geben Sie jetzt Ihre PIN ein und schließen Sie mit der Raute-Taste ab.}"
PIN_EN="${PIN_EN:-Please enter your PIN now, and finish with the hash key.}"

command -v pico2wave >/dev/null || { echo "pico2wave is not installed"; exit 1; }
mkdir -p "$OUT"

say() {
    pico2wave -l "$1" -w "$OUT/$2" "$3"
    chmod 0644 "$OUT/$2"
    echo "$OUT/$2"
}

say de-DE meeting-id-de.wav "$MEETING_DE"
say en-GB meeting-id-en.wav "$MEETING_EN"
say de-DE pin-de.wav "$PIN_DE"
say en-GB pin-en.wav "$PIN_EN"
