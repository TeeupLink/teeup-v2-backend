"""
백오피스 신고·차단 API (backoffice #29, SCR-023~025)

신고는 회원이 앱에서 만든다(routers/moderation.py). 운영자는 여기서 대상 확인 → 검토 → 처리/기각 한다.
처리·기각은 메모(사유)를 반드시 남기고, 상태 변경은 감사 로그에 전·후 값으로 남긴다.
대상에 대한 실제 조치(회원 비활성, 클럽 비활성 등)는 각 화면의 기존 API 로 한다 — 여기서 대상을 바꾸지 않는다.
차단은 회원끼리의 기능이라 운영자는 조회하고, 회원 요청이 있을 때만 사유를 남기고 해제한다.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, aliased

from database import get_db
from models import Club, ClubNotice, ContentReport, Meeting, Regulation, User, UserBlock
from models.enums import ReportReason, ReportStatus, ReportTargetType
from utils import audit
from utils.datetime_utils import get_kst_now

from .deps import get_admin_user
from .pagination import BulkRequest, PageParams, apply_sort, bulk_run, paginate

router = APIRouter(tags=["admin-reports"])

_CLOSED = {ReportStatus.RESOLVED, ReportStatus.DISMISSED}


def _v(x):
    return x.value if hasattr(x, "value") else x


def _targets(db: Session, reports: list[ContentReport]) -> dict:
    """(대상 종류, id) → 화면에 보여 줄 요약. 지워진 대상은 exists=False"""
    want: dict[ReportTargetType, set[int]] = {}
    for r in reports:
        want.setdefault(r.target_type, set()).add(r.target_id)
    out: dict[tuple, dict] = {}

    def put(t, i, name, link, state=None, club_id=None):
        out[(t, i)] = {"type": t.value, "id": i, "name": name, "link": link, "status": state, "club_id": club_id, "exists": True}

    if ids := want.get(ReportTargetType.USER):
        for u in db.query(User).filter(User.id.in_(ids)).all():
            put(ReportTargetType.USER, u.id, u.nickname, f"/users/{u.id}", _v(u.status))
    if ids := want.get(ReportTargetType.CLUB):
        for c in db.query(Club).filter(Club.id.in_(ids)).all():
            put(ReportTargetType.CLUB, c.id, c.name, f"/clubs/{c.id}", "DELETED" if c.deleted_at else _v(c.status))
    if ids := want.get(ReportTargetType.MEETING):
        for m in db.query(Meeting).filter(Meeting.id.in_(ids)).all():
            path = "socials" if _v(m.meeting_type) == "SOCIAL" else "rounds"
            put(ReportTargetType.MEETING, m.id, m.name, f"/{path}/{m.id}", _v(m.status), m.club_id)
    if ids := want.get(ReportTargetType.CLUB_NOTICE):
        for n in db.query(ClubNotice).filter(ClubNotice.id.in_(ids)).all():
            put(ReportTargetType.CLUB_NOTICE, n.id, n.title, f"/clubs/{n.club_id}/notices/{n.id}/edit", None, n.club_id)
    if ids := want.get(ReportTargetType.REGULATION):
        for g in db.query(Regulation).filter(Regulation.id.in_(ids)).all():
            put(ReportTargetType.REGULATION, g.id, g.title, f"/clubs/{g.club_id}/regulations", None, g.club_id)
    for t, ids in want.items():
        for i in ids:
            out.setdefault((t, i), {"type": t.value, "id": i, "name": None, "link": None, "status": None, "club_id": None, "exists": False})
    return out


def _same_target_counts(db: Session, reports: list[ContentReport]) -> dict:
    keys = {(r.target_type, r.target_id) for r in reports}
    if not keys:
        return {}
    rows = (db.query(ContentReport.target_type, ContentReport.target_id, func.count(ContentReport.id))
            .filter(or_(*[(ContentReport.target_type == t) & (ContentReport.target_id == i) for t, i in keys]))
            .group_by(ContentReport.target_type, ContentReport.target_id).all())
    return {(t, i): n for t, i, n in rows}


def _rows(db: Session, reports: list[ContentReport]) -> list[dict]:
    targets = _targets(db, reports)
    counts = _same_target_counts(db, reports)
    reporter_ids = {r.reporter_id for r in reports}
    reporters = {u.id: u for u in db.query(User).filter(User.id.in_(reporter_ids)).all()} if reporter_ids else {}
    out = []
    for r in reports:
        u = reporters.get(r.reporter_id)
        out.append({
            "id": r.id,
            "status": _v(r.status),
            "reason": _v(r.reason),
            "description": r.description,
            "admin_note": r.admin_note,
            "reporter": {"id": u.id, "nickname": u.nickname, "realname": u.realname} if u else None,
            "target": targets[(r.target_type, r.target_id)],
            "same_target_count": counts.get((r.target_type, r.target_id), 1),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        })
    return out


def _enum(enum_cls, values: list[str], field: str):
    try:
        return [enum_cls(v) for v in values]
    except ValueError:
        raise HTTPException(status_code=422, detail=f"알 수 없는 {field} 값입니다")


@router.get("/reports")
async def list_reports(
    params: PageParams = Depends(),
    status_: list[str] = Query([], alias="status"),
    target_type: str | None = Query(None),
    reason: str | None = Query(None),
    target_id: int | None = Query(None),
    reporter_id: int | None = Query(None),
    date_from: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """신고 목록 (기본 최신순). status 는 여러 개 줄 수 있다"""
    q = db.query(ContentReport)
    if status_:
        q = q.filter(ContentReport.status.in_(_enum(ReportStatus, status_, "상태")))
    if target_type:
        q = q.filter(ContentReport.target_type == _enum(ReportTargetType, [target_type], "대상")[0])
    if reason:
        q = q.filter(ContentReport.reason == _enum(ReportReason, [reason], "사유")[0])
    if target_id:
        q = q.filter(ContentReport.target_id == target_id)
    if reporter_id:
        q = q.filter(ContentReport.reporter_id == reporter_id)
    if params.q:
        q = q.filter(ContentReport.description.contains(params.q))
    if date_from:
        q = q.filter(ContentReport.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
    if date_to:
        q = q.filter(ContentReport.created_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1))
    q = apply_sort(q, params.sort, {"created_at": ContentReport.created_at}, (ContentReport.created_at.desc(), ContentReport.id.desc()))
    page = paginate(q, params, lambda r: r)
    page["items"] = _rows(db, page["items"])
    return page


def _get(db: Session, report_id: int) -> ContentReport:
    r = db.query(ContentReport).filter(ContentReport.id == report_id).first()
    if not r:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="신고를 찾을 수 없습니다")
    return r


@router.get("/reports/{report_id}")
async def get_report(report_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """신고 상세 + 같은 대상의 다른 신고 (최근 20건)"""
    r = _get(db, report_id)
    others = (db.query(ContentReport)
              .filter(ContentReport.target_type == r.target_type, ContentReport.target_id == r.target_id, ContentReport.id != r.id)
              .order_by(ContentReport.created_at.desc()).limit(20).all())
    detail = _rows(db, [r])[0]
    detail["same_target"] = _rows(db, others)
    return detail


class ReportUpdate(BaseModel):
    status: str
    admin_note: str | None = Field(None, max_length=2000)


def _change(db: Session, r: ContentReport, new_status: str, note: str | None) -> None:
    try:
        target = ReportStatus(new_status)
    except ValueError:
        raise HTTPException(status_code=422, detail=[{"field": "status", "message": "알 수 없는 상태입니다"}])
    note = (note or "").strip() or None
    if target in _CLOSED and not note:
        raise HTTPException(status_code=422, detail=[{"field": "admin_note", "message": "처리·기각은 메모를 남겨야 합니다"}])
    before = {"status": _v(r.status), "admin_note": r.admin_note}
    if r.status == target and (note is None or note == r.admin_note):
        return
    r.status = target
    if note is not None:
        r.admin_note = note
    r.updated_at = get_kst_now().replace(tzinfo=None)
    db.commit()
    action = "STATUS_CHANGE" if before["status"] != target.value else "UPDATE"
    audit.record(action, "REPORT", r.id, before=before, after={"status": target.value, "admin_note": r.admin_note},
                 reason=note, meta={"target": f"{_v(r.target_type)}#{r.target_id}"})


@router.put("/reports/{report_id}")
async def update_report(report_id: int, body: ReportUpdate, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    r = _get(db, report_id)
    _change(db, r, body.status, body.admin_note)
    return _rows(db, [r])[0]


@router.post("/reports/bulk")
async def bulk_reports(body: BulkRequest, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """여러 신고를 같은 상태로. action 은 status 로 쓴다 (reason = 메모)"""
    new_status = body.status or body.action

    def one(i: int):
        _change(db, _get(db, i), new_status, body.reason)

    return bulk_run(body.ids, one, audit_as=("STATUS_CHANGE", "REPORT"))


# ---- 차단 ----------------------------------------------------------------

@router.get("/blocks")
async def list_blocks(
    params: PageParams = Depends(),
    user_id: int | None = Query(None, description="차단했거나 차단당한 회원"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """회원 간 차단 목록 (최신순). q 는 두 회원의 닉네임"""
    blocker, blocked = aliased(User), aliased(User)
    q = (db.query(UserBlock, blocker, blocked)
         .join(blocker, blocker.id == UserBlock.blocker_id)
         .join(blocked, blocked.id == UserBlock.blocked_id))
    if user_id:
        q = q.filter(or_(UserBlock.blocker_id == user_id, UserBlock.blocked_id == user_id))
    if params.q:
        q = q.filter(or_(blocker.nickname.contains(params.q), blocked.nickname.contains(params.q)))
    q = q.order_by(UserBlock.created_at.desc(), UserBlock.id.desc())

    def row(t):
        b, a, c = t
        return {
            "id": b.id,
            "blocker": {"id": a.id, "nickname": a.nickname, "realname": a.realname},
            "blocked": {"id": c.id, "nickname": c.nickname, "realname": c.realname},
            "created_at": b.created_at.isoformat() if b.created_at else None,
        }

    return paginate(q, params, row)


@router.delete("/blocks/{block_id}")
async def delete_block(block_id: int, reason: str = Query(..., min_length=1, max_length=500), db: Session = Depends(get_db),
                       current_user: dict = Depends(get_admin_user)):
    """회원 요청으로 차단 해제. 사유 필수"""
    b = db.query(UserBlock).filter(UserBlock.id == block_id).first()
    if not b:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="차단 기록을 찾을 수 없습니다")
    snap = {"blocker_id": b.blocker_id, "blocked_id": b.blocked_id, "created_at": b.created_at.isoformat() if b.created_at else None}
    db.delete(b)
    db.commit()
    audit.record("DELETE", "USER_BLOCK", block_id, before=snap, reason=reason.strip())
    return {"message": "차단을 해제했습니다", "success": True}
