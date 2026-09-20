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

The WebRTC of a call lives in its own object behind `media` - see
call_media.py.
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
    # Who the caller is to Nextcloud, when direct dial-in made them a real
    # participant. Without it they exist only in the signaling server, and
    # the virtual session can name no actor.
    dialin_actor: dict = None
    number: str = ""   # the far end, as it should appear in the room

    # -- ringing in Talk, before anyone has answered ---------------------
    waiting_for_accept: bool = False
    talk_ring_opener: object = None       # cookie jar of the session that started the ring
    talk_ring_sessionid: str = None       # that session, to exclude it from "somebody answered"
    accepted_sessionid: str = None        # the session that did answer

    # -- the phone's name plate in the room ------------------------------
    virtual_sessionid: str = None         # chosen here, used to add and remove it
    virtual_room_sessionid: str = None    # assigned by the server, seen in room rosters

    # -- media -----------------------------------------------------------
    # A CallMedia once the call carries audio: the two peer connections and
    # what runs between them. Kept behind one field rather than spread over
    # six, because they are created together and die together.
    media: object = None

    @property
    def is_publishing(self) -> bool:
        """Whether this call reached the point of carrying audio. Distinguishes
        a call in progress from one still ringing."""
        return self.media is not None and self.media.is_publishing

    def own_session_ids(self) -> set:
        """The sessions in the room that belong to this bridge rather than to
        a person - the name plate, the server's id for it, and the session
        that started the ring. Mistaking one of these for somebody answering
        is what makes a call pick itself up."""
        return {s for s in (self.virtual_sessionid, self.virtual_room_sessionid,
                            self.talk_ring_sessionid) if s}
