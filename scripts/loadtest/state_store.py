from dataclasses import dataclass, field
from threading import Lock
import random


@dataclass
class UserRuntimeState:
    vu_id: int
    run_id: str
    access_token: str | None = None
    refresh_token: str | None = None
    user_id: int | None = None
    email: str | None = None
    is_new_user: bool = False

    joined_club_id: str | None = None
    managed_club_id: str | None = None
    managed_club_numeric_id: int | None = None
    target_meeting_id: int | None = None
    target_participant_id: int | None = None

    extra: dict = field(default_factory=dict)


@dataclass
class SharedClub:
    id: int
    display_id: str
    owner_vu_id: int


@dataclass
class SharedMeeting:
    id: int
    club_id: int
    club_display_id: str
    owner_vu_id: int
    rounding_completed: bool = False
    settlement_confirmed: bool = False
    completed: bool = False


class StateStore:
    def __init__(self):
        self._lock = Lock()
        self._states: dict[int, UserRuntimeState] = {}
        self._clubs: dict[int, SharedClub] = {}
        self._meetings: dict[int, SharedMeeting] = {}

    def get_or_create(self, vu_id: int, run_id: str) -> UserRuntimeState:
        with self._lock:
            state = self._states.get(vu_id)
            if state is None:
                state = UserRuntimeState(vu_id=vu_id, run_id=run_id)
                self._states[vu_id] = state
            return state

    def upsert_club(self, club_id: int, display_id: str, owner_vu_id: int) -> None:
        with self._lock:
            self._clubs[club_id] = SharedClub(id=club_id, display_id=display_id, owner_vu_id=owner_vu_id)

    def get_random_club(self) -> SharedClub | None:
        with self._lock:
            if not self._clubs:
                return None
            return random.choice(list(self._clubs.values()))

    def upsert_meeting(self, meeting_id: int, club_id: int, club_display_id: str, owner_vu_id: int) -> None:
        with self._lock:
            existing = self._meetings.get(meeting_id)
            if existing:
                existing.club_id = club_id
                existing.club_display_id = club_display_id
                existing.owner_vu_id = owner_vu_id
                return
            self._meetings[meeting_id] = SharedMeeting(
                id=meeting_id,
                club_id=club_id,
                club_display_id=club_display_id,
                owner_vu_id=owner_vu_id,
            )

    def update_meeting_flags(
        self,
        meeting_id: int,
        rounding_completed: bool | None = None,
        settlement_confirmed: bool | None = None,
        completed: bool | None = None,
    ) -> None:
        with self._lock:
            meeting = self._meetings.get(meeting_id)
            if not meeting:
                return
            if rounding_completed is not None:
                meeting.rounding_completed = rounding_completed
            if settlement_confirmed is not None:
                meeting.settlement_confirmed = settlement_confirmed
            if completed is not None:
                meeting.completed = completed

    def get_random_meeting(self, include_completed: bool = False) -> SharedMeeting | None:
        with self._lock:
            meetings = list(self._meetings.values())
            if not include_completed:
                meetings = [m for m in meetings if not m.completed]
            if not meetings:
                return None
            return random.choice(meetings)
