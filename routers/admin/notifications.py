"""
백오피스 알림 API (헤더 알림 벨, backoffice F-016)

관리자 알림은 admin_notifications(admins FK)에 있다. 예전 구현은 회원 알림(notifications, users FK)을
관리자 id 로 조회·읽음 처리해서, 번호가 같은 회원의 알림을 보여 주고 그 회원의 읽음 상태를 바꿨다.
응답 형태는 예전과 같다 (status: UNREAD/READ).
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from database import get_db
from models import AdminNotification
from utils.datetime_utils import get_kst_now

from .deps import get_admin_user

router = APIRouter(tags=["admin-notifications"])


def _row(n: AdminNotification) -> dict:
    return {
        "id": n.id,
        "type": n.type,
        "title": n.title,
        "content": n.content,
        "link": n.link,
        "status": "READ" if n.read_at else "UNREAD",
        "read_at": n.read_at.isoformat() if n.read_at else None,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


@router.get("/notifications")
async def get_admin_notifications(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, pattern="^(UNREAD|READ)$"),
    type_filter: str | None = Query(None, max_length=40),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """내 알림 (최신순)"""
    q = db.query(AdminNotification).filter(AdminNotification.admin_id == current_user["id"])
    if status_filter == "UNREAD":
        q = q.filter(AdminNotification.read_at.is_(None))
    elif status_filter == "READ":
        q = q.filter(AdminNotification.read_at.isnot(None))
    if type_filter:
        q = q.filter(AdminNotification.type == type_filter)
    rows = q.order_by(AdminNotification.created_at.desc(), AdminNotification.id.desc()).offset((page - 1) * limit).limit(limit).all()
    return [_row(n) for n in rows]


@router.get("/notifications/unread-count")
async def get_admin_unread_count(db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    n = db.query(AdminNotification).filter(AdminNotification.admin_id == current_user["id"], AdminNotification.read_at.is_(None)).count()
    return {"count": n}


@router.put("/notifications/read-all")
async def mark_all_admin_notifications_as_read(db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    unread = db.query(AdminNotification).filter(AdminNotification.admin_id == current_user["id"], AdminNotification.read_at.is_(None)).all()
    now = get_kst_now().replace(tzinfo=None)
    for n in unread:
        n.read_at = now
    db.commit()
    return {"message": f"{len(unread)}개의 알림을 읽음으로 표시했습니다", "success": True}


@router.put("/notifications/{notification_id}/read")
async def mark_admin_notification_as_read(notification_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    n = db.query(AdminNotification).filter(AdminNotification.id == notification_id, AdminNotification.admin_id == current_user["id"]).first()
    if not n:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="알림을 찾을 수 없습니다")
    if not n.read_at:
        n.read_at = get_kst_now().replace(tzinfo=None)
        db.commit()
    return {"message": "알림을 읽음으로 표시했습니다", "success": True}
