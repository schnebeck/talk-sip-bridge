# What a healthy call looks like

Every failure this bridge has had so far looked the same from outside: a
call that rings, connects, and is silent. The log lines and numbers below
are what a call that works produces, so a call that does not can be placed
against them line by line instead of guessed at.

Produced by `tests/hardware/test_human_call.py` - an inbound call from the
second SIP account, answered by a person in Talk, tones in one direction
and speech in the other. Names and identifiers here are from one such call;
what matters is the sequence and the orders of magnitude.

## The sequence, from ring to hangup

```
[sip:default] INVITE received for call <call-id>
[call:default] Incoming call from "sip-phone2" <sip:**622@fritz.box>;tag=…
[talk] Talk is ringing for room <room> (2.3s to join the call)
[talk] Rang <user> for the call in room <room>
[talk] Ringing lobby for call <call-id> from …, watching room <room> for accept
[talk] Human joined the call - accepting <call-id>
[talk] Publishing call audio for <call-id> as virtual session phone-…
[talk] Requested audio from <their session> for <call-id> (attempt 1/6)
[sip:default] ACK received for call <call-id>
[talk] Publish ICE state: checking
[talk] Publish connection state: connecting
[talk] Publish ICE state: completed
[talk] Publish connection state: connected
[talk] Receiving audio from <their session> for <call-id>
[talk] Subscriber connection state: connecting
[talk] Subscriber connection state: connected
    … one line per second per direction while the call runs …
[sip:default] BYE received for call <call-id>
[talk] Subscriber connection state: closed
[talk] Publish ICE state: closed
[talk] Publish connection state: closed
[talk] Call <call-id> ended, virtual session removed
[talk] Ended the call in room <room> - the phone call behind it is over
```

Worth knowing about this sequence:

- **"Requested audio … (attempt 1/6)" should be followed by "Receiving audio"
  within a second.** Repeated attempts mean the other side has no publisher
  yet; six of them mean it never got one.
- **Both connections report themselves.** The publisher (phone to Talk) and
  the subscriber (Talk to phone) each go connecting → connected, and each
  says so. A call where only the publisher appears is half a call.
- **The last line is the one to read.** "Ended the call in room …" means the
  call in Talk is over for everyone. The other wording - "Left the call …,
  but it is still running" - means the bridge account is not a moderator in
  that room, and whoever answered is now alone in a call with nobody on the
  other end, hearing Talk's waiting tone as endless ringing.

## The numbers, once per second per direction

```
[talk] Phone audio: 51 packets, 0 silence-filled, 0 dropped, peak 9913 (before agc), agc gain 1.0x
[talk] Talk audio for <call-id>: 50 frames, peak 3950
```

| Reading | Healthy | What a deviation means |
|---|---|---|
| `packets` | 50-52 | Below that, audio is not arriving from the gateway |
| `silence-filled` | 0 | The track ran dry and silence was inserted: the stream is late or gone |
| `dropped` | 0 in steady state, a burst at the start is normal | Audio arriving faster than real time and thrown away to hold latency down - heard as chopping |
| `peak` (phone side) | ~10000 while someone speaks, tens in silence | Stuck at tens throughout: the phone side is mute |
| `frames` (Talk side) | 50-52 | Below that, Talk's audio is not arriving |
| `peak` (Talk side) | thousands while someone speaks, under ~20 in silence | **Frames arriving with peak under 20 for the whole call is a muted microphone, not a broken path** - the distinction that is otherwise invisible |

## What the measurement says

The same call, measured by the script rather than by ear:

```
440 Hz  arrived as  439.5 Hz in 3 tones, worst distortion -50.7 dB, level moves up to 0.3 dB while steady, 50 Hz sidebands -60.0 dB
660 Hz  arrived as  660.4 Hz in 3 tones, worst distortion -45.6 dB, level moves up to 0.3 dB while steady, 50 Hz sidebands -60.7 dB
880 Hz  arrived as  880.2 Hz in 3 tones, worst distortion -41.9 dB, level moves up to 0.2 dB while steady, 50 Hz sidebands -58.3 dB
PASS  every tone reached the Talk side at its own frequency
PASS  no modulation at the packet rate

8.0s of audio came back from Talk
  4s  -21.3 dBFS
  5s  -24.4 dBFS
8 of 8 seconds carry signal above -45 dBFS
PASS  audio is flowing from Talk back to the phone
```

The three numbers to compare against:

- **Frequency** within a couple of Hz. A tone arriving 0.5% high means the
  timeline is being stretched somewhere - a resampler, or packets lost and
  concatenated.
- **Sidebands at the packet rate below -40 dB.** Above that, something acts
  once per packet and it is audible as a low ringing on anything steady.
- **Level swing under 1 dB while a tone is steady.** Several dB means gain
  control hunting or the audio being chopped.

## Client caveat

Answer in a **browser**. Talk Android 25.0.0 opens dozens of signaling
connections when answering an incoming call, all but one of them idle; the
session that publishes the microphone is replaced moments later and nothing
it sends arrives. 24.0.4 behaves correctly. See the project notes for the
measurements behind this.
