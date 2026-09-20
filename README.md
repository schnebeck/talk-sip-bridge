# FritzBox Talk Bridge

Connects a FritzBox phone line to Nextcloud Talk using Talk's native SIP
bridge protocol: incoming calls appear as real, named "phone" participants
in a room, and outgoing calls are placed from Talk's own call UI.

See `docs/SIGNALING-API.md` for the Talk signaling and OCS interface this
builds on, `docs/CONCEPT.md` for the architecture, `docs/CONFIG.md` for
configuration, and `deploy/README.md` for installation.

## Tests

`tests/` needs no phone gateway, no signaling server and no network:

```
python3 -m unittest discover -s tests -t .
```

It covers that every module imports on its own (`test_build.py`), that the
calls modules make into each other exist there (`test_api.py`, read from the
source, so paths that only a hangup or a timeout reaches are covered too),
and what the message, SDP and request-building functions compute. Tests
needing the media stack (numpy, av, aiortc) skip themselves where it is not
installed; run the suite in the deployment venv for the full set.

`tests/hardware/` is the other kind: scripts that place real calls against a
gateway, a signaling server, or the Asterisk test peer. They are run by hand
and are not part of the suite above.

Tests live outside `bridge/` and are installed - or removed - separately from
the daemon; `bridge/` carries the runtime and nothing else. See
[`tests/README.md`](./tests/README.md).

`test-peer/` is an Asterisk in a container to point the bridge at instead of
the FritzBox, so that "works with the FritzBox" and "speaks SIP" stay
distinguishable. It runs only while a test needs it.

### Which of these to run

What a change can break, not everything every time - the cost of a check
should stay below the cost of the change it guards.

| Changed | Worth running |
|---|---|
| `sip_messages`, `sip_sdp`, `sip_requests`, `payload_types` | `tests/` - sub-second, no setup |
| A module boundary: new module, moved code, changed signature | `tests/` - `test_build` and `test_api` are what catch it |
| `sip_call`, `sip_registrar`, `sip_transport` | `tests/`, then `test_peer_outbound.py` / `test_peer_inbound.py` against the test peer |
| `rtp`, `g711`, `g722`, `agc` | `test_audio_quality.py`, and `test_audio_over_sip.py` for the real phone path |
| `room_state`, `talk_ocs`, `media`, `call`, `call_media` | `tests/` - covered offline, including against recorded signaling traffic |
| `talk_client` | `tests/`, then `tests/hardware/test_publish_and_verify.py` against a signaling server - it publishes a tone into a room and checks it by FFT, with no phone involved |
| `sip_registrar` | `tests/`, then `tests/hardware/test_registration_recovery.py` - stops the test peer mid-flight and checks the line returns on its own |
| Anything an inbound call touches | `tests/hardware/test_call_lifecycle.py <sip-phone2-password>` - places a real call at the running bridge and asserts ringing, answering and teardown without anybody present |
| Deployment, config, the relay host | A real call; nothing offline covers that path |

## Status

- `bridge/` - the daemon: gateway registration, bidirectional real audio
  (G.722 preferred, PCMU fallback, with automatic gain control on the phone
  side) for inbound and outbound calls, native Talk dialout integration,
  reliable call-end detection, local HTTP control API (status/toggle).
  Verified against the production gateway and signaling server, including
  real phone calls and an independent FFT-verified audio check
  (`bridge/test_publish_and_verify.py`).
- `nextcloud-app/fritzboxbridge/` - admin settings page (status/toggle),
  installed and verified on the production Nextcloud instance.
- `relay/` - for a gateway the bridge cannot reach directly: `sip_pipe.py`
  carries SIP between the two networks and `rtp_relay.py` the media. Only
  needed for that case; a directly reachable gateway needs neither.
- Deployed as the primary bridge (`deploy/`), running as the
  `fritzbox-talk-bridge` systemd service.
