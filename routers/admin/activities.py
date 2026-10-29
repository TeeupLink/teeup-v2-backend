"""
회원 활동 타임라인 (API스펙 API-066, 화면 SCR-031 활동 탭). 권한: users (USER_ADMIN write, SUPPORT_ADMIN read)
무한 스크롤이라 공통 envelope 대신 커서(created_at, id 내림차순)를 쓴다.
"""
import base64
from datetime import datetime, time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from database import get_db
from models import User, UserActivityLog
from utils.activity import EVENT_TYPES

from .deps import get_admin_user

router = APIRouter(tags=["admin-user-activities"])

_DATE = r"^\d{4}-\d{2}-\d{2}$"


def mask_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.***.***.{parts[3]}"
    # IPv6: 앞 두 마디만
    head = ip.split(":")[:2]
    return ":".join(head) + ":****"


def _encode(row: UserActivityLog) -> str:
    return base64.urlsafe_b64encode(f"{row.created_at.isoformat()}|{row.id}".encode()).decode()


def _decode(cursor: str) -> tuple[datetime, int]:
    try:
        at, rid = base64.urlsafe_b64decode(cursor.encode()).decode().rsplit("|", 1)
        return datetime.fromisoformat(at), int(rid)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="cursor 가 올바르지 않습니다") from e


@router.get("/users/{user_id}/activities")
async def list_user_activities(
    user_id: int,
    types: list[str] | None = Query(None),
    date_from: str | None = Query(None, alias="from", pattern=_DATE),
    date_to: str | None = Query(None, alias="to", pattern=_DATE),
    cursor: str | None = Query(None, max_length=200),
    size: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    _: dict = Depends(get_admin_user),
):
    if not db.query(User.id).filter(User.id == user_id).first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다")
    unknown = [t for t in types or [] if t not in EVENT_TYPES]
    if unknown:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"알 수 없는 활동 유형입니다: {', '.join(unknown)}")

    q = db.query(UserActivityLog).filter(UserActivityLog.user_id == user_id)
    if types:
        q = q.filter(UserActivityLog.event_type.in_(types))
    if date_from:
        q = q.filter(UserActivityLog.created_at >= datetime.combine(datetime.fromisoformat(date_from), time.min))
    if date_to:
        q = q.filter(UserActivityLog.created_at <= datetime.combine(datetime.fromisoformat(date_to), time.max))
    if cursor:
        at, rid = _decode(cursor)
        q = q.filter(or_(UserActivityLog.created_at < at, and_(UserActivityLog.created_at == at, UserActivityLog.id < rid)))

    rows = q.order_by(UserActivityLog.created_at.desc(), UserActivityLog.id.desc()).limit(size + 1).all()
    more = len(rows) > size
    rows = rows[:size]
    return {
        "items": [
            {
                "id": r.id,
                "event_type": r.event_type,
                "summary": r.summary,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "meta": r.meta_json,
                "ip_masked": mask_ip(r.ip),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "next_cursor": _encode(rows[-1]) if more and rows else None,
    }
