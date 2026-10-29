"""
백오피스 헤더 전역 검색 API

볼 권한이 있는 영역만 찾는다 (예전에는 역할과 상관없이 회원·클럽·모임·관리자를 모두 돌려줬다).
link 는 새 백오피스 경로다 (라운딩 /rounds/:id, 소셜 /socials/:id, 클럽 /clubs/:id).
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from models import Admin, Club, Meeting, User

from .deps import get_admin_user
from .permissions import READ, can

router = APIRouter(tags=["admin-search"])

# 검색 영역 → 필요한 조회 권한
_RESOURCE = {"users": "users", "clubs": "clubs", "meetings": "meetings", "admins": None}


def _allowed(role: str | None, kind: str) -> bool:
    if kind == "admins":
        return role == "SUPER_ADMIN"
    return can(role, _RESOURCE[kind], READ)


def _v(x):
    return x.value if hasattr(x, "value") else x


@router.get("/search")
async def global_search(
    q: str = Query(..., min_length=1, max_length=100, description="검색어"),
    limit: int = Query(5, ge=1, le=20, description="영역별 최대 결과 수"),
    types: str | None = Query("users,clubs,meetings,admins", description="검색 대상 (쉼표 구분)"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """헤더 전역 검색. 개인정보(실명·이메일)는 화면에서 마스킹 모드를 거쳐 보여 준다"""
    result: dict[str, list] = {"users": [], "clubs": [], "meetings": [], "admins": []}
    term = q.strip()
    if not term:
        return result
    pattern = f"%{term}%"
    role = current_user.get("role")
    wanted = {t.strip().lower() for t in (types or "").split(",")} & set(result)
    kinds = [k for k in result if k in wanted and _allowed(role, k)]

    if "users" in kinds:
        rows = (db.query(User)
                .filter(User.deleted_at.is_(None), ~User.nickname.like("guest_%"), ~User.email.like("%@guest.local"))
                .filter(User.email.ilike(pattern) | User.nickname.ilike(pattern) | User.realname.ilike(pattern))
                .order_by(User.created_at.desc()).limit(limit).all())
        result["users"] = [{"id": u.id, "type": "user", "email": u.email, "nickname": u.nickname, "realname": u.realname, "link": f"/users/{u.id}"}
                           for u in rows]
    if "clubs" in kinds:
        rows = (db.query(Club).filter(Club.deleted_at.is_(None))
                .filter(Club.name.ilike(pattern) | (Club.description.isnot(None) & Club.description.ilike(pattern)))
                .order_by(Club.created_at.desc()).limit(limit).all())
        result["clubs"] = [{"id": c.id, "display_id": c.display_id, "type": "club", "name": c.name, "status": _v(c.status), "link": f"/clubs/{c.id}"}
                           for c in rows]
    if "meetings" in kinds:
        rows = (db.query(Meeting).filter(Meeting.club_id.isnot(None), Meeting.name.ilike(pattern))
                .order_by(Meeting.meeting_time.desc(), Meeting.id.desc()).limit(limit).all())
        result["meetings"] = [
            {"id": m.id, "type": "meeting", "meeting_type": _v(m.meeting_type), "name": m.name, "club_id": m.club_id,
             "meeting_time": m.meeting_time.isoformat() if m.meeting_time else None, "status": _v(m.status),
             "link": f"/{'socials' if _v(m.meeting_type) == 'SOCIAL' else 'rounds'}/{m.id}"}
            for m in rows
        ]
    if "admins" in kinds:
        rows = (db.query(Admin).filter(Admin.deleted_at.is_(None))
                .filter(Admin.email.ilike(pattern) | Admin.name.ilike(pattern))
                .order_by(Admin.created_at.desc()).limit(limit).all())
        result["admins"] = [{"id": a.id, "type": "admin", "email": a.email, "name": a.name, "link": f"/admins/{a.id}"} for a in rows]
    return result
