# talk-sip-bridge - bridge/call_media.py
# The WebRTC of one call: a publisher carrying the phone's audio into the
# room, and a subscriber carrying the room's audio back to the phone.
#
#   Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
#   Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
#   Written by Anthropic Claude Opus 5 - AI generated content.
#
#   Free software under the GNU General Public License, version 3 or later.
#   There is no warranty, to the extent permitted by law. The full text is
#   in LICENSES/GPL-3.0-or-later.txt.
#
# SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
# SPDX-FileContributor: Anthropic Claude Opus 5 (AI generated content)
# SPDX-License-Identifier: GPL-3.0-or-later

"""The WebRTC of one call: a publisher carrying the phone's audio into the
room, and a subscriber carrying the room's audio back to the phone.

This object knows peer connections and codecs. It does not know the
signaling protocol - what goes on the wire is the client's business, and
reaches here as SDP and ICE candidates. That division is why the two can be
reasoned about separately: everything below is WebRTC, everything above is
Talk's own message format.

Both directions belong to one call and die with it, which is what makes
them one object rather than two loose peer connections held in a dict.
"""
import asyncio

from aiortc import RTCConfiguration, RTCIceCandidate, RTCPeerConnection, RTCSessionDescription

import numpy as np

from config import config
from media import SipAudioTrack, StreamResampler, frame_to_mono_pcm
from mix_pump import pump
from mixer import SAMPLE_RATE as MIX_RATE
from mixer import Mixer

# aiortc defaults to a public STUN server, which costs a measured 5 seconds
# of candidate gathering per call before anything can be published - five
# seconds of silence after a caller is answered. The other end of every one
# of these connections is the signaling server's own Janus on this host, so
# host candidates are what actually get used; the reflexive ones it waits
# for are useless here, and asking for them tells a third party about every
# call placed.
NO_ICE_SERVERS = RTCConfiguration(iceServers=[])

# States in which a connection is not coming back.
DEAD_STATES = ("failed", "closed", "disconnected")

# How often the fallback poll checks a connection the event handlers may
# have failed to report on.
CONNECTION_POLL_INTERVAL = 5


def parse_ice_candidate(cand_str: str, sdp_mid=None, sdp_mline_index=0) -> RTCIceCandidate:
    parts = cand_str.replace("candidate:", "").split()
    return RTCIceCandidate(
        component=int(parts[1]), foundation=parts[0], ip=parts[4], port=int(parts[5]),
        priority=int(parts[3]), protocol=parts[2], type=parts[7],
        sdpMid=sdp_mid, sdpMLineIndex=sdp_mline_index,
    )


class Source:
    """One participant this call listens to: the connection, whether it
    has ever delivered a frame, and the offer it is waiting for."""

    def __init__(self, sessionid: str, pc, on_receiving=None):
        self.sessionid = sessionid
        self.pc = pc
        self.receiving = False             # a frame arrived: this one really works
        self.offer_arrived = asyncio.Event()
        self.answered_an_offer = False
        self.on_receiving = on_receiving
        self.relay_task = None

    @property
    def alive(self) -> bool:
        return self.pc is not None and self.pc.connectionState not in ("closed", "failed")


class CallMedia:
    """The two peer connections of one call, and what runs between them."""

    def __init__(self, sip_call_id: str, rtp_session, on_connection_lost=None):
        self.sip_call_id = sip_call_id
        self.rtp_session = rtp_session
        # Called once when the publisher's connection dies, which is the
        # only reliable sign that the person hung up in Talk.
        self._on_connection_lost = on_connection_lost or (lambda reason: None)

        self.publisher = None
        self.publisher_peer_sessionid = None   # the session the publish offer is addressed to
        # One Source per participant whose audio this call carries,
        # keyed by their session. A call used to hold exactly one of
        # these as five loose attributes; they are a bundle now because
        # there can be several, and losing track of which belongs to
        # whom is how an answer reaches the wrong negotiation.
        self.subscribers = {}
        # Called with a session id on the first frame that actually
        # arrives from them - the only evidence that their audio reaches
        # the phone.
        self.on_media_flowing = None
        self._poll_task = None
        self._lost = False
        # Everything a subscription receives goes in here, and one pump
        # clocks it out to the phone. With a single source that is a
        # sum of one - transparent but for the limiter's 20ms of
        # look-ahead - and it is the same path several sources use, so
        # the ordinary call exercises it every time.
        self.mixer = Mixer()
        self._pump_task = None

    # -- publisher: phone -> Talk ----------------------------------------
    def open_publisher(self, peer_sessionid: str, on_talking=None):
        """Builds the publishing connection and starts watching it. The
        offer is addressed to our own session: a virtual session only
        represents the call in the participant list and has no client that
        could answer one.

        `on_talking` is called with True or False as the caller starts and
        stops speaking, measured once a second on the audio arriving from
        the phone."""
        self.publisher = RTCPeerConnection(NO_ICE_SERVERS)
        track = SipAudioTrack(self.rtp_session)
        track.on_talking = on_talking
        self.publisher.addTrack(track)
        self.publisher_peer_sessionid = peer_sessionid
        self._watch(self.publisher)

    async def publisher_offer(self) -> str:
        """Creates the offer and waits out ICE gathering; the SDP that comes
        back is what the client sends."""
        await self.publisher.setLocalDescription(await self.publisher.createOffer())
        return self.publisher.localDescription.sdp

    async def accept_publisher_answer(self, sdp: str):
        await self.publisher.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))

    def source(self, sessionid: str):
        return self.subscribers.get(sessionid)

    def listening_to(self) -> list:
        """Whose audio this call is carrying, sorted so that two runs
        with the same participants line up."""
        return sorted(self.subscribers)

    def alive_for(self, sessionid: str) -> bool:
        """Whether that participant's subscription is still a connection
        that could carry audio. One that closed or failed is not, and
        the bridge has to be free to ask again - a client that changes
        its microphone tears its publisher down and builds a new one,
        and the subscription dies with it."""
        source = self.subscribers.get(sessionid)
        return source is not None and source.alive

    @property
    def subscriber_alive(self) -> bool:
        """Whether this call is listening to anybody at all."""
        return any(source.alive for source in self.subscribers.values())

    @property
    def receiving_from(self) -> list:
        """The participants who have actually delivered a frame. Not the
        same as the ones subscribed: a negotiation that completes and
        carries nothing is the failure this project kept meeting."""
        return sorted(s.sessionid for s in self.subscribers.values() if s.receiving)

    # -- subscriber: Talk -> phone ---------------------------------------
    def open_subscriber(self, sessionid: str, on_receiving=None):
        """Builds a subscribing connection for one participant. Audio
        only starts flowing when a track arrives, which is the first
        positive evidence that anything reaches the phone at all -
        everything before it is negotiation."""
        pc = RTCPeerConnection(NO_ICE_SERVERS)
        previous = self.subscribers.get(sessionid)
        source = Source(sessionid, pc, on_receiving=on_receiving)
        if previous is not None and previous.offer_arrived.is_set():
            # A rebuild waits for its own offer, not the one the
            # connection it replaces already had.
            source.offer_arrived.clear()
        self.subscribers[sessionid] = source

        @pc.on("connectionstatechange")
        async def on_subscriber_state():
            # The connection this handler belongs to, not whichever one
            # is current: a subscription that is replaced or closed still
            # reports its last states, and reading them off the call
            # would ask a connection that is already gone.
            print(f"[talk] Subscriber connection state for {sessionid[:8]}: "
                  f"{pc.connectionState}")

        @pc.on("track")
        def on_track(track):
            if track.kind != "audio":
                return
            if on_receiving:
                on_receiving(track)
            self.start_pump()
            source.relay_task = asyncio.ensure_future(self._relay(track, source))
        return source

    async def drop_subscriber(self, sessionid: str):
        """Stops listening to one participant. The others are untouched,
        which is the point of holding them separately."""
        source = self.subscribers.pop(sessionid, None)
        if source is None:
            return
        self.mixer.remove(sessionid)
        if source.relay_task:
            source.relay_task.cancel()
        if source.pc is not None:
            await source.pc.close()

    def start_pump(self):
        """One clock per call, started by the first track to arrive and
        not before: a pump running while nobody is subscribed would send
        a phone silence it has not asked for, and on an inbound call
        that silence would cover the dial-in prompt."""
        if self._pump_task is not None:
            return
        self._pump_task = asyncio.ensure_future(pump(
            self.mixer, self.rtp_session, lambda: not self._lost,
            on_error=lambda e: print(
                f"[talk] The audio pump for {self.sip_call_id} stopped: {e!r}")))

    async def answer_subscriber_offer(self, sessionid: str, sdp: str):
        """Answers the offer the server relays for one participant's
        stream.

        A second offer for the same one means one of two opposite
        things, and which is decided by whether audio is already
        arriving from them:

        - audio is flowing: the offer is a late duplicate, and answering
          it resets a connection that works - observed as audio dropping
          mid-call.
        - nothing is flowing yet: the server has thrown the old
          subscription away and built a new one, which it does when the
          publisher is not sending yet. The old one is dead, its handle
          is gone, and an answer carrying its id is refused ("answer
          message sid does not match subscriber sid"). The new offer is
          the live one, so the connection is rebuilt for it.

        Returns the answer SDP, or None if there is nothing to answer."""
        source = self.subscribers.get(sessionid)
        if source is None or source.receiving:
            return None
        if source.answered_an_offer and source.pc is not None:
            replaced = source.pc
            source = self.open_subscriber(sessionid, on_receiving=source.on_receiving)
            await replaced.close()
            print(f"[talk] The server re-offered {sessionid[:8]}'s audio for "
                  f"{self.sip_call_id} - subscribing again")
        source.answered_an_offer = True
        source.offer_arrived.set()
        await source.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        await source.pc.setLocalDescription(await source.pc.createAnswer())
        return source.pc.localDescription.sdp

    async def prepare_for_new_offer(self, sessionid: str):
        """Throws away one subscription the server no longer knows and
        builds a fresh one, ready to be offered to again.

        Needed because the server can re-attach its side without
        offering again: it does that while the publisher is not sending
        yet, and every answer after that names a handle that is gone."""
        source = self.subscribers.get(sessionid)
        if source is None or source.receiving:
            return False
        replaced = source.pc
        self.open_subscriber(sessionid, on_receiving=source.on_receiving)
        await replaced.close()
        return True

    async def _relay(self, track, source):
        """Reads one participant's audio (48kHz, from whatever their
        device captured) and puts it in the call's mixer under their own
        session id. Ends when the subscription is closed: track.recv()
        then raises."""
        sessionid = source.sessionid
        resampler = None
        # The mirror of the phone-side counters. Without them a silent call
        # in this direction looks identical to a broken one: the track
        # arrives either way, and everything after it is invisible.
        loop = asyncio.get_running_loop()
        stats = {"frames": 0, "peak": 0, "since": None}
        try:
            while True:
                frame = await track.recv()
                # The first frame, not the track: a track object exists as
                # soon as the offer is applied, long before anything
                # flows - and treating that as a working connection is
                # what let a refused answer go unrepaired.
                if not source.receiving:
                    source.receiving = True
                    if self.on_media_flowing is not None:
                        self.on_media_flowing(sessionid)
                pcm = frame_to_mono_pcm(frame)
                stats["frames"] += 1
                stats["peak"] = max(stats["peak"], int(np.abs(pcm).max()) if len(pcm) else 0)
                if stats["since"] is None:
                    stats["since"] = loop.time()
                elif config.audio_report_interval and (
                        loop.time() - stats["since"] >= config.audio_report_interval):
                    print(f"[talk] Audio from {sessionid[:8]} for {self.sip_call_id} over "
                          f"{loop.time() - stats['since']:.0f}s: {stats['frames']} frames, "
                          f"peak {stats['peak']}")
                    stats = {"frames": 0, "peak": 0, "since": loop.time()}
                # Into the mixer at Talk's own rate, not resampled here:
                # the sum is built at 48kHz and comes down once, which is
                # both cheaper and cleaner than resampling every source.
                # Sending is the pump's job - a relay that sent directly
                # would be a second writer to the same RTP session.
                if frame.sample_rate != MIX_RATE:
                    if resampler is None or resampler.in_rate != frame.sample_rate:
                        resampler = StreamResampler(frame.sample_rate, MIX_RATE)
                    pcm = resampler.process(pcm)
                self.mixer.write(sessionid, pcm)
        except Exception as e:
            print(f"[talk] Human audio relay for {self.sip_call_id} ended ({e!r})")

    # -- routing and candidates ------------------------------------------
    def peer_for(self, sender_sessionid: str):
        """Which connection an incoming WebRTC message belongs to, as
        (connection, is_subscriber), or None if neither.

        A message without a sender matches nothing: comparing it against an
        id that is also unset would otherwise hand it to whichever
        connection happens to exist."""
        if not sender_sessionid:
            return None
        if self.publisher is not None and self.publisher_peer_sessionid == sender_sessionid:
            return self.publisher, False
        source = self.subscribers.get(sender_sessionid)
        if source is not None and source.pc is not None:
            return source.pc, True
        return None

    async def add_candidate(self, pc, payload: dict):
        candidate = (payload or {}).get("candidate", {})
        as_string = candidate.get("candidate", "")
        if not as_string:
            return
        try:
            await pc.addIceCandidate(parse_ice_candidate(
                as_string, sdp_mid=candidate.get("sdpMid"),
                sdp_mline_index=candidate.get("sdpMLineIndex", 0)))
        except Exception as e:
            print(f"[talk] Could not add ICE candidate: {e!r}")

    # -- teardown detection and closing ----------------------------------
    def _watch(self, pc):
        """Ending a call in Talk's UI sends no hangup for this room type -
        the Janus publisher is torn down at the DTLS level instead. That
        does not reliably move iceConnectionState, but connectionState,
        which also reflects DTLS, does. Without noticing it the phone stays
        connected however the call looks in Talk."""

        def lost(source: str, state: str):
            print(f"[talk] Publish {source}: {state}")
            if not self._lost and state in DEAD_STATES:
                self._lost = True
                self._on_connection_lost(None)

        @pc.on("iceconnectionstatechange")
        async def on_ice_state_change():
            lost("ICE state", pc.iceConnectionState)

        @pc.on("connectionstatechange")
        async def on_connection_state_change():
            lost("connection state", pc.connectionState)

        async def poll():
            # Redundant against the handlers above, and needed: observed at
            # least once that neither fired although the connection had gone
            # bad, leaving a phone call connected with nothing to end it.
            while not self._lost:
                await asyncio.sleep(CONNECTION_POLL_INTERVAL)
                if pc.connectionState in DEAD_STATES or pc.iceConnectionState in DEAD_STATES:
                    lost("state poll", pc.connectionState)
                    return

        self._poll_task = asyncio.ensure_future(poll())

    @property
    def is_publishing(self) -> bool:
        return self.publisher is not None

    async def close(self):
        """Stops everything this call had running. Safe to call twice."""
        self._lost = True
        if self._pump_task:
            self._pump_task.cancel()
            self._pump_task = None
        for source in list(self.subscribers.values()):
            if source.relay_task:
                source.relay_task.cancel()
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        for sessionid in list(self.subscribers):
            source = self.subscribers.pop(sessionid)
            if source.pc is not None:
                await source.pc.close()
        if self.publisher:
            await self.publisher.close()
            self.publisher = None
