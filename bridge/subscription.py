# talk-sip-bridge - bridge/subscription.py
# Getting hold of another participant's audio, as one state machine.
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

"""Getting hold of another participant's audio, as one state machine.

Subscribing to a stream in this signaling protocol is not a request with
an answer. It is a conversation that can go wrong at four different
points, each with its own message: the publisher may not exist yet
("client_not_found"), the offer may never come, the answer may be
refused because the server re-attached its own end in the meantime
("answer message sid does not match subscriber sid"), and a connection
that negotiated perfectly may still carry no audio.

Handled as separate reflexes - a retry loop here, an offer handler
there, an error handler somewhere else - those four produce repairs that
do not know about each other. Measured on a live call: seven rebuilds in
five seconds, each throwing away a subscription that might have been a
second from working, because every refusal started its own repair.

So the whole negotiation lives here, as states and transitions, with no
I/O of its own: it is told what happened and answers with what to do
next. Two properties follow from that and are what the scattered version
could not have:

- **One negotiation at a time.** Every offer starts a generation; an
  answer or a refusal that names an older one is history and changes
  nothing.
- **Every repair is bounded and delayed.** Requests and rebuilds share
  one budget, and the machine hands out the delay with the action, so
  nothing can retry in a tight loop.

Audio flowing is the one state a repair may not disturb: FLOWING absorbs
every later offer and refusal, which is how a working call survives the
duplicate offers the server sends after it.
"""
import dataclasses
import enum

# How often the bridge may ask again in total - requesting an offer and
# rebuilding a refused subscription come out of the same budget, because
# to the person on the phone they are the same waiting.
MAX_ATTEMPTS = 6
# Between asking for an offer again: the publisher usually appears within
# a second or two of joining.
REQUEST_DELAY = 5.0
# Before rebuilding a refused subscription: long enough for the server to
# finish the re-attach that refused it.
REBUILD_DELAY = 3.0


class State(enum.Enum):
    IDLE = "idle"                # nothing asked yet
    REQUESTED = "requested"      # an offer was asked for
    ANSWERING = "answering"      # an offer arrived and is being answered
    CONNECTING = "connecting"    # the answer went out; no audio yet
    FLOWING = "flowing"          # audio is arriving - the only state that carries sound
    GIVEN_UP = "given-up"        # out of attempts
    CLOSED = "closed"            # the call ended


class Action(enum.Enum):
    NOTHING = "nothing"
    REQUEST = "request"          # ask (again) for an offer
    ANSWER = "answer"            # answer the offer named in the step
    REBUILD = "rebuild"          # throw the subscription away, then ask again
    GIVE_UP = "give-up"          # say so once, then stay quiet


@dataclasses.dataclass(frozen=True)
class Step:
    """What to do about what just happened, and when."""

    action: Action = Action.NOTHING
    delay: float = 0.0
    sid: str = None
    generation: int = 0

    def __bool__(self):
        return self.action is not Action.NOTHING


class Subscription:
    """One attempt to hear one participant, from asking to audio."""

    def __init__(self, *, max_attempts: int = MAX_ATTEMPTS,
                 request_delay: float = REQUEST_DELAY,
                 rebuild_delay: float = REBUILD_DELAY):
        self.state = State.IDLE
        self.max_attempts = max_attempts
        self.request_delay = request_delay
        self.rebuild_delay = rebuild_delay
        self.attempts = 0
        self.generation = 0
        self.sid = None

    # -- what the bridge does --------------------------------------------
    def start(self) -> Step:
        if self.state is not State.IDLE:
            return Step()
        self.state = State.REQUESTED
        self.attempts = 1
        return Step(Action.REQUEST)

    def answer_sent(self, generation: int) -> Step:
        """The answer for that generation is on its way."""
        if self.state is not State.ANSWERING or generation != self.generation:
            return Step()
        self.state = State.CONNECTING
        return Step()

    # -- what the server says --------------------------------------------
    def offer(self, sid: str) -> Step:
        """An offer arrived. The newest one is always the live one - the
        server re-attaches its end and offers again - except once audio
        is flowing, when a repeat is a late duplicate and answering it
        would reset a connection that works."""
        if self.state in (State.FLOWING, State.CLOSED, State.GIVEN_UP):
            return Step()
        self.generation += 1
        self.sid = sid
        self.state = State.ANSWERING
        return Step(Action.ANSWER, sid=sid, generation=self.generation)

    def refused(self, generation: int = None) -> Step:
        """The server would not take our answer. Usually because it
        re-attached its own end while the publisher was not sending yet,
        so the handle we answered for is gone."""
        if self.state in (State.FLOWING, State.CLOSED, State.GIVEN_UP):
            return Step()
        if generation is not None and generation != self.generation:
            return Step()      # about a negotiation that has been superseded
        return self._retry(Action.REBUILD, self.rebuild_delay)

    def no_publisher(self) -> Step:
        """The publisher does not exist yet ("client_not_found"), or the
        offer never came."""
        if self.state in (State.FLOWING, State.CLOSED, State.GIVEN_UP, State.IDLE):
            return Step()
        return self._retry(Action.REQUEST, self.request_delay)

    def media_arrived(self) -> Step:
        """A frame, not a track: a track exists as soon as an offer is
        applied, and a connection that never completes has one too."""
        if self.state in (State.CLOSED,):
            return Step()
        self.state = State.FLOWING
        return Step()

    def close(self) -> Step:
        self.state = State.CLOSED
        return Step()

    # -- internals --------------------------------------------------------
    def _retry(self, action: Action, delay: float) -> Step:
        if self.attempts >= self.max_attempts:
            self.state = State.GIVEN_UP
            return Step(Action.GIVE_UP)
        self.attempts += 1
        self.state = State.REQUESTED
        self.generation += 1      # anything still in flight is now history
        return Step(action, delay=delay, generation=self.generation)

    def still_current(self, step) -> bool:
        """Whether a step decided earlier is still the one to take.

        Every step carries the generation it was decided in, and a step
        with a delay is acted on later - by which time an offer may have
        arrived, audio may be flowing, or the call may be over. Asking
        this after the wait is what keeps a timer that was armed while
        nothing worked from firing into a connection that does."""
        if self.state in (State.FLOWING, State.CLOSED, State.GIVEN_UP):
            return False
        return step.generation == self.generation

    @property
    def working(self) -> bool:
        return self.state is State.FLOWING

    def __repr__(self):
        return (f"Subscription({self.state.value}, attempt {self.attempts}"
                f"/{self.max_attempts}, generation {self.generation})")
