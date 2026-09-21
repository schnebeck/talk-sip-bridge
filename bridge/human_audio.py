# talk-sip-bridge - bridge/human_audio.py
# Getting the room's audio to the phone.
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

"""Getting the room's audio to the phone.

The other direction of a call. Publishing is one message and an answer;
this side is a negotiation that can fail at four separate points, and
the state machine that keeps those four from repairing each other to
death lives in `subscription.py`. What is here is everything around it:
choosing whom to listen to, asking, answering what comes back, and
noticing when nothing does.

It works on the client's state and sends on the client's room
connection - it is that client's subscriber half, not a thing of its
own.
"""
import asyncio
import json

import talk_messages
from config import config
from subscription import Action, Subscription

# A single requestoffer is not enough: the other side's publisher may not
# exist yet when it goes out, and the signaling server answers that with
# "client_not_found" rather than queuing. Talk's own client re-requests
# every 10s for exactly this reason.
RETRY_INTERVAL = 5
MAX_ATTEMPTS = 6
# A refused answer is repaired by building the subscription again, and
# that has to be rare: each rebuild throws away one that may be seconds
# from working, and a handful in a row is not a repair but a storm -
# measured, seven in five seconds, with nothing left standing.
REBUILD_MAX_ATTEMPTS = 2
REBUILD_DELAY = 3


class HumanAudio:
    """One TalkClient's subscriber half, working on its state."""

    def __init__(self, client):
        self.client = client

    def people(self) -> list:
        """Everybody in the room this call could take audio from, best
        first: those who say they are carrying audio before those who do
        not, and sorted within each group so two runs agree.

        A participant whose permissions do not let them speak joins
        without the audio flag, and subscribing to them spends attempts
        on a stream that will never exist. But the flags are only the
        server's word, and this bridge has been wrong about them before,
        so the others stay on the list behind them rather than off it."""
        with self.client._call_sessions_lock:
            everybody = [sessionid for sessionid, info in self.client._room_roster.items()
                         if info.get("is_human")]
        speaking = [s for s in sorted(everybody) if self.client._room_call.carries_audio(s)]
        return speaking + [s for s in sorted(everybody) if s not in set(speaking)]

    def targets(self) -> list:
        """Whose audio to ask for. One, or the room.

        A telephone call carries one stream, so hearing more than one
        person means summing them (mixer.py). That is off by default:
        one subscription is what every call this bridge has carried so
        far, and the summing path has never run against a telephone."""
        people = self.people()
        if not config.mix_participants:
            return people[:1]
        return people[:max(1, config.max_mixed_sources)]

    async def find_human(self, retries: int = 5, delay: float = 0.3) -> str | None:
        """The first participant worth listening to, waiting briefly for
        the roster if it is not there yet.

        The roster (see room_presence) is normally populated before we
        publish at all - for a dialout the person placing the call is
        already in the room - so the retry loop only covers our own
        room-join confirmation racing ahead of the broadcast for them."""
        for _ in range(retries):
            people = self.people()
            if people:
                return people[0]
            await asyncio.sleep(delay)
        return None

    async def start_for_late_joiner(self, sip_call_id: str, entered):
        """Subscribes to somebody who joined after the phone was already
        publishing.

        `publish` looks for a participant exactly once, at the moment the
        call connects, because for a dialout there is always somebody
        there already - they placed the call. A caller who dials in
        arrives in an empty room, so that one look finds nobody and the
        phone would never hear Talk at all. Measured: the person joined
        thirteen seconds after the caller, the bridge announced the
        phone to them, and never asked for their audio."""
        with self.client._call_sessions_lock:
            entry = self.client._call_sessions.get(sip_call_id)
            media = entry.media if entry else None
            if media is None or not entry.is_publishing or media.subscriber_alive:
                return  # nothing to publish into, or already listening to somebody
            people = [sessionid for sessionid in sorted(entered)
                      if self.client._room_roster.get(sessionid, {}).get("is_human")]
        # Somebody who says they are publishing audio first. A
        # participant whose permissions do not let them speak joins
        # without that flag, and subscribing to them spends the
        # attempts on a stream that will never exist. Sorted, so that
        # several arriving at once resolve the same way twice; and
        # anybody at all rather than nobody, because the flags are the
        # server's word and this bridge has been wrong about them
        # before.
        joined = next((s for s in people if self.client._room_call.carries_audio(s)),
                      people[0] if people else None)
        if joined is None:
            return
        print(f"[talk] {joined} joined after {sip_call_id} was already publishing "
              f"- asking for their audio now")
        await self.start(sip_call_id, media, joined)

    async def start(self, sip_call_id: str, media, human_sessionid: str):
        """Asks for the other side's audio and follows the negotiation to
        audio or to giving up.

        What to do at each turn is decided by the call's Subscription (see
        subscription.py); everything here is the doing - sending, waiting,
        rebuilding. Splitting it that way is what keeps two repairs from
        running at once, which is what tore the return direction down."""
        def on_receiving(track):
            print(f"[talk] Receiving audio from {human_sessionid} for {sip_call_id}")

        media.open_subscriber(human_sessionid, on_receiving=on_receiving)
        state = self.restart_state_for(sip_call_id, human_sessionid)
        if state is None:
            return

        def flowing(sessionid):
            arrived = self.state_for(sip_call_id, sessionid)
            if arrived is not None:
                arrived.media_arrived()
            print(f"[talk] {sessionid[:8]}'s audio reaches the phone for {sip_call_id} "
                  f"({len(media.receiving_from)} of {len(media.subscribers)} sources)")

        media.on_media_flowing = flowing
        await self.pursue(sip_call_id, media, human_sessionid, state.start())

    async def start_all(self, sip_call_id: str, media):
        """Subscribes to everybody worth listening to, one negotiation
        each, all at once. With mixing off that is a list of one and
        this is what has always happened."""
        started = []
        for sessionid in self.targets():
            if media.alive_for(sessionid):
                continue
            started.append(sessionid)
            asyncio.ensure_future(self.start(sip_call_id, media, sessionid))
        return started

    @staticmethod
    def _fresh():
        return Subscription(max_attempts=MAX_ATTEMPTS,
                            request_delay=RETRY_INTERVAL,
                            rebuild_delay=REBUILD_DELAY)

    def restart_state_for(self, sip_call_id: str, sessionid: str):
        """A new subscription is a new negotiation, and gets a machine
        that has not been anywhere.

        Keeping the last one hands back a state that already reached
        FLOWING, and a machine that believes audio is flowing refuses to
        ask for any - measured: after a client came back from changing
        its microphone, the subscription was rebuilt and not one message
        went out.

        One machine per participant. Sharing one across several would
        let a repair for somebody whose stream never came tear down the
        connection to somebody whose did."""
        with self.client._call_sessions_lock:
            entry = self.client._call_sessions.get(sip_call_id)
            if entry is None:
                return None
            entry.subscriptions[sessionid] = self._fresh()
            return entry.subscriptions[sessionid]

    def state_for(self, sip_call_id: str, sessionid: str):
        with self.client._call_sessions_lock:
            entry = self.client._call_sessions.get(sip_call_id)
            if entry is None:
                return None
            if entry.subscriptions.get(sessionid) is None:
                entry.subscriptions[sessionid] = self._fresh()
            return entry.subscriptions[sessionid]

    async def pursue(self, sip_call_id: str, media, human_sessionid: str, step):
        """Carries out one step of the negotiation, and the waiting it
        asks for."""
        if not step:
            return
        if step.delay:
            await asyncio.sleep(step.delay)
        with self.client._call_sessions_lock:
            entry = self.client._call_sessions.get(sip_call_id)
            still_the_call = entry is not None and entry.media is media
            state = entry.subscriptions.get(human_sessionid) if entry else None
        if not still_the_call or state is None:
            return  # the call ended, or a newer subscription replaced this one
        if media.source(human_sessionid) is None:
            # This call has stopped listening to them. A step decided
            # beforehand would ask the server about a session the call
            # has nothing to do with any more.
            return
        if not state.still_current(step):
            # Decided before the wait, overtaken during it: an offer
            # arrived, audio started flowing, or the attempts ran out.
            # Acting anyway is how a repair reaches into a working
            # connection and closes it.
            return

        if step.action is Action.GIVE_UP:
            print(f"[talk] No audio from {human_sessionid} for {sip_call_id} after "
                  f"{state.attempts} attempts - the phone side stays silent for this call")
            return
        if step.action is Action.REBUILD:
            if not await media.prepare_for_new_offer(human_sessionid):
                return
            print(f"[talk] Subscribing to {human_sessionid} again for {sip_call_id}")
        if step.action in (Action.REQUEST, Action.REBUILD):
            try:
                await self.client.ws.send(json.dumps(
                    talk_messages.request_offer(sip_call_id, human_sessionid)))
            except Exception as e:
                print(f"[talk] Could not request audio from {human_sessionid}: {e!r}")
                return
            print(f"[talk] Requested audio from {human_sessionid} for {sip_call_id} "
                  f"(attempt {state.attempts}/{MAX_ATTEMPTS})")
            # Nothing may arrive at all: the server answers a request for
            # a publisher that does not exist yet with an error, and
            # sometimes with silence.
            asyncio.ensure_future(self.watch_for_offer(sip_call_id, media, human_sessionid,
                                                        state.generation))

    async def watch_for_offer(self, sip_call_id: str, media, human_sessionid: str,
                               generation: int):
        await asyncio.sleep(RETRY_INTERVAL)
        with self.client._call_sessions_lock:
            entry = self.client._call_sessions.get(sip_call_id)
            state = (entry.subscriptions.get(human_sessionid)
                     if entry and entry.media is media else None)
        if (state is None or state.working or state.generation != generation
                or media.source(human_sessionid) is None):
            return  # something else happened in the meantime; not our turn
        await self.pursue(sip_call_id, media, human_sessionid, state.no_publisher())

    async def answer_refused(self, sip_call_id: str, sessionid: str, error: dict):
        """The server would not take our answer for the other side's
        audio: it re-attaches its own end while the publisher is not
        sending yet, without offering again, and an answer naming the
        handle it dropped is refused ("answer message sid does not match
        subscriber sid").

        What follows from that is the Subscription's decision - repair,
        or stop trying. Acting on every refusal directly is what produced
        seven rebuilds in five seconds, none of which survived the
        next."""
        with self.client._call_sessions_lock:
            entry = self.client._call_sessions.get(sip_call_id)
            media = entry.media if entry else None
            state = entry.subscriptions.get(sessionid) if entry else None
        if media is None or not sessionid or state is None:
            return
        step = state.refused()
        if not step:
            return
        print(f"[talk] The server refused our answer for {sessionid[:8]}'s audio "
              f"({error.get('code')})")
        await self.pursue(sip_call_id, media, sessionid, step)

    async def answer_offer(self, media, peer: str, data: dict):
        """Answers an offer for a subscription, if it is still the one
        being negotiated.

        The server offers again when it re-attaches its end, and answers
        naming a handle it has dropped are refused. The call's
        Subscription decides which offer counts; a second answer for an
        offer it has moved past is not sent at all."""
        state = self.state_for(media.sip_call_id, peer)
        if state is None:
            return
        step = state.offer(data.get("sid"))
        if step.action is not Action.ANSWER:
            return   # audio already flows, or this negotiation is history
        answer_sdp = await media.answer_subscriber_offer(peer, data["payload"]["sdp"])
        if answer_sdp is None or state.generation != step.generation:
            return
        await self.client.ws.send(json.dumps(talk_messages.subscribe_answer(
            peer, step.sid, answer_sdp, sip_call_id=media.sip_call_id)))
        state.answer_sent(step.generation)
