<!--
talk-sip-bridge - tests/README.md
What the tests are, which of them need hardware, and how to run them.

  Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
  Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
  Written by Anthropic Claude Opus 5 - AI generated content.

  Free software under the GNU General Public License, version 3 or later.
  There is no warranty, to the extent permitted by law. The full text is
  in LICENSES/GPL-3.0-or-later.txt.

SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
SPDX-FileContributor: Anthropic Claude Opus 5 (AI generated content)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Tests

What each test covers, and what each hardware script needs. How they are
built - the three dependency tiers, `tests/support.py`, where a fake is
allowed, and what a new test has to do to fit - is
[`docs/TESTING.md`](../docs/TESTING.md).

Installed and removed separately from the daemon. Nothing here is needed to
run the bridge, and removing all of it changes nothing about a deployment:

```
rm -rf /opt/talk-sip-bridge-tests
```

The code under test is found through `BRIDGE_CODE`, or - without it - in the
sibling `bridge/` of a checkout. That is what keeps the two installs
independent of each other.

## The offline suite

No phone gateway, no signaling server, no network, no sockets.

Two of them read the source rather than run it, and each needs one tool
the rest do not - `ruff` and `reuse`, both in `tests/requirements.txt`.
Each skips itself when its own tool is absent.

`test_static.py` is the only check that sees a name missing *inside* a
function, which is a module that imports perfectly and fails the moment
that line runs; the rules it applies are in `ruff.toml` at the top of the
repository. It also covers `tests/hardware/`, which the suite never runs
- but only for names. A method that *moved* is `client._hello()`, a
perfectly good attribute access on a name that exists, and no static
check of the file it is written in can know better. That one is
`test_api.py`'s `HardwareScriptApiTest`, which asks every class in
`bridge/` whether it has the attribute.

`test_headers.py` is the only check that reads what the files say about
themselves: that each one names its own path, states a licence, and - for
Python - still describes itself the way its module docstring does.

```
python3 -m unittest discover -s tests -t .                       # in a checkout
BRIDGE_CODE=/opt/talk-sip-bridge \
    python3 -m unittest discover -s tests -t .                   # against an install
```

| File | Covers |
|---|---|
| `test_build.py` | Every module imports on its own, in a fresh interpreter, with only the environment it declares; and that SDP and message building stay clear of the media stack |
| `test_api.py` | The calls modules make into each other exist there - read from the source, so paths only a hangup or a timeout reaches are covered too |
| `test_messages.py` | Header parsing, digest authentication, the injection allowlist |
| `test_sdp.py` | Offer and answer building, codec choice, media-address parsing, key-press negotiation |
| `test_dtmf.py` | One digit per key press out of the packet storm one press produces |
| `test_dtmf_inband.py` | Reading key presses out of the audio, including against a real handset's recorded tones - and above all what must *not* be read as a key |
| `test_requests.py` | The shape of every SIP message this bridge sends |
| `test_framing.py` | Where one message ends and the next begins in a TCP stream |
| `test_transport.py` | Which connection a SIP message is written to, including when the one it should use is gone |
| `test_gateway_messages.py` | The same parsers against messages a real gateway sent, not ones written to be parsed |
| `test_room_state.py` | Who is in a room's call, replayed from recorded signaling traffic |
| `test_room_roster.py` | Who in a room is a person to take audio from, and who is a phone or one of this bridge's own connections |
| `test_talk_messages.py` | The shape of every signaling message this bridge sends |
| `test_talk_ocs.py` | The OCS call sequence, against a recording opener |
| `test_dialout.py` | Reading a dialout request, the dial plan, the reply Talk gets, and which of the two connections each message goes out on |
| `test_registrar.py` | Registered versus meant-to-be-registered, and recovery from a failed refresh |
| `test_call.py` | The per-call state the Talk side accumulates |
| `test_call_media.py` | Which connection a media message belongs to, and teardown of both |
| `test_ws_dump.py` | Reading signaling messages back out of a capture - masked frames, split frames, two in one packet |
| `test_media.py` | Resampling between the call's rate and Talk's 48kHz |
| `test_mixer.py` | Summing several participants into the one stream a phone can carry: the ring buffer's wrap, underrun and overrun; the gate; the limiter's look-ahead; and the two properties the module promises - the same calls give the same samples, and writers racing a reader corrupt nothing |
| `test_answer.py` | Answering an inbound call: the codec chosen, and where the caller's audio is actually sent |
| `test_hangup.py` | Every way a call can end, in both directions - and which SIP message each of them is |
| `test_subscription.py` | The negotiation for another participant's audio, transition by transition |
| `test_subscription_properties.py` | The same machine against thousands of sequences nobody wrote down |
| `test_dialout_teardown.py` | A dialout that never connected: what has to be taken back, and on which connection |
| `test_phone_participant.py` | What the bridge announces about a call it is carrying - which name, to whom, with which state |
| `test_signaling_loop.py` | The two connections staying up: a message that cannot be handled costs only itself, a loop that ends is started again |
| `test_resilience.py` | The same promise for the media receiver and the registration keepalive, where a dead thread is invisible from everywhere else |
| `test_sip_bridge_api.py` | Talk's SIP endpoints as this bridge calls them, and which number reaches which conversation |
| `test_empty_room.py` | A room that looks empty for a moment - saving a microphone setting is a leave and a join, not the end of a conversation |
| `test_late_joiner.py` | Somebody joining after the phone is already publishing - the dial-in case, where the caller reaches an empty room |
| `test_static.py` | What the suite cannot see: names used but never defined, imports never used, and the layout rules in `ruff.toml` |
| `test_headers.py` | Every file states its licence: a header naming the file it sits in, or an entry in `REUSE.toml` for the ones that cannot carry one. A Python header also has to still say what its module docstring says |

Tests that need the media stack (numpy, av, aiortc) skip themselves where it
is absent, so the suite is meaningful on a bare interpreter and complete in
the deployment venv.

### fixtures/

Real signaling traffic, captured during real calls and anonymised: session
ids replaced by the role they had, user names removed, chat payloads reduced
to their envelope. They are inputs. What the tests assert is the decision
taken from them, never the bytes we send - a recording of our own output
would only pin today's behaviour, bugs included.

`fixtures/fritzbox/` is the SIP side of the same idea: whole messages the
FRITZ!Box this bridge is deployed against actually sent, taken off the
wire. Only what the gateway itself originated belongs there - a capture
also holds our own replies and anything a proxy in the path generated, and
neither says what a gateway does. `Server:`, `User-Agent:` and a tag this
bridge would have made are what tell them apart, not the direction of
capture. A
handset's display name is replaced and the gateway's firmware version is
dropped from its `User-Agent`; the addresses are the same RFC1918 ones the
rest of this repository uses as examples, and no message in the set carries
an `Authorization` header, so there is no digest response to attack. They
are stored
with their original CRLF line endings, so read them as bytes -
`read_text()` translates the line endings and takes the framing with them.
`dtmf-keypad.wav` belongs to the same set: every key of that gateway's
DECT handset as the bridge received it, cut to the stretches carrying a
tone so no speech is in the file. It is the recording that corrected the
detector - built against textbook tones of equal amplitude, it read one
key in twelve.

The directory name is the disclaimer: one gateway's behaviour, not the
protocol. A second gateway's recordings belong beside it, under its own
name, with the same tests running over both.

## hardware/

The other kind: scripts that place real calls. They are not discovered by
the suite above and are run by hand, each documenting its own environment in
its docstring.

| Script | Needs |
|---|---|
| `test_peer_outbound.py`, `test_peer_inbound.py` | The Asterisk test peer (`../test-peer/`) - a container, no telephony |
| `test_publish_and_verify.py` | A signaling server; publishes a tone into a room and verifies it by FFT, without any SIP call |
| `test_audio_quality.py` | Nothing beyond the media stack: measures what the publish path does to speech |
| `test_audio_over_sip.py`, `test_call_lifecycle.py` | A real gateway and a second SIP account |
| `test_human_call.py` | The same, plus a person to answer in Talk - measures both directions of one call and leaves both as WAV |
| `test_dialin_answer.py` | A running Nextcloud with a mapped dial-in number and `BRIDGE_SIP_SHARED_SECRET` - no gateway and no second account, because `fake_gateway.py` plays the caller |
| `test_dialin_ivr.py` | The same, plus a SIP-enabled conversation whose token is all digits: dials its meeting id, with a PIN if it wants one, and checks that wrong ids end the call |
| `test_talk_to_phone.py` | The other direction, with nobody human in it: `talk_participant.py` publishes a tone into the conversation and the tone is measured in the RTP the caller receives |
| `test_two_publishers.py` | How many participants one call can carry. Two tones play in the room, the bridge is pointed at one, and the caller's audio is searched for both - measured 0 dB and -57.5 dB, so: one |
| `relay_probe.py` | The media relay on its own, two ends on two hosts: a paced stream in on the overlay side, arrival times and losses out on the LAN side |
| `ws_dump.py` | Nothing running: reads signaling messages back out of a `tcpdump` capture, unmasking what the browser sent |
| `send_control.py` | A signaling server: sends a phone one of the control messages Talk's UI sends it - hang up, mute - without a browser |
| `room_listener.py` | The same: a second internal connection that joins a room and prints what the room tells it |

The last three exist for questions no log on one side can answer. Both ends
of every signaling conversation cross one loopback port on the Nextcloud host
in plain text - the reverse proxy decrypts the browser's WSS before it gets
there - so `tcpdump -i lo -s 0 -w cap.pcap port <signaling port>` plus
`ws_dump.py` shows what one participant sent *and* what another was handed.
`send_control.py` and `room_listener.py` then play the other side: they act as
Talk's UI and as this bridge's room connection, so a failure can be pinned to
one of them rather than argued about. A capture contains the internal secret
in the `hello` - delete it when the question is answered.

`test_dialin_answer.py` is the one test here that needs no telephony at
all. A dial-in number has to arrive as a *different* number than the one a
person's phone rings on, which a single line cannot deliver; `fake_gateway.py`
sends the INVITE for it instead, and everything from the CallManager
onwards - the `direct-dial-in` endpoint, the conversation Nextcloud
creates, the answer, the audio - is the deployed path.

`test_human_call.py` needs a second SIP pipe and a second media relay pipe,
since the bridge's line holds the first of each for the call being placed;
its docstring carries the command. Tones shorter than about a second
measure the gain control settling rather than the path, which is why its
are long and it skips their first 350 ms.

## Installing them next to a deployment

```
install -d /opt/talk-sip-bridge-tests
cp -r tests /opt/talk-sip-bridge-tests/
cd /opt/talk-sip-bridge-tests
BRIDGE_CODE=/opt/talk-sip-bridge \
    /opt/talk-sip-bridge-venv/bin/python3 -m unittest discover -s tests -t .
```

The venv belongs to the daemon and is shared; the tests add no dependencies
of their own.
