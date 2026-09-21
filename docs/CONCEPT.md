# Concept

## Goal

Incoming phone calls appear as real, named "phone" participants in a Talk
room; outgoing calls are placed from Talk's own native call UI ("call a
phone number"), using Nextcloud Talk's standalone-signaling SIP bridge
protocol.

## Reference

[`SIGNALING-API.md`](./SIGNALING-API.md) defines the interface this builds on -
message shapes, flags, events, the OCS endpoints, and which parts are
undocumented upstream. This document does not repeat those definitions; it
records what this bridge does with them and why. Configuration is in
[`CONFIG.md`](./CONFIG.md).

## Talk configuration in this deployment

The `spreed` app config values that unlock the native UI are listed in
[`SIGNALING-API.md`](./SIGNALING-API.md#prerequisites-in-nextcloud). As set on
the production server: `sip_bridge_groups` is a `sip-testers` group holding a
single administrator account, so the native "call a phone number" UI is
visible to that one account; `sip_bridge_dialin_info` is a placeholder
string, since no real dial-in numbers are used; `sip_dialout` is `yes`; and
`sip_bridge_shared_secret` is a generated secret held only in the production
app config, not written down here.

Restricting the UI to a test group is worth keeping while a deployment is being
evaluated, so the native call button does not appear for regular users before
the daemon side is verified.

## Architecture

1. **Config, not hardcoded constants.** All IPs/ports/secrets come from a
   config file, not Python module-level constants. The relay path (via the
   backup server) is one connectivity mode among others, not a fixed
   assumption - a deployment where the phone gateway is directly reachable
   needs no relay at all (`config.media_relay_enabled`).
2. **Native dialout integration**, through Talk's own "call a phone number"
   UI rather than a custom chat command.
   - Two connections to the signaling server, one per role: the **dialout
     connection** declares `start-dialout` and never enters a room, the **room
     connection** declares `internal-incall` and carries the call. They cannot
     be one, because a `start-dialout` session that joins any room is dropped
     from the server's dialout candidates and only a fresh hello puts it back.
     Owning the in-call flags is what lets the room connection announce audio
     only once its publisher exists, instead of from the moment it connects -
     the difference is a real call's first seconds of audio.
   - Each request carries the room it is for, so nothing is assumed about which
     room a dialout belongs to.
   - Nextcloud validates any number before the request reaches this bridge at
     all. Mapping a valid-looking number back to the gateway's internal
     extension notation is therefore the bridge's job - see `CONFIG.md`'s
     `BRIDGE_DIALOUT_STRIP_PREFIX` / `BRIDGE_DIALOUT_INTERNAL_DIAL_PREFIX`.
3. **Virtual sessions for calls.** Every phone call gets its own virtual
   session, which is what makes the caller a real, named participant in the
   room and lets dialout status and hangups be correlated by call id.

   Because such a session can never carry media, the call's audio rides on the
   bridge's own session instead. One caller therefore appears as **two**
   participants: the named phone session, and the bridge's own session carrying
   the audio, which shows as "Gast" - an internal client has no display name in
   the protocol, and naming the caller as a real Nextcloud actor is not
   available for someone who is not already invited to the room.

   The virtual session goes up when the call is **answered**, never while a
   dialout still rings: it is announced as being in the call, which stops
   Talk's ringback and leaves the caller in front of a line that looks
   connected and carries nothing.

   Publishing also requires the bridge to join the room itself; verified via
   `bridge/test_publish_and_verify.py`. The room connection joins as soon as
   there is a call - for a dialout that is while it rings, which is what makes
   "end meeting for everyone" reach this bridge at all - and leaves again when
   the call is over. Nothing expires that membership, and a room still holding
   a session is a room Nextcloud counts somebody in.
4. **Call-end detection.** Ending a call in Talk's UI does not tear down this
   bridge's publisher - confirmed in the signaling server's own logs, only the
   human's publisher and room membership are destroyed. Watching the bridge's
   own `RTCPeerConnection` therefore never detects it reliably, and that check
   is kept only as a secondary signal for other teardown paths, such as a
   browser tab closing outright.

   The decision is taken from the room's call membership instead, as a
   transition rather than a state. The bridge tracks its own virtual session's
   server-assigned room session id for this, so it does not mistake itself for
   another participant still on the call.
5. **Persistent daemon.** The daemon registers with the gateway and accepts
   new calls indefinitely - both signaling and real audio are handled by the
   same long-running process, not a one-shot script.

   Whether a line *is* registered and whether it is *meant to be* are two
   different things (`SipRegistrar.registered` / `.wanted`). A gateway that
   reboots costs one refresh; treating that as "switched off" leaves the
   line unreachable until somebody notices, so the keepalive keeps trying
   and reports when the line comes back.
6. **Packaging.** A `custom_apps`-installed Nextcloud app for admin settings
   (status/config), installed and updated via `occ app:*`.
7. **Localization.** Any Nextcloud UI component (admin settings page, JS)
   uses English as the source language (`$l->t()` / `t()` calls with English
   strings), with a generated German translation in `l10n/de.json` /
   `l10n/de.js` - Nextcloud's standard i18n mechanism, not hardcoded German
   text in the templates.
8. **Codec negotiation (G.722/"HD-Telefonie", PCMU fallback).** The SIP side
   offers both (`sip_sdp.py`'s `offer_sdp`/`answer_sdp`), preferring
   G.722 - real 16kHz audio despite SDP historically labeling it
   `G722/8000` (see `g722.py`). `rtp.py`'s `RtpSession` is codec-agnostic
   past construction (`set_payload_type`), and `media.py`'s
   `SipAudioTrack` resamples from whatever rate was actually negotiated.
9. **Bidirectional audio.** Both directions of one call are one object
   (`call_media.py`): a publisher carrying the phone into the room, and a
   subscriber carrying the room back, relaying decoded frames into the RTP
   session at the call's negotiated rate. It knows peer connections and not
   the signaling protocol - what goes on the wire stays on the Talk side,
   and reaches the media as SDP and candidates.

   Who to subscribe to is the session id that accept detection saw entering the
   call. The room roster (`_room_roster`) is only the fallback for dialout
   calls: it can hold sessions that dropped without the server ever announcing
   it, and asking one of those for audio fails.
10. **Automatic gain control.** Phone-side audio only (`agc.py`, applied in
    `media.py`'s `SipAudioTrack`) - some handsets (e.g. a DECT cordless) have a much
    quieter microphone than a laptop/headset, with no way to adjust that
    from this end of the call. The human-to-phone direction is left alone:
    browsers already apply their own mic AGC by default, and a second one
    on top risks audible "pumping".
11. **Multiple lines.** Each SIP account/number (`config.LineConfig`) gets
    its own `SipTransport`/`SipRegistrar`/`CallManager` (still one call at a
    time per line) and its own dedicated `TalkClient` - i.e. its own
    pair of connections to the signaling server, not shared ones. That is
    necessary, not just simpler: a line's room connection is in that line's
    room for the duration of a call, and a session can only be in one room at
    a time, so lines sharing one would evict each other.
    `daemon.py` starts one full set per configured line; `control_api.py`
    aggregates their status and accepts an optional `?line=<id>` on `/toggle`
    and `/hangup` (defaulting to the first line, so a single-line deployment is
    unaffected). Lines are independent of the registrar/gateway they point at,
    so a deployment could mix e.g. a FritzBox line and an Asterisk line.
12. **Ring signal, for a line where a human should get a chance to answer
    in Talk.** The signaling protocol has no ringing or accept-decline exchange
    for inbound calls, so there is no native way to ask before picking up.
    `LineConfig.notify_user`/`notify_app_password` build one out of Talk's own
    OCS call API: `on_incoming_call` (when `BRIDGE_AUTO_ANSWER` is off) signs
    into that Nextcloud account and joins the room's call exactly as a real
    Talk client would (`_talk_ring_start_sync`), which is what makes every
    other device on that account ring for real - push notification and
    full-screen call UI, not a chat message. Talk's own API is used rather than
    a custom endpoint in this repository's Nextcloud app: that app's routes
    return a bare 405 from Symfony's router on this instance, root cause
    unidentified.

    The triggering session is deliberately kept open (its cookies/opener stored
    on the call entry) rather than left immediately - leaving right away would
    end the ring before another device had a chance to answer.

    The bridge also joins the room over its own internal-client connection,
    purely to watch for a *different* real session joining the call - the same
    room model as the call-end detection above, read for the opposite
    transition and ignoring the bridge's own sessions. On a genuine accept it
    answers the SIP call **first** and leaves the ring-trigger session after:
    the caller waits on the answer, not on two OCS round trips.

    Joining the call is not enough on its own to get a person to answer, so the
    bridge additionally rings the room's users for it. If a different device
    wins the race instead - a physical phone in the same FritzBox parallel-ring
    group, say - the gateway cancels this INVITE as usual
    (`CallManager.handle_cancel`), which leaves the ring-trigger session the
    same way. The room is left when the call ends, whether it was accepted or
    cancelled.

## Known gaps in the SIP implementation

What this bridge does not implement, and what depends on that:

- **Digest authentication is the RFC 2069 form**: `MD5(HA1:nonce:HA2)`, with
  no `qop`, `cnonce`, nonce count or `opaque` echo. Asterisk 20 challenges
  with `qop="auth"` and `opaque=...` and accepts the older response anyway;
  a registrar that *requires* `qop` would reject it.
- **`Record-Route` and `Route` are ignored entirely.** In-dialog requests
  (BYE) go straight to the peer's Contact. A proxy that builds a route set
  for the dialog would not be honoured.
- **`;rport` is sent but the answer is never read.** A registrar that reports
  back the source address it actually saw is telling us something this
  bridge discards - which matters the moment it sits behind NAT.
- **Two codecs**, G.722 and PCMU (`sip_sdp.py`). Anything else a peer offers
  is answered with PCMU.

`test-peer/` exists to keep this list honest.
