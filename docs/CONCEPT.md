# Concept

## Goal

Incoming phone calls appear as real, named "phone" participants in a Talk
room; outgoing calls are placed from Talk's own native call UI ("call a
phone number"), using Nextcloud Talk's documented standalone-signaling SIP
bridge protocol.

## Reference

Protocol: https://nextcloud-spreed-signaling.readthedocs.io/en/latest/standalone-signaling-api-v1/
("Internal clients", "Dialout session", "Start dialout from a room",
"Add/update/remove virtual session" sections).

No reference implementation exists publicly (neither in the
`nextcloud-spreed-signaling` repository itself, which contains only a Go
benchmarking client, nor anywhere else found on GitHub) - Nextcloud's own
SIP bridge product is closed-source. This is built directly against the
protocol documentation. The internal-client `hello` handshake also requires
a `backend` param (the Nextcloud instance URL) that isn't mentioned in that
documentation - found only by reading the signaling server's own Go source.

## Unlocking the native UI in Talk

All native SIP/dialout endpoints (`POST /call/{token}/dialout/{attendeeId}`,
`verify-dialout`, etc.) are gated behind `Config::isSIPConfigured()` /
`isSIPDialOutEnabled()`, found in `custom_apps/spreed/lib/Config.php`. Three
`spreed` app config values, settable via `occ config:app:set`:

- `sip_bridge_shared_secret` - any non-empty secret (used for the separate
  REST `/signaling/settings` SIP-bridge auth path, not the WS-level
  `dialout` mechanism itself, but required for `isSIPConfigured()`).
- `sip_bridge_dialin_info` - must be non-empty (free text shown to users;
  content irrelevant here since real dial-in phone numbers aren't used).
- `sip_dialout` - must not be `no` (e.g. `yes`) to satisfy
  `isSIPDialOutEnabled()`.
- `sip_bridge_groups` (optional) - restricts which groups can enable SIP
  per-room; empty means all users. Worth setting to an admin/test group
  while evaluating, so the native UI doesn't appear for regular users
  before the daemon side is verified working.

**Applied on the production server**: `sip_bridge_groups` set to a
`sip-testers` group containing only `sip-tester`, `sip_bridge_dialin_info`
set to a placeholder string, `sip_dialout` set to `yes`, and
`sip_bridge_shared_secret` set to a generated secret (value is in the
production `spreed` app config only, not written down here). The native
"call a phone number" UI is visible only to `sip-tester`.

## Architecture

1. **Config, not hardcoded constants.** All IPs/ports/secrets come from a
   config file, not Python module-level constants. The relay path (via the
   backup server) is one connectivity mode among others, not a fixed
   assumption - a deployment where the phone gateway is directly reachable
   needs no relay at all (`config.media_relay_enabled`).
2. **Native dialout integration**, through Talk's own "call a phone number"
   UI.
   - Declare `"features": ["start-dialout", "internal-incall"]` in the hello
     message. `internal-incall` puts this client in charge of its own inCall
     flags: it announces `FLAG_IN_CALL | FLAG_WITH_AUDIO` only once its
     publisher exists, and clears them when the call ends. Without it the
     server sets both on connect, so Talk clients ask this session for an
     audio stream while it is merely watching a room for an accept, get
     `client_not_found`, and then back off to one retry every 10 seconds -
     which costs a real call its first seconds of audio.
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
3. **Virtual sessions for calls.** Each phone call is represented as its own
   session via `addsession`/`updatesession`/`removesession`, with
   `user.type = "phone"`, `callid`, and `number` - so a caller shows up as a
   real, named participant, and dialout status/hangup can be correlated by
   call id. It is a name plate only: in MCU mode a virtual session can never
   carry media, because the signaling server looks a publisher up by the raw
   recipient session id with no virtual-to-owner mapping, and publishers only
   ever exist under a real client session's own id. Its flags are therefore
   set to `FLAG_IN_CALL | FLAG_WITH_PHONE` explicitly, without
   `FLAG_WITH_AUDIO` - Talk clients only request a stream from a participant
   carrying audio or video, and pointing them at a session that cannot
   publish leaves them retrying forever against a silent tile. The call's
   audio rides on the bridge's own session instead, labelled with the
   caller's name through the publish offer's `nick`.
   `addsession` alone does not route audio anywhere - the bridge also joins
   the room itself (`{"type": "room", "room": {"roomid": ...}}`) so the
   signaling server routes its self-addressed WebRTC offer to that room's
   Janus instance; verified via `bridge/test_publish_and_verify.py`. Joining
   a room permanently drops the session from the signaling server's dialout
   candidates for the lifetime of that WebSocket connection - confirmed in
   its own source, there is no code path that restores it, including on
   leaving the room again. The only way to regain dialout eligibility is a
   fresh connection with a new hello, so the bridge deliberately closes and
   reconnects once a call ends (handled by the existing reconnect loop in
   `_connect_and_serve`) - fine given only one call is ever handled at a
   time.
4. **Call-end detection.** Ending a call in Talk's UI does not tear down the
   bridge's own publisher - confirmed via the signaling server's own logs,
   only the human's own publisher/room gets destroyed - so watching the
   bridge's own `RTCPeerConnection`'s ICE/connection state never reliably
   detects it (kept as a secondary check for other teardown paths, e.g. the
   browser tab closing outright). The actual, reliable signal is the
   signaling server's room-wide `{"type": "event", "event": {"target":
   "participants", "type": "update", "update": {"roomid": ..., "incall": 0,
   "all": true}}}` broadcast (`Room.PublishUsersInCallChangedAll`
   server-side) - sent to every room member, including the bridge since it
   joins the room to publish, whenever the call itself ends for the whole
   room. `incall` uses the server's `FlagInCall = 1` bit; `incall & 1 == 0`
   means the call has ended. Two other, narrower shapes of the same
   `participants`/`update` event exist server-side (a `changed` delta from
   backend-driven "incall" updates, and a full `users` room-membership
   snapshot from `NotifySessionChanged`) and are also handled, though the
   `all: true` broadcast is what an actual "Anruf beenden" click in Talk
   sends. The virtual "phone" session added via `addsession` gets its own
   server-assigned room session id (announced via a `room`/`join` event,
   matched back to the call via its `user.callid`) that is unrelated to the
   name chosen when adding it - needed to recognize the bridge's own virtual
   session in room-roster snapshots rather than mistaking it for another
   participant still on the call.
5. **Persistent daemon.** The daemon registers with the gateway and accepts
   new calls indefinitely - both signaling and real audio are handled by the
   same long-running process, not a one-shot script.
6. **Packaging.** A `custom_apps`-installed Nextcloud app for admin settings
   (status/config), installed and updated via `occ app:*`.
7. **Localization.** Any Nextcloud UI component (admin settings page, JS)
   uses English as the source language (`$l->t()` / `t()` calls with English
   strings), with a generated German translation in `l10n/de.json` /
   `l10n/de.js` - Nextcloud's standard i18n mechanism, not hardcoded German
   text in the templates.
8. **Codec negotiation (G.722/"HD-Telefonie", PCMU fallback).** The SIP side
   offers both (`sip_core.py`'s `_offer_sdp`/`_answer_sdp`), preferring
   G.722 - real 16kHz audio despite SDP historically labeling it
   `G722/8000` (see `g722.py`). `rtp.py`'s `RtpSession` is codec-agnostic
   past construction (`set_payload_type`), and `talk_client.py`'s
   `SipAudioTrack` resamples from whatever rate was actually negotiated.
9. **Bidirectional audio.** `_publish_call_audio` covers the phone-to-Talk
   direction (a self-addressed WebRTC publish, see above). The opposite
   direction subscribes to the human room participant's own audio via a
   `requestoffer`/offer/answer/candidate exchange (`_subscribe_human_audio`)
   and relays decoded frames into the RTP session (`_relay_human_audio`),
   resampled to the call's negotiated codec rate. The `requestoffer` is
   repeated until an offer arrives: the other side's publisher may not exist
   yet, and the signaling server rejects such a request with
   `client_not_found` rather than queuing it. Who to subscribe to is the
   session id that accept detection saw entering the call; the room roster
   (`_room_roster`, tracked from `room`/`join`/`leave` events) is only the
   fallback for dialout calls, and it can hold sessions that dropped without
   the server ever sending a `leave` for them.
10. **Automatic gain control.** Phone-side audio only (`agc.py`, applied in
    `SipAudioTrack`) - some handsets (e.g. a DECT cordless) have a much
    quieter microphone than a laptop/headset, with no way to adjust that
    from this end of the call. The human-to-phone direction is left alone:
    browsers already apply their own mic AGC by default, and a second one
    on top risks audible "pumping".
11. **Multiple lines.** Each SIP account/number (`config.LineConfig`) gets
    its own `SipTransport`/`SipRegistrar`/`CallManager` (still one call at a
    time per line, as before) and its own dedicated `TalkClient` - i.e. its
    own connection to the signaling server, not a shared one. That's
    necessary, not just simpler: joining a room to publish a call's audio
    makes an internal-client connection ineligible for new dial-out
    requests for as long as it stays open (point 3 above); lines sharing
    one connection would make each other's dial-out unavailable whenever
    either has a call in progress. `daemon.py` starts one full set per
    configured line; `control_api.py` aggregates their status and accepts
    an optional `?line=<id>` on `/toggle` and `/hangup` (defaulting to the
    first line, so a single-line deployment is unaffected). Lines are
    independent of the registrar/gateway they point at, so a deployment
    could mix e.g. a FritzBox line and an Asterisk line.
12. **Ring signal, for a line where a human should get a chance to answer
    in Talk.** The signaling protocol has no ringing/accept-decline
    exchange for inbound calls, so there's no native way to ask before
    picking up - `LineConfig.notify_user`/`notify_app_password` build one:
    `on_incoming_call` (when `BRIDGE_AUTO_ANSWER` is off) signs into that
    Nextcloud account and calls Talk's own OCS call API
    (`_talk_ring_start_sync`) - `POST .../room/{token}/participants/active`
    to establish a room session, then `POST .../call/{token}` to join the
    call - the same two calls a real Talk client makes to start a call, so
    it triggers real ringing (push, full-screen call UI) on every other
    device logged into that account or already in the room, not just a
    chat message or a bare notification. Confirmed live that skipping the
    room-join step and calling the call endpoint directly fails with 404
    (`RequireParticipant`) - a bare custom Nextcloud endpoint was tried
    first here and abandoned after extensive testing surfaced a routing bug
    specific to this instance's `fritzboxbridge` app (all its POST/GET
    routes except one returning a bare 405 straight from Symfony's router,
    root cause not identified); Talk's own OCS API sidesteps it entirely.

    The triggering session is deliberately kept open (its cookies/opener
    stored on the call entry) rather than immediately left - leaving right
    away would end the call's ring before another device had a chance to
    answer. The bridge also joins the room itself over its own internal-
    client connection (same mechanism as `_publish_call_audio`'s room-join)
    purely to watch `participants`/`update` events for a *different* real
    session joining the call - `_handle_participants_update` handles this
    symmetrically to its existing call-end detection, checking for the
    opposite inCall transition, deliberately excluding both its own
    session and the ring-trigger session's id (the "all: true" room-wide
    broadcast is excluded from this check entirely, since the ring-trigger
    joining a previously-callless room is itself what causes that specific
    broadcast). On a genuine accept, the bridge leaves the ring-trigger
    session and calls `CallManager.answer()`. If a different device
    answers instead (e.g. a physical phone in the same FritzBox
    parallel-ring group), the gateway cancels this INVITE as usual
    (`CallManager.handle_cancel`), which leaves the ring-trigger session
    the same way. `_handle_incoming_ring`'s internal-client room-join has
    the same dial-out-eligibility cost as a real call's publish (point 3)
    and is cleaned up the same way (a forced reconnect once the call ends,
    whether accepted or cancelled).
