"""
백오피스 알림 API
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
import logging

from database import get_db
from .deps import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-notifications"])


@router.get("/notifications")
async def get_admin_notifications(
    page: int = 1,
    limit: int = 20,
    status_filter: str | None = None,
    type_filter: str | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 알림 목록 조회"""
    try:
        from models import Notification, NotificationType, NotificationStatus
        from sqlalchemy import desc

        offset = (page - 1) * limit
        query = db.query(Notification).filter(Notification.user_id == current_user.get("id"))
        if status_filter:
            status_val = NotificationStatus(status_filter).value if hasattr(NotificationStatus(status_filter), "value") else str(NotificationStatus(status_filter))
            query = query.filter(Notification.status == status_val)
        if type_filter:
            type_val = NotificationType(type_filter).value if hasattr(NotificationType(type_filter), "value") else str(NotificationType(type_filter))
            query = query.filter(Notification.type == type_val)
        total = query.count()
        notifications = query.order_by(desc(Notification.created_at)).offset(offset).limit(limit).all()
        notification_responses = []
        for n in notifications:
            notification_responses.append({
                "id": n.id, "user_id": n.user_id,
                "type": n.type.value if hasattr(n.type, "value") else str(n.type),
                "title": n.title, "content": n.content,
                "status": n.status.value if hasattr(n.status, "value") else str(n.status),
                "read_at": n.read_at.isoformat() if n.read_at else None,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            })
        return notification_responses
    except Exception as e:
        logger.error(f"관리자 알림 목록 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="알림 목록 조회 중 오류가 발생했습니다")


@router.put("/notifications/{notification_id}/read")
async def mark_admin_notification_as_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 알림 읽음 처리"""
    try:
        from models import Notification, NotificationStatus

        notification = db.query(Notification).filter(
            Notification.id == notification_id,
            Notification.user_id == current_user.get("id"),
        ).first()
        if not notification:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="알림을 찾을 수 없습니다")
        notification.status = NotificationStatus.READ.value
        notification.read_at = datetime.now()
        db.commit()
        return {"message": "알림을 읽음으로 표시했습니다", "success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 알림 읽음 처리 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="알림 읽음 처리 중 오류가 발생했습니다")


@router.put("/notifications/read-all")
async def mark_all_admin_notifications_as_read(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 모든 알림 읽음 처리"""
    try:
        from models import Notification, NotificationStatus

        unread = db.query(Notification).filter(
            Notification.user_id == current_user.get("id"),
            Notification.status == NotificationStatus.UNREAD.value,
        ).all()
        for n in unread:
            n.status = NotificationStatus.READ.value
            n.read_at = datetime.now()
        db.commit()
        return {"message": f"{len(unread)}개의 알림을 읽음으로 표시했습니다", "success": True}
    except Exception as e:
        logger.error(f"관리자 모든 알림 읽음 처리 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="알림 읽음 처리 중 오류가 발생했습니다")
