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

from media import SipAudioTrack, StreamResampler, frame_to_mono_pcm

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
        self.subscriber = None
        self.human_sessionid = None            # whose audio the subscriber asked for
        self.offer_arrived = None              # set once the server's offer for it came in
        self.subscriber_receiving = False      # a frame arrived: this one really works
        self._answered_an_offer = False
        self._on_receiving = None
        # Called once, on the first frame that actually arrives - the
        # only evidence that Talk's audio reaches the phone.
        self.on_media_flowing = None
        self.relay_task = None
        self._poll_task = None
        self._lost = False

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

    @property
    def subscriber_alive(self) -> bool:
        """Whether the subscription is still a connection that could
        carry audio. One that closed or failed is not, and the bridge
        has to be free to ask somebody for their audio again - a client
        that changes its microphone tears its publisher down and builds
        a new one, and the old subscription dies with it."""
        return (self.subscriber is not None
                and self.subscriber.connectionState not in ("closed", "failed"))

    # -- subscriber: Talk -> phone ---------------------------------------
    def open_subscriber(self, human_sessionid: str, on_receiving=None):
        """Builds the subscribing connection. Audio only starts flowing when
        a track arrives, which is the first positive evidence that anything
        reaches the phone at all - everything before it is negotiation."""
        subscriber = RTCPeerConnection(NO_ICE_SERVERS)
        self.subscriber = subscriber
        self.human_sessionid = human_sessionid
        self.offer_arrived = self.offer_arrived or asyncio.Event()
        self.subscriber_receiving = False
        self._answered_an_offer = False
        self._on_receiving = on_receiving

        @subscriber.on("connectionstatechange")
        async def on_subscriber_state():
            # The connection this handler belongs to, not whichever one
            # is current: a subscription that is replaced or closed still
            # reports its last states, and reading them off the call
            # would ask a connection that is already gone.
            print(f"[talk] Subscriber connection state: {subscriber.connectionState}")

        @self.subscriber.on("track")
        def on_track(track):
            if track.kind != "audio":
                return
            if on_receiving:
                on_receiving(track)
            self.relay_task = asyncio.ensure_future(self._relay(track))

    async def answer_subscriber_offer(self, sdp: str):
        """Answers the offer the server relays for the stream we asked for.

        A second offer means one of two opposite things, and which one is
        decided by whether audio is already arriving:

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
        if self.subscriber_receiving:
            return None
        if self._answered_an_offer and self.subscriber is not None:
            replaced = self.subscriber
            self.open_subscriber(self.human_sessionid, on_receiving=self._on_receiving)
            await replaced.close()
            print(f"[talk] The server re-offered {self.human_sessionid}'s audio for "
                  f"{self.sip_call_id} - subscribing again")
        self._answered_an_offer = True
        if self.offer_arrived is not None:
            self.offer_arrived.set()
        await self.subscriber.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        await self.subscriber.setLocalDescription(await self.subscriber.createAnswer())
        return self.subscriber.localDescription.sdp

    async def prepare_for_new_offer(self):
        """Throws away a subscription the server no longer knows and
        builds a fresh one, ready to be offered to again.

        Needed because the server can re-attach its side without
        offering again: it does that while the publisher is not sending
        yet, and every answer after that names a handle that is gone."""
        if self.subscriber_receiving or self.subscriber is None:
            return False
        replaced = self.subscriber
        self.open_subscriber(self.human_sessionid, on_receiving=self._on_receiving)
        self.offer_arrived.clear()
        await replaced.close()
        return True

    async def _relay(self, track):
        """Reads Talk's audio (48kHz, from whatever the person's device
        captured) and forwards it to the phone, downsampled to the rate the
        call negotiated. Ends when the subscriber is closed: track.recv()
        then raises."""
        resampler = None
        pending = np.zeros(0, dtype=np.int16)
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
                if not self.subscriber_receiving:
                    self.subscriber_receiving = True
                    if self.on_media_flowing is not None:
                        self.on_media_flowing()
                pcm = frame_to_mono_pcm(frame)
                stats["frames"] += 1
                stats["peak"] = max(stats["peak"], int(np.abs(pcm).max()) if len(pcm) else 0)
                if stats["since"] is None:
                    stats["since"] = loop.time()
                elif loop.time() - stats["since"] >= 1.0:
                    print(f"[talk] Talk audio for {self.sip_call_id}: {stats['frames']} frames, "
                          f"peak {stats['peak']}")
                    stats = {"frames": 0, "peak": 0, "since": loop.time()}
                if resampler is None or resampler.in_rate != frame.sample_rate:
                    resampler = StreamResampler(frame.sample_rate, self.rtp_session.sample_rate)
                # Whole packets only: send_pcm pads a short one with
                # silence, and a resampler that carries its phase hands
                # out 159 samples as readily as 160 - padding every one of
                # those would put back the very artefact it removes.
                pending = np.concatenate((pending, resampler.process(pcm)))
                packet = self.rtp_session.samples_per_packet
                whole = len(pending) - len(pending) % packet
                if whole:
                    await asyncio.to_thread(self.rtp_session.send_pcm, pending[:whole])
                    pending = pending[whole:]
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
        if self.subscriber is not None and self.human_sessionid == sender_sessionid:
            return self.subscriber, True
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
        if self.relay_task:
            self.relay_task.cancel()
            self.relay_task = None
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        if self.subscriber:
            await self.subscriber.close()
            self.subscriber = None
        if self.publisher:
            await self.publisher.close()
            self.publisher = None
