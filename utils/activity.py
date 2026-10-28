"""
회원 활동 로그 (backend #50, backoffice docs/02.기획문서/기능명세서.md F-092)

- log(): 앱 API 가 성공 처리 뒤 부른다. 앱 요청의 트랜잭션과 섞이지 않도록 별도 세션으로 기록하고,
  기록이 실패해도 예외를 밖으로 내지 않는다 (앱 응답·스키마 불변).
- ActivityContextMiddleware: 요청의 IP·기기(X-Client-Type)·앱 버전(X-App-Version)을 담아 둔다.
- purge_expired(): 1년 지난 행을 나눠 지운다 (일일 배치).
"""
import contextvars
import logging
from collections.abc import Callable
from typing import Any
from datetime import timedelta

from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger(__name__)

EVENT_TYPES = (
    "SIGNUP",
    "WITHDRAW",
    "LOGIN",
    "PROFILE_UPDATE",
    "CLUB_JOIN_REQUEST",
    "CLUB_JOIN",
    "CLUB_LEAVE",
    "MEETING_JOIN",
    "MEETING_CANCEL",
    "PAYMENT",
    "REPORT",
)
RETENTION_DAYS = 365

_request_meta: contextvars.ContextVar[dict | None] = contextvars.ContextVar("activity_request", default=None)


def _default_factory() -> Session:
    from database import SessionLocal

    return SessionLocal()


# 테스트는 이 값을 메모리 DB 세션으로 바꾼다
session_factory: Callable[[], Session] = _default_factory


def _now():
    from utils.datetime_utils import get_kst_now

    return get_kst_now().replace(tzinfo=None)


def log(
    user: Any,
    event_type: str,
    summary: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    meta: dict | None = None,
) -> None:
    """user: 회원 id 또는 회원 객체. 관리자(Admin)가 앱 API 를 부른 경우는 남기지 않는다 (id 공간이 다르다)"""
    from models import Admin

    if isinstance(user, Admin):
        return
    user_id = user if isinstance(user, int) else getattr(user, "id", None)
    if not user_id:
        return
    if event_type not in EVENT_TYPES:
        logger.error(f"알 수 없는 활동 유형: {event_type}")
        return
    from models import UserActivityLog

    req = _request_meta.get() or {}
    merged = {k: v for k, v in {"device": req.get("device"), "app_version": req.get("app_version"), **(meta or {})}.items() if v is not None}
    db = None
    try:
        db = session_factory()
        db.add(
            UserActivityLog(
                user_id=user_id,
                event_type=event_type,
                target_type=target_type,
                target_id=target_id,
                summary=summary[:255],
                meta_json=merged or None,
                ip=req.get("ip"),
                created_at=_now(),
            )
        )
        db.commit()
    except Exception as e:
        if db is not None:
            db.rollback()
        logger.error(f"활동 로그 기록 실패 (user={user_id}, {event_type}): {e}")
    finally:
        if db is not None:
            db.close()


def purge_expired(db: Session, batch: int = 5000) -> int:
    """1년 지난 활동 로그 삭제. 한 번에 batch 건씩 반복 (긴 잠금 방지)"""
    from models import UserActivityLog

    cutoff = _now() - timedelta(days=RETENTION_DAYS)
    total = 0
    while True:
        ids = [r[0] for r in db.query(UserActivityLog.id).filter(UserActivityLog.created_at < cutoff).limit(batch).all()]
        if not ids:
            return total
        db.query(UserActivityLog).filter(UserActivityLog.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        total += len(ids)


class ActivityContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        from utils.audit import client_ip

        token = _request_meta.set(
            {
                "ip": client_ip(request) or None,
                "device": request.headers.get("x-client-type"),
                "app_version": request.headers.get("x-app-version"),
            }
        )
        try:
            return await call_next(request)
        finally:
            _request_meta.reset(token)
