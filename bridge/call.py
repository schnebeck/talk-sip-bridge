"""One phone call, as the Talk side of the bridge sees it.

A call is created when it starts - an inbound INVITE, or a dialout request
from Talk - and removed when it ends. Everything the Talk side accumulates
about it in between lives in these fields, which is what makes them
enumerable: a dict grown key by key across a dozen methods does not say
which of them exist when.

Fields are filled in by different parts at different moments and stay unset
until then, so `None` means "not yet" throughout. Every access happens
under the owning client's lock: the SIP side writes from worker threads,
the Talk side from its event loop.
"""
import dataclasses

INBOUND = "inbound"
DIALOUT = "dialout"


@dataclasses.dataclass
class Call:
    """The Talk-side state of one call on one line."""

    sip_call_id: str
    kind: str          # INBOUND or DIALOUT - a dialout reports status back
    roomid: str = ""   # dialout learns it from the request; inbound uses the line's default
    number: str = ""   # the far end, as it should appear in the room

    # -- ringing in Talk, before anyone has answered ---------------------
    waiting_for_accept: bool = False
    talk_ring_opener: object = None       # cookie jar of the session that started the ring
    talk_ring_sessionid: str = None       # that session, to exclude it from "somebody answered"
    accepted_sessionid: str = None        # the session that did answer

    # -- the phone's name plate in the room ------------------------------
    virtual_sessionid: str = None         # chosen here, used to add and remove it
    virtual_room_sessionid: str = None    # assigned by the server, seen in room rosters

    # -- media, one peer connection per direction ------------------------
    publisher: object = None              # phone -> Talk
    publisher_peer_sessionid: str = None  # who the publish offer is addressed to (ourselves)
    subscriber: object = None             # Talk -> phone
    human_sessionid: str = None           # whose audio the subscriber asked for
    subscriber_offer: object = None       # set once that offer arrives, ending the retries
    relay_task: object = None             # forwards subscribed frames into the RTP session

    @property
    def is_publishing(self) -> bool:
        """Whether this call reached the point of carrying audio. Distinguishes
        a call in progress from one still ringing."""
        return self.publisher is not None

    def own_session_ids(self) -> set:
        """The sessions in the room that belong to this bridge rather than to
        a person - the name plate, the server's id for it, and the session
        that started the ring. Mistaking one of these for somebody answering
        is what makes a call pick itself up."""
        return {s for s in (self.virtual_sessionid, self.virtual_room_sessionid,
                            self.talk_ring_sessionid) if s}
