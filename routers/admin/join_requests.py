"""
가입신청 통합 목록·일괄 처리 (API스펙 API-030·031, 화면 SCR-020).
전 클럽의 PENDING 멤버십을 한 목록에서 처리한다. 권한: joinRequests (CLUB_ADMIN write)
"""
from datetime import datetime, time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import get_db
from models import AdminAuditLog, Club, ClubMembership, MembershipStatus, User
from services import admin_membership_service as svc
from utils import activity, audit

from .deps import get_admin_user
from .pagination import BulkRequest, PageParams, apply_sort, bulk_run, paginate

router = APIRouter(tags=["admin-join-requests"])

SORTABLE = {"created_at": ClubMembership.created_at}


def _item(row) -> dict:
    m, u, c = row
    return {
        "membership_id": m.id,
        "club": {"id": c.id, "display_id": c.display_id, "name": c.name},
        "user": {"id": u.id, "nickname": u.nickname, "name": u.realname, "average_score": u.average_score},
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def pending_query(db: Session):
    return (
        db.query(ClubMembership, User, Club)
        .join(User, User.id == ClubMembership.user_id)
        .join(Club, Club.id == ClubMembership.club_id)
        .filter(ClubMembership.status == MembershipStatus.PENDING, Club.deleted_at.is_(None), User.deleted_at.is_(None))
    )


@router.get("/join-requests")
async def list_join_requests(
    params: PageParams = Depends(),
    club_id: int | None = Query(None),
    date_from: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    db: Session = Depends(get_db),
    _: dict = Depends(get_admin_user),
):
    query = pending_query(db)
    if club_id:
        query = query.filter(ClubMembership.club_id == club_id)
    if params.q:
        query = query.filter(or_(User.nickname.like(f"%{params.q}%"), User.realname.like(f"%{params.q}%")))
    if date_from:
        query = query.filter(ClubMembership.created_at >= datetime.combine(datetime.fromisoformat(date_from), time.min))
    if date_to:
        query = query.filter(ClubMembership.created_at <= datetime.combine(datetime.fromisoformat(date_to), time.max))
    query = apply_sort(query, params.sort, SORTABLE, default=(ClubMembership.created_at.desc(), ClubMembership.id.desc()))
    return paginate(query, params, _item)


def _history_item(log: AdminAuditLog) -> dict:
    b = log.before_json or {}
    return {
        "id": log.id,
        "membership_id": b.get("membership_id") or log.target_id,
        "action": log.action,
        "club": {"id": b.get("club_id"), "name": b.get("club_name")},
        "user": {"id": b.get("user_id"), "nickname": b.get("user_nickname"), "name": b.get("user_name")},
        "reason": log.reason,
        "processed_by": {"id": log.actor_admin_id, "email": log.actor_email},
        "requested_at": b.get("requested_at"),
        "processed_at": log.created_at.isoformat() if log.created_at else None,
    }


@router.get("/join-requests/history")
async def join_request_history(
    params: PageParams = Depends(),
    action: str | None = Query(None, pattern="^(APPROVE|REJECT)$"),
    club_id: int | None = Query(None),
    date_from: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    db: Session = Depends(get_db),
    _: dict = Depends(get_admin_user),
):
    """처리 이력 (API-032). 거절은 행이 지워지므로 감사 로그의 before 스냅샷으로 보여 준다"""
    query = db.query(AdminAuditLog).filter(
        AdminAuditLog.target_type == "CLUB_MEMBERSHIP",
        AdminAuditLog.action.in_([action] if action else ["APPROVE", "REJECT"]),
        AdminAuditLog.result == "SUCCESS",
    )
    if club_id:
        query = query.filter(AdminAuditLog.before_json["club_id"].as_integer() == club_id)
    if params.q:
        query = query.filter(
            or_(
                AdminAuditLog.before_json["user_nickname"].as_string().like(f"%{params.q}%"),
                AdminAuditLog.before_json["user_name"].as_string().like(f"%{params.q}%"),
            )
        )
    if date_from:
        query = query.filter(AdminAuditLog.created_at >= datetime.combine(datetime.fromisoformat(date_from), time.min))
    if date_to:
        query = query.filter(AdminAuditLog.created_at <= datetime.combine(datetime.fromisoformat(date_to), time.max))
    query = apply_sort(query, params.sort, {"processed_at": AdminAuditLog.created_at}, default=(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc()))
    return paginate(query, params, _history_item)


@router.post("/join-requests/bulk")
async def bulk_join_requests(body: BulkRequest, db: Session = Depends(get_db), _: dict = Depends(get_admin_user)):
    if body.action not in ("approve", "reject"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="action 은 approve 또는 reject 입니다")
    handler = svc.approve if body.action == "approve" else svc.reject
    action = "APPROVE" if body.action == "approve" else "REJECT"

    def one(membership_id: int) -> None:
        # 건별 커밋: 한 건 실패가 다른 건을 되돌리지 않는다
        try:
            membership = svc.find_pending(db, membership_id)
            before = svc.describe(db, membership)
            handler(db, membership)
            db.commit()
        except Exception:
            db.rollback()
            raise
        after = {"status": "ACTIVE"} if action == "APPROVE" else None
        if action == "APPROVE":
            activity.log(before["user_id"], "CLUB_JOIN", f"{before['club_name']} 가입", target_type="CLUB", target_id=before["club_id"])
        audit.record(action, "CLUB_MEMBERSHIP", membership_id, before=before, after=after, reason=body.reason, meta={"bulk": True})

    return bulk_run(body.ids, one, audit_as=(action, "CLUB_MEMBERSHIP"))
