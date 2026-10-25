"""
백오피스 헤더 전역 검색 API
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
import logging

from database import get_db
from models import Admin, User, Club, Meeting
from .deps import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-search"])


@router.get("/search")
async def global_search(
    q: str = Query(..., min_length=1, max_length=100, description="검색어"),
    limit: int = Query(5, ge=1, le=20, description="카테고리별 최대 결과 수"),
    types: str | None = Query("users,clubs,meetings,admins", description="검색 대상 (쉼표 구분)"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """백오피스 헤더 전역 검색"""
    if not q or not q.strip():
        return {"users": [], "clubs": [], "meetings": [], "admins": []}
    q = q.strip()
    pattern = f"%{q}%"
    result = {"users": [], "clubs": [], "meetings": [], "admins": []}
    target_types = [t.strip().lower() for t in types.split(",") if t.strip()]
    try:
        if "users" in target_types:
            users = (
                db.query(User)
                .filter(User.deleted_at.is_(None), ~User.nickname.like("guest_%"), ~User.email.like("%@guest.local"))
                .filter((User.email.ilike(pattern)) | (User.nickname.ilike(pattern)) | (User.realname.ilike(pattern)))
                .order_by(User.created_at.desc())
                .limit(limit)
                .all()
            )
            result["users"] = [
                {"id": u.id, "type": "user", "email": u.email, "nickname": u.nickname, "realname": getattr(u, "realname", None), "link": f"/users/{u.id}"}
                for u in users
            ]
        if "clubs" in target_types:
            clubs = (
                db.query(Club)
                .filter(Club.deleted_at.is_(None))
                .filter((Club.name.ilike(pattern)) | ((Club.description.isnot(None)) & (Club.description.ilike(pattern))))
                .order_by(Club.created_at.desc())
                .limit(limit)
                .all()
            )
            result["clubs"] = [
                {"id": c.id, "display_id": c.display_id, "type": "club", "name": c.name, "status": c.status.value if c.status else None, "link": f"/clubs/{c.display_id or c.id}"}
                for c in clubs
            ]
        if "meetings" in target_types:
            meetings = (
                db.query(Meeting)
                .filter(Meeting.club_id.isnot(None))
                .filter(Meeting.name.ilike(pattern))
                .order_by(Meeting.created_at.desc())
                .limit(limit)
                .all()
            )
            result["meetings"] = [
                {"id": m.id, "type": "meeting", "name": m.name, "club_id": m.club_id, "meeting_time": m.meeting_time.isoformat() if m.meeting_time else None, "status": m.status, "link": f"/meetings/{m.id}"}
                for m in meetings
            ]
        if "admins" in target_types:
            admins = (
                db.query(Admin)
                .filter(Admin.deleted_at.is_(None))
                .filter((Admin.email.ilike(pattern)) | (Admin.name.ilike(pattern)))
                .order_by(Admin.created_at.desc())
                .limit(limit)
                .all()
            )
            result["admins"] = [
                {"id": a.id, "type": "admin", "email": a.email, "name": a.name, "link": f"/admins/{a.id}"}
                for a in admins
            ]
        return result
    except Exception as e:
        logger.error(f"전역 검색 중 오류: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="검색 중 오류가 발생했습니다")
