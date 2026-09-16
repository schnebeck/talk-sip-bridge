# FritzBox Talk Bridge

Connects a FritzBox phone line to Nextcloud Talk using Talk's native SIP
bridge protocol: incoming calls appear as real, named "phone" participants
in a room, and outgoing calls are placed from Talk's own call UI.

See `docs/CONCEPT.md` for the architecture, `docs/CONFIG.md` for
configuration, and `deploy/README.md` for installation.

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
- Not yet deployed as the primary bridge - runs alongside the
  `sip-fritzbox-experiment` PoC's daemon during evaluation, not in place of
  it.
