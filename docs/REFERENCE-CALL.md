# What a healthy call looks like

Every failure this bridge has had looked the same from outside: a call that
rings, connects, and is silent. So this is what the working versions
produce, line by line, to hold a broken one against instead of guessing.

Three kinds of call reach this bridge and each has its own shape. The
identifiers below are from real calls; what matters is the sequence, the
order, and the orders of magnitude.

## At rest

Nothing at all. A daemon with no call logs five lines at startup and then
stays quiet until something happens:

```
[talk] room connection up as internal client, session <id>
[talk] dialout connection up as internal client, session <id>
[sip:default] TCP connection to <gateway or relay> established
[daemon] Line default (<user>@<gateway>) was registered before restart - resumed: True (last_error=None)
[daemon] Control API on 127.0.0.1:8765 (1 line(s): default)
```

**Two signaling connections, always.** One of them missing means half the
bridge: no dialout, or no calls at all. They reconnect on their own and say
so; anything logged as `ERROR the … connection loop` is a fault that should
never happen and is being recovered from.

Silence afterwards is correct. A registration refresh, which happens every
few minutes, logs nothing unless it fails - six hours of a healthy idle
bridge is five lines.

## Talk calls a phone (dialout)

```
[talk] Dialout request for 620 in room <token>
[call:default] Outbound call started to **620
   … ringing …
[talk] Phone participant phone-<id> announced without audio, as phones/<actor>
[talk] Publishing call audio for <call-id> as virtual session phone-<id>
[talk] Told 1 participant(s) that 620's microphone is on
[talk] Requested audio from <their session> for <call-id> (attempt 1/6)
[talk] Receiving audio from <their session> for <call-id>
[talk] Publish ICE state: checking
[talk] Publish connection state: connecting
[talk] Publish ICE state: completed
[talk] Publish connection state: connected
[talk] Subscriber connection state: connecting
[talk] Subscriber connection state: connected
[talk] Talk's audio reaches the phone for <call-id>
   … one line per direction every 15s …
[sip:default] BYE received for call <call-id>
[talk] Call <call-id> ended, virtual session removed
[call:default] Hangup for <call-id> acknowledged: SIP/2.0 200 OK
```

- The request arrives on the **dialout** connection; everything after
  "Publishing" is the **room** connection. They are different sessions and
  the ids differ.
- **The name plate appears when the call is answered, not while it rings.**
  A phone announced early is announced as being in the call, and Talk then
  stops the ringback the caller is listening to.
- `announced without audio` is normal with `BRIDGE_PHONE_PARTICIPANT=phone`.
  The audio does not ride on the name plate - it cannot - and nothing about
  this line says the call is silent.
- **Both connections report themselves.** The publisher (phone into the
  room) and the subscriber (the room to the phone) each go connecting then
  connected. A call where only the publisher appears is half a call.
- **"Talk's audio reaches the phone" is the one that matters.** Everything
  above it is negotiation; this line is a frame having arrived.

### Ended from Talk instead

```
[talk] Room call ended - ending SIP side
[call:default] Cancelling the outbound call to 620 - it was hung up before anyone answered
[call:default] **620 refused the call: SIP/2.0 487 Request Cancelled
```

`Cancelling` rather than `Hanging up` is correct for a call still ringing,
and `487` is the gateway confirming it. A `BYE` here would be answered
`481` and the phone would ring on.

## A phone calls in and is answered by a person

The bridge rings the room through Talk's OCS API and answers the SIP side
the moment somebody joins - there is no ringing exchange in the signaling
protocol to do it with.

```
[sip:default] INVITE received for call <call-id>
[call:default] Incoming call from "sip-phone2" <sip:**622@fritz.box>
[talk] Ringing <user> for call <call-id> from …, watching room <room> for accept
[talk] Human joined the call - accepting <call-id>
[talk] Publishing call audio for <call-id> as virtual session phone-<id>
   … as above, to "Talk's audio reaches the phone" …
[sip:default] BYE received for call <call-id>
[talk] Call <call-id> ended, virtual session removed
```

- **The accept is a transition, not a state.** "Human joined" means a
  session *moved into* the call; the bridge never reads who is in it,
  because the server keeps listing sessions whose clients are long gone.
- If the line has no `NOTIFY_USER` configured, the call simply rings
  unnoticed and the journal says so once. That is the default and it is
  deliberate: nothing here may answer a call that might be for a person.

## A phone dials in to a conversation

The caller reaches the bridge's own number, is answered at once, and is
asked for a meeting id.

```
[call:default] Answering <call-id> on conference number **9 - the caller will be asked for a meeting id
[call:default] DTMF 1 on <call-id>            … one per key …
[ivr] Meeting <token> accepted the caller into <token>
[talk] <call-id> dialled into conversation <token> as guests/<actor>
[talk] Phone participant phone-<id> announced without audio, as guests/<actor>
[talk] Publishing call audio for <call-id> as virtual session phone-<id>
[talk] No other participant found in room <token> - phone side will not hear Talk's audio
[talk] Publish connection state: connected
   … when somebody joins, however much later …
[talk] <their session> joined after <call-id> was already publishing - asking for their audio now
[talk] Requested audio from <their session> for <call-id> (attempt 1/6)
[talk] Talk's audio reaches the phone for <call-id>
```

- **"No other participant found" is expected here**, not a fault: a caller
  who dials in reaches an empty room. The line that resolves it is
  "joined after … asking for their audio now", and it can come minutes
  later.
- Every key press should produce exactly one `DTMF` line. Two for one
  press means the de-duplication window is too short; none means the
  gateway sends them by a road that is not being read.

## A participant who leaves and comes back

Saving a new microphone in Talk, reloading the page, a network blip - all
look like this, and the call is meant to survive them:

```
[talk] Human audio relay for <call-id> ended (MediaStreamError())
[talk] Subscriber connection state: closed
[talk] Nobody is left in the call - giving it 30s before ending the SIP side
[talk] <new session> joined after <call-id> was already publishing - asking for their audio now
[talk] Requested audio from <new session> for <call-id> (attempt 1/6)
[talk] Talk's audio reaches the phone for <call-id>
[talk] The call filled up again within 30s - not ending the SIP side
```

Measured: 4.8 to 7.1 seconds from leaving to being back, with a **new
session id** each time. The whole interruption, relay to audio again, was
eight seconds. If the last line instead reads "Everyone but this bridge
left the call", nobody came back within the wait
(`BRIDGE_EMPTY_ROOM_GRACE`).

## The numbers, once every 15 seconds per direction

```
[talk] Phone audio over 15s: 751 packets, 0 silence-filled, 0 dropped, peak 9913 (before agc), agc gain 1.0x
[talk] Talk audio for <call-id> over 15s: 748 frames, peak 3950
```

| Reading | Healthy | What a deviation means |
|---|---|---|
| `packets` | ~50 per second, so ~750 per interval | Below that, audio is not arriving from the gateway |
| `silence-filled` | 0 | The track ran dry and silence was inserted: the stream is late or gone |
| `dropped` | 0 in steady state, a burst at the start is normal | Audio arriving faster than real time and thrown away to hold latency down - heard as chopping |
| `peak` (phone side) | ~10000 while someone speaks, tens in silence | Stuck at tens throughout: the phone side is mute |
| `frames` (Talk side) | ~750 per interval | Below that, Talk's audio is not arriving |
| `peak` (Talk side) | thousands while someone speaks, under ~20 in silence | **Frames arriving with peak under 20 for the whole call is a muted microphone, not a broken path** - the distinction that is otherwise invisible |

The interval is `BRIDGE_AUDIO_REPORT_INTERVAL`; per second these two lines
were four fifths of everything the daemon ever logged and buried the lines
above. Set it to 1 while chasing something that changes within a second.

## What the measurement says

The same call, measured by `tests/hardware/test_audio_quality.py` rather
than by ear:

```
440 Hz  arrived as  439.5 Hz in 3 tones, worst distortion -50.7 dB, level moves up to 0.3 dB while steady, 50 Hz sidebands -60.0 dB
660 Hz  arrived as  660.4 Hz in 3 tones, worst distortion -45.6 dB, level moves up to 0.3 dB while steady, 50 Hz sidebands -60.7 dB
880 Hz  arrived as  880.2 Hz in 3 tones, worst distortion -41.9 dB, level moves up to 0.2 dB while steady, 50 Hz sidebands -58.3 dB
PASS  every tone reached the Talk side at its own frequency
PASS  no modulation at the packet rate

8.0s of audio came back from Talk
8 of 8 seconds carry signal above -45 dBFS
PASS  audio is flowing from Talk back to the phone
```

The three numbers to compare against:

- **Frequency** within a couple of Hz. A tone arriving 0.5 % high means the
  timeline is being stretched somewhere - a resampler, or packets lost and
  concatenated.
- **Sidebands at the packet rate below -40 dB.** Above that, something acts
  once per packet and it is audible as a low ringing on anything steady.
- **Level swing under 1 dB while a tone is steady.** Several dB means gain
  control hunting or the audio being chopped.

## Two things the journal cannot tell you

- **Answer in a browser.** Talk Android 25.0.0 opens dozens of signaling
  connections when answering an incoming call, all but one idle; the
  session that publishes the microphone is replaced moments later and
  nothing it sends arrives. 24.0.4 behaves correctly.
- **A participant count that disagrees with the database is the client's
  own bookkeeping**, not a call that failed to tear down - see the stale
  in-call list in [`SIGNALING-API.md`](./SIGNALING-API.md), which no
  released signaling server has the fix for.
