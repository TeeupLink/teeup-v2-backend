"""
관리자 감사 로그 (backend #49, backoffice docs/02.기획문서/API스펙.md 4장)

- AuditMiddleware: /api/v1/admin/ 의 모든 변경 요청(POST·PUT·PATCH·DELETE)을 요청 1건당 1행 기록한다.
  조회(GET)는 record() 로 남긴 항목(VIEW_PII·EXPORT)이 있을 때만 기록한다.
- record(): 서비스·라우터가 대상·변경 전후·사유를 남긴다. 같은 요청의 항목은 request_id 를 공유하고,
  일괄 처리는 건별로 1행씩 남는다. record() 가 있으면 미들웨어의 기본 행 대신 이 항목들이 기록된다.
- 기록 실패는 애플리케이션 로그로만 남기고 응답에는 영향을 주지 않는다.
"""
import contextvars
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger(__name__)

ADMIN_PREFIX = "/api/v1/admin/"
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_DEFAULT_ACTION = {"POST": "CREATE", "PUT": "UPDATE", "PATCH": "UPDATE", "DELETE": "DELETE"}

# 경로 첫 세그먼트 → target_type (record() 가 없는 요청의 기본 행에 쓴다)
_TARGET_BY_SEGMENT = {
    "users": "USER",
    "admins": "ADMIN",
    "clubs": "CLUB",
    "join-requests": "CLUB_MEMBERSHIP",
    "meetings": "MEETING",
    "rounds": "MEETING",
    "socials": "MEETING",
    "scores": "SCORE",
    "notices": "NOTICE",
    "faq": "FAQ",
    "faqs": "FAQ",
    "terms": "TERMS",
    "inquiries": "INQUIRY",
    "reports": "REPORT",
    "payments": "PAYMENT",
    "plans": "PLAN",
    "subscriptions": "SUBSCRIPTION",
    "billing": "BILLING",
    "audit-logs": "AUDIT",
    "profile": "ADMIN",
}

# 키 이름 부분 일치로 값 제거 (재귀)
_SENSITIVE = ("password", "token", "billing_key", "billingkey", "secret", "card_number")


@dataclass
class _Entry:
    action: str
    target_type: str | None
    target_id: str | None
    before: Any = None
    after: Any = None
    reason: str | None = None
    meta: dict | None = None
    result: str | None = None


@dataclass
class AuditContext:
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    actor_id: int | None = None
    actor_email: str | None = None
    actor_role: str | None = None
    entries: list[_Entry] = field(default_factory=list)


_ctx: contextvars.ContextVar[AuditContext | None] = contextvars.ContextVar("admin_audit", default=None)


def current() -> AuditContext | None:
    return _ctx.get()


def set_actor(admin: dict | None = None, *, email: str | None = None) -> None:
    """요청의 행위자를 정한다. 권한 의존성이 호출하며, 로그인은 실패해도 시도한 이메일을 남긴다"""
    ctx = _ctx.get()
    if ctx is None:
        return
    if admin:
        ctx.actor_id = admin.get("id")
        ctx.actor_email = admin.get("email")
        ctx.actor_role = admin.get("role")
    elif email:
        ctx.actor_email = email


def record(
    action: str,
    target_type: str | None = None,
    target_id: Any = None,
    *,
    before: Any = None,
    after: Any = None,
    reason: str | None = None,
    meta: dict | None = None,
    result: str | None = None,
) -> None:
    """감사 항목을 남긴다. 미들웨어 밖(테스트·배치)에서 부르면 아무 일도 하지 않는다"""
    ctx = _ctx.get()
    if ctx is None:
        return
    ctx.entries.append(
        _Entry(
            action=action,
            target_type=target_type,
            target_id=None if target_id is None else str(target_id),
            before=sanitize(before),
            after=sanitize(after),
            reason=(reason or None) and reason[:500],
            meta=sanitize(meta),
            result=result,
        )
    )


def snapshot(obj, fields: tuple[str, ...]) -> dict:
    """ORM 객체에서 지정 필드만 JSON 으로 옮길 수 있는 값으로 뽑는다"""
    return {f: _plain(getattr(obj, f, None)) for f in fields}


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if any(s in str(k).lower() for s in _SENSITIVE) else sanitize(v)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    return _plain(value)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "")[:45]


def _default_target(path: str) -> tuple[str | None, str | None]:
    rest = path[len(ADMIN_PREFIX):].strip("/").split("/")
    target_type = _TARGET_BY_SEGMENT.get(rest[0]) if rest else None
    target_id = rest[1] if len(rest) > 1 and rest[1] not in ("bulk",) else None
    return target_type, target_id


def _default_action(method: str, path: str, status_code: int) -> str:
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    if tail == "login":
        return "LOGIN" if status_code < 400 else "LOGIN_FAIL"
    if tail == "logout":
        return "LOGOUT"
    if tail == "approve":
        return "APPROVE"
    if tail == "reject":
        return "REJECT"
    if tail == "status":
        return "STATUS_CHANGE"
    return _DEFAULT_ACTION.get(method, method)


def build_rows(ctx: AuditContext, *, method: str, path: str, status_code: int, ip: str, user_agent: str | None, now: datetime):
    from models import AdminAuditLog

    ok = "SUCCESS" if status_code < 400 else "FAIL"
    entries = ctx.entries
    if not entries:
        target_type, target_id = _default_target(path)
        entries = [_Entry(action=_default_action(method, path, status_code), target_type=target_type, target_id=target_id)]
    return [
        AdminAuditLog(
            request_id=ctx.request_id,
            actor_admin_id=ctx.actor_id,
            actor_email=(ctx.actor_email or "")[:255],
            actor_role=ctx.actor_role,
            action=e.action,
            target_type=e.target_type,
            target_id=e.target_id,
            before_json=e.before,
            after_json=e.after,
            reason=e.reason,
            meta_json=e.meta,
            method=method,
            path=path[:255],
            status_code=status_code,
            result=e.result or ok,
            ip=ip,
            user_agent=(user_agent or None) and user_agent[:500],
            created_at=now,
        )
        for e in entries
    ]


class AuditMiddleware(BaseHTTPMiddleware):
    """세션은 앱의 get_db 를 그대로 쓴다 (테스트의 dependency_overrides 도 따른다)"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith(ADMIN_PREFIX) or request.method == "OPTIONS":
            return await call_next(request)

        ctx = AuditContext()
        token = _ctx.set(ctx)
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            _ctx.reset(token)
            should_write = request.method in _WRITE_METHODS or bool(ctx.entries)
            # 행위자를 모르는 요청(토큰 없음·만료)은 로그인 시도가 아니면 남기지 않는다
            if should_write and (ctx.actor_email or _default_action(request.method, path, status_code) == "LOGIN_FAIL"):
                await run_in_threadpool(
                    self._write,
                    request,
                    ctx,
                    status_code,
                )

    @staticmethod
    def _write(request: Request, ctx: AuditContext, status_code: int) -> None:
        from database import get_db
        from utils.datetime_utils import get_kst_now

        factory = request.app.dependency_overrides.get(get_db, get_db)
        gen = factory()
        db = next(gen)
        try:
            db.add_all(
                build_rows(
                    ctx,
                    method=request.method,
                    path=request.url.path,
                    status_code=status_code,
                    ip=client_ip(request),
                    user_agent=request.headers.get("user-agent"),
                    now=get_kst_now().replace(tzinfo=None),
                )
            )
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"감사 로그 기록 실패 ({request.method} {request.url.path}): {e}")
        finally:
            gen.close()
