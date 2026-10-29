"""
선택용 조회 (API스펙 API-008~010).

다른 역할 화면에서 회원·클럽을 '고르기'만 할 때 쓴다. 예: CLUB_ADMIN 이 클럽 대표자를 고를 때,
MEETING_ADMIN 이 라운딩의 클럽을 고를 때. 예전에는 /admin/users/search, /admin/clubs 를 불러 403 이 났다.
개인정보는 마스킹한 최소 항목만 돌려준다.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import get_db
from models import Club, ClubMembership, MembershipStatus, User, UserStatus

from .deps import get_admin_user
from .pagination import PageParams

router = APIRouter(tags=["admin-lookup"])


def mask_name(name: str | None) -> str | None:
    if not name:
        return None
    chars = list(name.strip())
    if len(chars) <= 1:
        return "*"
    if len(chars) == 2:
        return chars[0] + "*"
    return chars[0] + "*" * (len(chars) - 2) + chars[-1]


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    keep = 1 if len(local) <= 2 else 2
    return f"{local[:keep]}***@{domain}"


def _user_item(u: User) -> dict:
    return {
        "id": u.id,
        "nickname": u.nickname,
        "name_masked": mask_name(u.realname),
        "email_masked": mask_email(u.email),
        "average_score": u.average_score,
        "status": u.status.value if u.status else None,
    }


@router.get("/lookup/users")
async def lookup_users(params: PageParams = Depends(), db: Session = Depends(get_db), _: dict = Depends(get_admin_user)):
    if not params.q or len(params.q) < 2:
        return {"items": [], "total": 0, "page": params.page, "size": params.size}
    like = f"{params.q}%"
    query = (
        db.query(User)
        .filter(User.deleted_at.is_(None), User.status != UserStatus.DELETED, ~User.nickname.like("guest_%"))
        .filter(or_(User.nickname.like(like), User.realname.like(like), User.email.like(like)))
        .order_by(User.nickname.asc())
    )
    total = query.count()
    rows = query.offset(params.offset).limit(params.size).all()
    return {"items": [_user_item(u) for u in rows], "total": total, "page": params.page, "size": params.size}


@router.get("/lookup/clubs")
async def lookup_clubs(
    params: PageParams = Depends(),
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
    _: dict = Depends(get_admin_user),
):
    query = db.query(Club).filter(Club.deleted_at.is_(None))
    if not include_inactive:
        from models import ClubStatus

        query = query.filter(Club.status == ClubStatus.ACTIVE)
    if params.q:
        query = query.filter(Club.name.like(f"%{params.q}%"))
    query = query.order_by(Club.name.asc())
    total = query.count()
    rows = query.offset(params.offset).limit(params.size).all()
    items = [{"id": c.id, "display_id": c.display_id, "name": c.name, "status": c.status.value if c.status else None} for c in rows]
    return {"items": items, "total": total, "page": params.page, "size": params.size}


@router.get("/lookup/clubs/{club_id}/members")
async def lookup_club_members(club_id: int, params: PageParams = Depends(), db: Session = Depends(get_db), _: dict = Depends(get_admin_user)):
    if not db.query(Club.id).filter(Club.id == club_id, Club.deleted_at.is_(None)).first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다")
    query = (
        db.query(User, ClubMembership.role)
        .join(ClubMembership, ClubMembership.user_id == User.id)
        .filter(ClubMembership.club_id == club_id, ClubMembership.status == MembershipStatus.ACTIVE, User.deleted_at.is_(None))
    )
    if params.q:
        query = query.filter(or_(User.nickname.like(f"%{params.q}%"), User.realname.like(f"%{params.q}%")))
    query = query.order_by(User.nickname.asc())
    total = query.count()
    rows = query.offset(params.offset).limit(params.size).all()
    items = [{**_user_item(u), "club_role": role.value if role else None} for u, role in rows]
    return {"items": items, "total": total, "page": params.page, "size": params.size}
