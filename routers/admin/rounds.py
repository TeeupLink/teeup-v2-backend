"""
백오피스 라운딩 보강 API (backoffice #23, F-053·F-054)

- PUT /meetings/{id}/teams/{tid}/members : 팀 멤버를 참가자 목록으로 지정 (다른 팀에 있던 참가자는 옮겨 온다)
- PUT /meetings/{id}/scores/bulk         : 총타 여러 건을 한 번에 저장. 건별 독립 처리, 공통 일괄 응답

meetings.py 의 PUT /meetings/{id}/scores/{participant_id} 보다 먼저 등록해야 한다 ("bulk" 가 participant_id 로 해석되지 않도록).
"""
import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import Meeting, MeetingParticipant, Team, TeamMember, UserScoreHistory
from schemas import MeetingType
from utils import audit
from utils.datetime_utils import get_kst_now

from .deps import get_admin_user
from .pagination import BULK_MAX, bulk_run

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-rounds"])

SCORE_MIN, SCORE_MAX = 55, 144


def get_round(db: Session, meeting_id: int) -> Meeting:
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")
    if getattr(meeting.meeting_type, "value", meeting.meeting_type) != MeetingType.ROUND.value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라운딩 모임만 지원합니다.")
    return meeting


def save_score(db: Session, meeting: Meeting, participant_id: int, gross: int) -> int | None:
    """참가자 총타 저장 (UserScoreHistory, 회원만). 이전 총타를 돌려준다. 커밋은 하고 핸디캡 갱신은 호출부가 한다"""
    from utils.handicap_calculator import get_user_handicap_for_formation

    p = db.query(MeetingParticipant).filter(MeetingParticipant.id == participant_id, MeetingParticipant.meeting_id == meeting.id).first()
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="참가자를 찾을 수 없습니다.")
    if not p.user_id:
        raise ValueError("게스트 참가자는 총타를 등록할 수 없습니다.")
    if not SCORE_MIN <= gross <= SCORE_MAX:
        raise ValueError(f"총타는 {SCORE_MIN}~{SCORE_MAX} 사이여야 합니다.")

    handicap_used = get_user_handicap_for_formation(db, p.user_id) or Decimal("0")
    net_score = Decimal(str(gross)) - handicap_used
    existing = db.query(UserScoreHistory).filter(UserScoreHistory.user_id == p.user_id, UserScoreHistory.meeting_id == meeting.id).first()
    before = existing.gross_score if existing else None
    if existing:
        existing.gross_score = gross
        existing.net_score = net_score
        existing.handicap_used = handicap_used
    else:
        db.add(UserScoreHistory(user_id=p.user_id, meeting_id=meeting.id, gross_score=gross, net_score=net_score,
                                handicap_used=handicap_used, played_at=meeting.meeting_time or get_kst_now()))
    db.commit()
    return before


def refresh_handicap(db: Session, user_id: int) -> None:
    from utils.handicap_calculator import update_user_handicap

    try:
        update_user_handicap(db=db, user_id=user_id, score_count=5)
    except Exception as ex:
        logger.warning(f"관리자 점수 저장 후 핸디캡 갱신 실패 (user={user_id}): {ex}")


class ScoreItem(BaseModel):
    participant_id: int
    score: int


class ScoreBulkBody(BaseModel):
    items: list[ScoreItem] = Field(..., min_length=1, max_length=BULK_MAX)
    reason: str | None = Field(None, max_length=500)


@router.put("/meetings/{meeting_id}/scores/bulk")
async def put_scores_bulk(meeting_id: int, body: ScoreBulkBody, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """총타 일괄 저장 (F-054). 한 건 실패가 나머지를 막지 않는다"""
    meeting = get_round(db, meeting_id)
    scores = {i.participant_id: i.score for i in body.items}
    touched: set[int] = set()

    def one(pid: int) -> None:
        try:
            before = save_score(db, meeting, pid, scores[pid])
        except Exception:
            db.rollback()
            raise
        uid = db.query(MeetingParticipant.user_id).filter(MeetingParticipant.id == pid).scalar()
        touched.add(uid)
        audit.record("UPDATE", "SCORE", pid, before={"score": before}, after={"score": scores[pid]}, reason=body.reason,
                     meta={"bulk": True, "meeting_id": meeting_id, "user_id": uid})

    result = bulk_run(list(scores), one, audit_as=("UPDATE", "SCORE"))
    for uid in touched:
        refresh_handicap(db, uid)
    return result


class TeamMembersBody(BaseModel):
    participant_ids: list[int] = Field(..., max_length=8)


def _member_key(user_id: int | None, guest_id: int | None) -> tuple:
    return ("g", guest_id) if guest_id else ("u", user_id)


@router.put("/meetings/{meeting_id}/teams/{team_id}/members")
async def put_team_members(meeting_id: int, team_id: int, body: TeamMembersBody, db: Session = Depends(get_db),
                           current_user: dict = Depends(get_admin_user)):
    """팀 멤버 지정 (F-053). 보낸 순서가 팀 내 순서. 다른 팀에 있던 참가자는 그 팀에서 빠진다"""
    get_round(db, meeting_id)
    team = db.query(Team).filter(Team.id == team_id, Team.meeting_id == meeting_id).first()
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="팀을 찾을 수 없습니다.")
    ids = list(dict.fromkeys(body.participant_ids))
    parts = {p.id: p for p in db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id, MeetingParticipant.id.in_(ids)).all()} if ids else {}
    missing = [i for i in ids if i not in parts]
    if missing:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"이 모임의 참가자가 아닙니다: {missing}")

    team_ids = [t.id for t in db.query(Team.id).filter(Team.meeting_id == meeting_id).all()]
    all_members = db.query(TeamMember).filter(TeamMember.team_id.in_(team_ids)).all()
    by_key = {p.id: _member_key(p.user_id, p.guest_id) for p in parts.values()}
    wanted = set(by_key.values())
    before = [m for m in all_members if m.team_id == team_id]
    before_keys = [_member_key(m.user_id, m.guest_id) for m in sorted(before, key=lambda m: m.order or 0)]
    moved_from = sorted({m.team_id for m in all_members if m.team_id != team_id and _member_key(m.user_id, m.guest_id) in wanted})

    for m in all_members:
        if m.team_id == team_id or _member_key(m.user_id, m.guest_id) in wanted:
            db.delete(m)
    db.flush()
    for order, pid in enumerate(ids, start=1):
        p = parts[pid]
        db.add(TeamMember(team_id=team_id, user_id=None if p.guest_id else p.user_id, guest_id=p.guest_id, order=order))
    db.commit()
    audit.record("UPDATE", "TEAM", team_id, before={"members": [list(k) for k in before_keys]}, after={"participant_ids": ids},
                 meta={"meeting_id": meeting_id, "moved_from_teams": moved_from})
    return {"team_id": team_id, "participant_ids": ids, "moved_from_teams": moved_from}
