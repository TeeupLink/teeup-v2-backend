"""
백오피스 접근 권한 표 — backoffice docs/02.기획문서/정보구조도.md 3장과 1:1.

조회(GET·HEAD)는 read, 그 외 메서드는 write 권한이 필요하다.
SUPER_ADMIN 은 항상 write. 역할이 비어 있으면 아무 권한도 없다.
프론트(src/lib/auth/permissions.ts)도 같은 표를 쓰며, /admin/me 응답의 permissions 로 내려준다.
"""
from fastapi import Depends, HTTPException, Request, status

from models import AdminRole

from .deps import get_admin_user

READ = "read"
WRITE = "write"

_ALL_READ = {
    AdminRole.CLUB_ADMIN: READ,
    AdminRole.MEETING_ADMIN: READ,
    AdminRole.USER_ADMIN: READ,
    AdminRole.CONTENT_ADMIN: READ,
    AdminRole.SUPPORT_ADMIN: READ,
}

PERMISSIONS: dict[str, dict[AdminRole, str]] = {
    "dashboard": _ALL_READ,
    "kpi": {},
    "joinRequests": {AdminRole.CLUB_ADMIN: WRITE},
    "inquiries": {AdminRole.SUPPORT_ADMIN: WRITE},
    "reports": {AdminRole.SUPPORT_ADMIN: WRITE},
    "users": {AdminRole.USER_ADMIN: WRITE, AdminRole.SUPPORT_ADMIN: READ},
    "clubs": {AdminRole.CLUB_ADMIN: WRITE, AdminRole.MEETING_ADMIN: READ},
    "meetings": {AdminRole.MEETING_ADMIN: WRITE, AdminRole.CLUB_ADMIN: READ},
    "content": {AdminRole.CONTENT_ADMIN: WRITE},
    "payments": {AdminRole.SUPPORT_ADMIN: READ},
    "billingSandbox": {},
    "admins": {},
    "auditLogs": {},
}

_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def _role(value: str | None) -> AdminRole | None:
    try:
        return AdminRole(value) if value else None
    except ValueError:
        return None


def access_for(role_value: str | None, resource: str) -> str | None:
    role = _role(role_value)
    if role is None:
        return None
    if role == AdminRole.SUPER_ADMIN:
        return WRITE
    return PERMISSIONS[resource].get(role)


def permissions_for(role_value: str | None) -> dict[str, str | None]:
    return {resource: access_for(role_value, resource) for resource in PERMISSIONS}


def can(role_value: str | None, resource: str, need: str = READ) -> bool:
    got = access_for(role_value, resource)
    return got == WRITE or (need == READ and got == READ)


def require_resource(resource: str):
    """라우터 단위로 붙이는 권한 의존성. HTTP 메서드로 read/write 를 판단한다"""
    if resource not in PERMISSIONS:
        raise ValueError(f"알 수 없는 권한 영역: {resource}")

    def _check(request: Request, current_user: dict = Depends(get_admin_user)) -> dict:
        need = READ if request.method in _READ_METHODS else WRITE
        if not can(current_user.get("role"), resource, need):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="이 기능에 대한 접근 권한이 없습니다.")
        return current_user

    return _check
