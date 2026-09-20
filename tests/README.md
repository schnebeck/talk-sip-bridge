# Tests

Installed and removed separately from the daemon. Nothing here is needed to
run the bridge, and removing all of it changes nothing about a deployment:

```
rm -rf /opt/fritzbox-talk-bridge-tests
```

The code under test is found through `BRIDGE_CODE`, or - without it - in the
sibling `bridge/` of a checkout. That is what keeps the two installs
independent of each other.

## The offline suite

No phone gateway, no signaling server, no network, no sockets.

```
python3 -m unittest discover -s tests -t .                       # in a checkout
BRIDGE_CODE=/opt/fritzbox-talk-bridge \
    python3 -m unittest discover -s tests -t .                   # against an install
```

| File | Covers |
|---|---|
| `test_build.py` | Every module imports on its own, in a fresh interpreter, with only the environment it declares; and that SDP and message building stay clear of the media stack |
| `test_api.py` | The calls modules make into each other exist there - read from the source, so paths only a hangup or a timeout reaches are covered too |
| `test_messages.py` | Header parsing, digest authentication, the injection allowlist |
| `test_sdp.py` | Offer and answer building, codec choice, media-address parsing |
| `test_requests.py` | The shape of every SIP message this bridge sends |
| `test_room_state.py` | Who is in a room's call, replayed from recorded signaling traffic |
| `test_talk_ocs.py` | The OCS call sequence, against a recording opener |
| `test_media.py` | Resampling between the call's rate and Talk's 48kHz |

Tests that need the media stack (numpy, av, aiortc) skip themselves where it
is absent, so the suite is meaningful on a bare interpreter and complete in
the deployment venv.

### fixtures/

Real signaling traffic, captured during real calls and anonymised: session
ids replaced by the role they had, user names removed, chat payloads reduced
to their envelope. They are inputs. What the tests assert is the decision
taken from them, never the bytes we send - a recording of our own output
would only pin today's behaviour, bugs included.

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

## Installing them next to a deployment

```
install -d /opt/fritzbox-talk-bridge-tests
cp -r tests /opt/fritzbox-talk-bridge-tests/
cd /opt/fritzbox-talk-bridge-tests
BRIDGE_CODE=/opt/fritzbox-talk-bridge \
    /opt/fritzbox-talk-bridge-venv/bin/python3 -m unittest discover -s tests -t .
```

The venv belongs to the daemon and is shared; the tests add no dependencies
of their own.
