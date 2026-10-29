"""관리자 알림 벨 (backoffice F-016) — admin_notifications 에 행을 만든다.

처리 권한이 있는 활성 관리자에게만 보낸다 (최고 관리자 포함). 회원 알림(notifications, users FK)이나
푸시와는 상관없다. 회원 요청을 처리하는 도중에 부르므로 실패해도 예외를 밖으로 내지 않고,
회원 요청의 트랜잭션과 섞이지 않도록 별도 세션을 쓴다.
"""
import logging

from utils import activity

logger = logging.getLogger(__name__)

# 알림 유형 → 처리 권한 영역 (routers/admin/permissions.py)
RESOURCE_FOR = {
    "JOIN_REQUEST": "joinRequests",
    "INQUIRY": "inquiries",
    "REPORT": "reports",
    "BILLING_FAILURE": "payments",
}


def recipients(db, notification_type: str) -> list[int]:
    from models import Admin, UserStatus
    from routers.admin.permissions import READ, WRITE, can

    resource = RESOURCE_FOR[notification_type]
    admins = db.query(Admin.id, Admin.role).filter(Admin.deleted_at.is_(None), Admin.status == UserStatus.ACTIVE).all()
    # 결제 실패는 조회 권한만 있는 역할(고객지원)도 받아야 회원 문의에 답할 수 있다
    need = READ if notification_type == "BILLING_FAILURE" else WRITE
    return [a.id for a in admins if can(a.role.value if hasattr(a.role, "value") else a.role, resource, need)]


def notify(notification_type: str, title: str, content: str | None = None, link: str | None = None) -> int:
    """보낸 관리자 수를 돌려준다. 알 수 없는 유형이면 보내지 않는다"""
    if notification_type not in RESOURCE_FOR:
        logger.error(f"알 수 없는 관리자 알림 유형: {notification_type}")
        return 0
    from models import AdminNotification

    db = None
    try:
        db = activity.session_factory()
        ids = recipients(db, notification_type)
        now = activity._now()
        for admin_id in ids:
            db.add(AdminNotification(admin_id=admin_id, type=notification_type, title=title[:255], content=content, link=link, created_at=now))
        db.commit()
        return len(ids)
    except Exception as e:
        if db is not None:
            db.rollback()
        logger.error(f"관리자 알림 생성 실패 ({notification_type}): {e}")
        return 0
    finally:
        if db is not None:
            db.close()
