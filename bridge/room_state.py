"""Who is in a Talk room's call, tracked from the signaling server's
`participants`/`update` events.

Every decision taken from this model is a *transition* - somebody entered
the call, somebody left - never the current state. Reading state instead is
what made a session the signaling server still listed as in-call, long
after its client was gone, look exactly like a person answering: inbound
calls were then picked up instantly against a dead peer. Transitions also
make several sessions of one person (Talk in a browser and on a phone at
once) unremarkable: only the one that moves counts.

The bridge's own sessions move through the call too - the virtual phone
session and the bridge's own publisher both enter it - so every question
here takes the set of session ids that belong to the bridge.
"""

# The in-call bit the signaling server sets; the remaining bits say what
# media a session carries.
FLAG_IN_CALL = 1
FLAG_WITH_AUDIO = 2


def as_flags(raw) -> int:
    """The flags of one entry, or none at all. The field is missing as
    often as it is present, and arrives as a string as often as a
    number."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def has_in_call_flag(raw) -> bool:
    return bool(as_flags(raw) & FLAG_IN_CALL)


def is_room_wide_call_end(update: dict) -> bool:
    """The server broadcasts the end of the call itself as an "all" entry
    with a lowercase "incall" - this is what Talk's "end call" button
    produces, and it is the reliable end-of-call signal."""
    if not update.get("all"):
        return False
    raw = update.get("incall", update.get("inCall"))
    return raw is not None and not has_in_call_flag(raw)


def _session_id(entry: dict):
    return entry.get("sessionId") or entry.get("sessionid")


class RoomCallState:
    """The in-call membership of one room, updated event by event."""

    def __init__(self):
        # session id -> its in-call flags. None until the first update
        # seeds it. The flags rather than a yes/no, because which media a
        # session carries decides whom there is any point listening to.
        self._flags = None

    @property
    def seeded(self) -> bool:
        return self._flags is not None

    def apply(self, update: dict) -> tuple[set, set]:
        """Folds one `participants`/`update` into the model and reports what
        moved, as (entered, left) sets of session ids.

        The first update after connecting carries the room's membership
        rather than a change, so it seeds the model and reports nothing."""
        previous = self._flags
        current = dict(previous or {})

        users = update.get("users")
        if users is not None:
            # A snapshot replaces what is known: a session missing from it is
            # gone. This is the only way the model learns about sessions that
            # dropped without the server ever sending a "leave".
            current = {}
            for user in users:
                session_id = _session_id(user)
                if session_id:
                    current[session_id] = as_flags(user.get("inCall"))

        for item in update.get("changed") or []:
            session_id = _session_id(item)
            if session_id and "inCall" in item:
                current[session_id] = as_flags(item["inCall"])

        self._flags = current
        if previous is None:
            return set(), set()
        entered = {s for s, now in current.items()
                   if now & FLAG_IN_CALL and not previous.get(s, 0) & FLAG_IN_CALL}
        left = {s for s, was in previous.items()
                if was & FLAG_IN_CALL and not current.get(s, 0) & FLAG_IN_CALL}
        return entered, left

    def accepted_by(self, entered: set, ours: set):
        """Which session id counts as somebody answering, or None. Sorted so
        that a snapshot listing several at once resolves the same way twice."""
        return next((s for s in sorted(entered) if s not in ours), None)

    def others_in_call(self, ours: set) -> set:
        """Everyone in the call who is not this bridge - who to tell what
        the phone's microphone is doing."""
        return {session_id for session_id, flags in (self._flags or {}).items()
                if flags & FLAG_IN_CALL and session_id not in ours}

    def anyone_in_call_besides(self, ours: set) -> bool:
        return any(flags & FLAG_IN_CALL for session_id, flags in (self._flags or {}).items()
                   if session_id not in ours)

    def carries_audio(self, session_id: str) -> bool:
        """Whether that session says it is publishing audio. A
        participant whose permissions do not let them speak joins
        without the flag, and there is nothing to take from them."""
        return bool((self._flags or {}).get(session_id, 0) & FLAG_WITH_AUDIO)
