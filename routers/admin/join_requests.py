"""
가입신청 통합 목록·일괄 처리 (API스펙 API-030·031, 화면 SCR-020).
전 클럽의 PENDING 멤버십을 한 목록에서 처리한다. 권한: joinRequests (CLUB_ADMIN write)
"""
from datetime import datetime, time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import get_db
from models import Club, ClubMembership, MembershipStatus, User
from services import admin_membership_service as svc

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


@router.post("/join-requests/bulk")
async def bulk_join_requests(body: BulkRequest, db: Session = Depends(get_db), _: dict = Depends(get_admin_user)):
    if body.action not in ("approve", "reject"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="action 은 approve 또는 reject 입니다")
    handler = svc.approve if body.action == "approve" else svc.reject

    def one(membership_id: int) -> None:
        # 건별 커밋: 한 건 실패가 다른 건을 되돌리지 않는다
        try:
            handler(db, svc.find_pending(db, membership_id))
            db.commit()
        except Exception:
            db.rollback()
            raise

    return bulk_run(body.ids, one)
