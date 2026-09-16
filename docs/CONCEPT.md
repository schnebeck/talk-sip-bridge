# Concept

## Goal

Replace the PoC's custom chat-bot UI (`/accept`, `/decline`, `/dial` commands
in a Talk room) with Nextcloud Talk's native, documented SIP bridge protocol:
incoming phone calls appear as real, named "phone" participants in a room,
and outgoing calls are triggered from Talk's own call UI, not a chat command.

## Reference

Protocol: https://nextcloud-spreed-signaling.readthedocs.io/en/latest/standalone-signaling-api-v1/
("Internal clients", "Dialout session", "Start dialout from a room",
"Add/update/remove virtual session" sections).

No reference implementation exists publicly (neither in the
`nextcloud-spreed-signaling` repository itself, which contains only a Go
benchmarking client, nor anywhere else found on GitHub) - Nextcloud's own
SIP bridge product is closed-source. This is built directly against the
protocol documentation, the same way the PoC's internal-client mechanism was
built against Go source and trial and error before any docs were found for
it.

## What carries over from `sip-fritzbox-experiment` (proven, reusable)

- FritzBox connectivity: Kamailio SIP relay, `rtp_relay.py` media relay,
  OpenVPN routing via the backup server (all of `NETZWERK.md`).
- SIP client core: REGISTER/INVITE/digest auth/RTP session handling
  (`rtp.py`, `g711.py`, SIP header parsing).
- Internal-client WebSocket handshake and WebRTC publish/subscribe
  (`aiortc`-based), including the previously undocumented `backend` hello
  param and the ICE trickle-candidate handling.
- FFT-based audio verification methodology for testing.

## Unlocking the native UI in Talk

All native SIP/dialout endpoints (`POST /call/{token}/dialout/{attendeeId}`,
`verify-dialout`, etc.) are gated behind `Config::isSIPConfigured()` /
`isSIPDialOutEnabled()`, found in `custom_apps/spreed/lib/Config.php`. Three
`spreed` app config values, settable via `occ config:app:set`:

- `sip_bridge_shared_secret` - any non-empty secret (used for the separate
  REST `/signaling/settings` SIP-bridge auth path, not the WS-level
  `dialout` mechanism itself, but required for `isSIPConfigured()`).
- `sip_bridge_dialin_info` - must be non-empty (free text shown to users;
  content irrelevant to us since we don't use real dial-in phone numbers).
- `sip_dialout` - must not be `no` (e.g. `yes`) to satisfy
  `isSIPDialOutEnabled()`.
- `sip_bridge_groups` (optional) - restricts which groups can enable SIP
  per-room; empty means all users. Worth setting to an admin/test group
  while this is unfinished, so the new UI doesn't appear for regular users
  before the daemon side actually works.

**Applied on the production server** (2026-09-16): `sip_bridge_groups` set to
a new `sip-testers` group containing only `sip-tester`, `sip_bridge_dialin_info`
set to a placeholder string, `sip_dialout` set to `yes`, and
`sip_bridge_shared_secret` set to a freshly generated secret (value is in the
production `spreed` app config only, not written down here). The native
"call a phone number" UI is now visible only to `sip-tester`.

## What's new here

1. **Config, not hardcoded constants.** All IPs/ports/secrets come from a
   config file, not Python module-level constants. The relay path (via the
   backup server) becomes one connectivity mode among others, not a fixed
   assumption - a deployment where the phone gateway is directly reachable
   needs no relay at all.
2. **Native dialout integration.** Verified working end to end through
   Talk's own "call a phone number" UI.
   - Declare `"features": ["start-dialout"]` in the hello message.
     Deliberately not `"internal-incall"`: that tells the server the client
     manages its own inCall/publishing-audio flags, which this bridge does
     not do - without it, the server sets both automatically on connect,
     which the self-addressed WebRTC publish below relies on.
   - A dialout request arrives as `{"id": "...", "type": "internal",
     "internal": {"type": "dialout", "dialout": {"roomid": "...", "backend":
     "...", "request": {"number": "...", "options": {...}}}}}` - the room id
     is read from this message, not assumed. The reply must echo the same
     `id`, wrapped the same way, with `dialout: {"type": "status", "roomid":
     ..., "status": {"callid": ..., "status": "accepted"}}` (or `"type":
     "error"` with an `error` object) - a flat `{"type": "dialout", ...}` at
     the top level, with no `internal` wrapper or `id` echo, is silently
     ignored by the signaling server.
   - Nextcloud's own number validation (`libphonenumber`, not configurable)
     runs before any request reaches this bridge at all, and Talk formats
     any number a user enters as E.164 - see `docs/CONFIG.md`'s
     `BRIDGE_DIALOUT_STRIP_PREFIX` / `BRIDGE_DIALOUT_INTERNAL_DIAL_PREFIX`
     for how a real, syntactically valid number gets mapped back to the
     gateway's own internal-extension dial notation.
   - This replaces the PoC's `/dial` chat command with Talk's own "call a
     phone number" UI.
3. **Virtual sessions for calls.** Represent each phone call as its own
   session via `addsession`/`updatesession`/`removesession`, with
   `user.type = "phone"`, `callid`, and `number` - so a caller shows up as a
   real, named participant instead of anonymous published audio. Replaces
   the PoC's chat messages for call state. `addsession` alone does not
   route audio anywhere - the bridge also joins the room itself
   (`{"type": "room", "room": {"roomid": ...}}`) so the signaling server
   routes its self-addressed WebRTC offer to that room's Janus instance;
   verified via `bridge/test_publish_and_verify.py`. Joining a room
   permanently drops the session from the signaling server's dialout
   candidates for the lifetime of that WebSocket connection - confirmed in
   its own source, there is no code path that restores it, including on
   leaving the room again. The only way to regain dialout eligibility is a
   fresh connection with a new hello, so the bridge deliberately closes and
   reconnects once a call ends (handled by the existing reconnect loop in
   `_connect_and_serve`) - fine given only one call is ever handled at a
   time.
4. **Call-end detection.** Ending a call in Talk's UI does not tear down the
   bridge's own publisher - confirmed via the signaling server's own logs,
   only the human's own publisher/room gets destroyed - so watching our own
   `RTCPeerConnection`'s ICE/connection state never reliably detects it (kept
   as a secondary check for other teardown paths, e.g. the browser tab
   closing outright). The actual, reliable signal is the signaling server's
   room-wide `{"type": "event", "event": {"target": "participants", "type":
   "update", "update": {"roomid": ..., "incall": 0, "all": true}}}`
   broadcast (`Room.PublishUsersInCallChangedAll` server-side) - sent to
   every room member, including the bridge since it joins the room to
   publish, whenever the call itself ends for the whole room. `incall` uses
   the server's `FlagInCall = 1` bit; `incall & 1 == 0` means the call has
   ended. Two other, narrower shapes of the same `participants`/`update`
   event exist server-side (a `changed` delta from backend-driven "incall"
   updates, and a full `users` room-membership snapshot from
   `NotifySessionChanged`) and are also handled, though the `all: true`
   broadcast is what an actual "Anruf beenden" click in Talk sends. The
   virtual "phone" session added via `addsession` gets its own
   server-assigned room session id (announced via a `room`/`join` event,
   matched back to the call via its `user.callid`) that is unrelated to the
   name chosen when adding it - needed to recognize the bridge's own virtual
   session in room-roster snapshots rather than mistaking it for another
   participant still on the call.
5. **Multi-call daemon.** The PoC's standalone audio-bridge script handles
   exactly one call and then exits; the production daemon needs to keep
   registering and accepting new calls indefinitely (the always-on
   `sipbridge.service` already does this for signaling-only calls - this
   extends that, not the one-shot `audio-bridge/` scripts).
6. **Real audio in the persistent daemon.** The PoC's signaling-only daemon
   (`AUTO_BYE_SECONDS` safety net, no real audio) and its separate
   audio-bridge proof of concept get merged into one daemon that both
   registers continuously and carries real audio.
7. **Packaging.** A `custom_apps`-installed Nextcloud app for admin settings
   (status/config), installed and updated via `occ app:*`.
8. **Localization.** Any Nextcloud UI component (admin settings page, JS)
   uses English as the source language (`$l->t()` / `t()` calls with English
   strings), with a generated German translation in `l10n/de.json` /
   `l10n/de.js` - Nextcloud's standard i18n mechanism, not hardcoded German
   text in the templates.
9. **Codec negotiation (G.722/"HD-Telefonie", PCMU fallback).** The SIP side
   offers both (`sip_core.py`'s `_offer_sdp`/`_answer_sdp`), preferring
   G.722 - real 16kHz audio despite SDP historically labeling it
   `G722/8000` (see `g722.py`). `rtp.py`'s `RtpSession` is codec-agnostic
   past construction (`set_payload_type`), and `talk_client.py`'s
   `SipAudioTrack` resamples from whatever rate was actually negotiated.
10. **Bidirectional audio.** `_publish_call_audio` covers only the phone-to-
    Talk direction (a self-addressed WebRTC publish, see above). The
    opposite direction subscribes to the human room participant's own audio
    via a `requestoffer`/offer/answer/candidate exchange
    (`_subscribe_human_audio`) and relays decoded frames into the RTP
    session (`_relay_human_audio`), resampled to the call's negotiated
    codec rate. The room roster needed to find "the human" to subscribe to
    is tracked from `room`/`join`/`leave` events (`_room_roster`).
11. **Automatic gain control.** Phone-side audio only (`agc.py`, applied in
    `SipAudioTrack`) - some handsets (e.g. a DECT cordless) have a much
    quieter microphone than a laptop/headset, with no way to adjust that
    from this end of the call. The human-to-phone direction is left alone:
    browsers already apply their own mic AGC by default, and a second one
    on top risks audible "pumping".
