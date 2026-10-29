"""
백오피스 관리자(Admin) CRUD API — 최고 관리자 전용 (routers/admin/__init__.py 의 require_super_admin)

- 역할·상태는 정해진 값만 받는다 (예전에는 모르는 값을 조용히 무시했고, 역할을 빼면 최고 관리자로 만들었다)
- 마지막 활성 최고 관리자는 강등·비활성·삭제할 수 없다. 자기 자신의 역할·상태도 여기서 바꾸지 않는다
- 변경은 감사 로그에 전·후 값으로 남긴다 (비밀번호는 값 없이 사실만)
"""
import hashlib
import re

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import Admin, AdminAuditLog, AdminRole, Provider, UserStatus
from schemas import AdminCreate, AdminPasswordUpdate, AdminUpdate
from utils import audit
from utils.datetime_utils import get_kst_now

from .deps import get_admin_user

router = APIRouter(tags=["admin-admins"])

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_STATUSES = ("ACTIVE", "DEACTIVATED")


def _err(code: int, field: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail=[{"field": field, "message": message}])


def _check_password(pw: str, field: str) -> None:
    """회원 관리자 등록과 같은 규칙: 8~64자, 영문·숫자·특수문자 모두"""
    if not (8 <= len(pw) <= 64 and re.search(r"[A-Za-z]", pw) and re.search(r"\d", pw) and re.search(r"[^A-Za-z0-9]", pw)):
        raise _err(422, field, "비밀번호는 8자 이상이며 영문·숫자·특수문자를 모두 포함해야 합니다")


def _role(value: str | None) -> AdminRole:
    try:
        return AdminRole[value or ""]
    except KeyError:
        raise _err(422, "role", "역할을 선택해주세요")


def _v(x):
    return x.value if hasattr(x, "value") else x


def _row(a: Admin, last_login=None) -> dict:
    return {
        "id": a.id,
        "email": a.email,
        "name": a.name,
        "profile_image": a.profile_image,
        "phone_number": a.phone_number,
        "provider": _v(a.provider),
        "status": _v(a.status),
        # 역할이 비어 있으면 그대로 null — 권한 없음으로 다룬다 (예전 응답은 최고 관리자로 보였다)
        "role": _v(a.role),
        "last_login_at": last_login.isoformat() if last_login else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


def _snapshot(a: Admin) -> dict:
    return {"name": a.name, "phone_number": a.phone_number, "status": _v(a.status), "role": _v(a.role)}


def _get(db: Session, admin_id: int) -> Admin:
    a = db.query(Admin).filter(Admin.id == admin_id, Admin.deleted_at.is_(None)).first()
    if not a:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="관리자를 찾을 수 없습니다")
    return a


def _last_logins(db: Session, ids: list[int]) -> dict:
    if not ids:
        return {}
    rows = (db.query(AdminAuditLog.actor_admin_id, func.max(AdminAuditLog.created_at))
            .filter(AdminAuditLog.actor_admin_id.in_(ids), AdminAuditLog.action == "LOGIN")
            .group_by(AdminAuditLog.actor_admin_id).all())
    return dict(rows)


def _guard_last_super(db: Session, a: Admin, *, losing: bool) -> None:
    """a 가 최고 관리자 자격을 잃는 변경이면, 남는 활성 최고 관리자가 있어야 한다"""
    if not losing or a.role != AdminRole.SUPER_ADMIN or a.status != UserStatus.ACTIVE:
        return
    others = db.query(Admin).filter(Admin.id != a.id, Admin.deleted_at.is_(None), Admin.role == AdminRole.SUPER_ADMIN,
                                    Admin.status == UserStatus.ACTIVE).count()
    if others == 0:
        raise HTTPException(status_code=409, detail="마지막 최고 관리자는 강등·비활성·삭제할 수 없습니다. 다른 최고 관리자를 먼저 지정하세요.")


@router.get("/admins")
async def get_admin_list(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    search: str | None = Query(None),
    q: str | None = Query(None, description="이메일·이름 (search 와 같음)"),
    status_filter: str | None = Query(None),
    role: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자 목록 (최근 로그인 포함)"""
    query = db.query(Admin).filter(Admin.deleted_at.is_(None))
    term = q or search
    if term:
        sp = f"%{term}%"
        query = query.filter((Admin.email.ilike(sp)) | (Admin.name.ilike(sp)))
    if status_filter in _STATUSES:
        query = query.filter(Admin.status == UserStatus[status_filter])
    if role == "NONE":
        query = query.filter(Admin.role.is_(None))
    elif role:
        query = query.filter(Admin.role == _role(role))
    total = query.count()
    admins = query.order_by(Admin.created_at.desc(), Admin.id.desc()).offset((page - 1) * limit).limit(limit).all()
    logins = _last_logins(db, [a.id for a in admins])
    return {"data": [_row(a, logins.get(a.id)) for a in admins], "total": total, "page": page, "limit": limit,
            "total_pages": (total + limit - 1) // limit if total > 0 else 0}


@router.get("/admins/{admin_id}")
async def get_admin_detail(admin_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    a = _get(db, admin_id)
    return _row(a, _last_logins(db, [a.id]).get(a.id))


@router.post("/admins", status_code=201)
async def create_admin(data: AdminCreate, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """새 관리자. 역할은 반드시 고른다"""
    email = data.email.strip().lower()
    if not _EMAIL.match(email):
        raise _err(422, "email", "이메일 형식이 올바르지 않습니다")
    if not data.name.strip():
        raise _err(422, "name", "이름을 입력해주세요")
    role = _role(data.role)
    _check_password(data.password, "password")
    if db.query(Admin).filter(func.lower(Admin.email) == email, Admin.deleted_at.is_(None)).first():
        raise _err(409, "email", "이미 등록된 이메일입니다")
    a = Admin(email=email, password=hashlib.sha256(data.password.encode()).hexdigest(), name=data.name.strip(),
              phone_number=(data.phone_number or "").strip() or None, profile_image=data.profile_image, provider=Provider.LOCAL,
              status=UserStatus.ACTIVE, role=role)
    db.add(a)
    db.commit()
    db.refresh(a)
    audit.record("CREATE", "ADMIN", a.id, after={"email": a.email, **_snapshot(a)})
    return _row(a)


@router.put("/admins/{admin_id}")
async def update_admin(admin_id: int, data: AdminUpdate, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    a = _get(db, admin_id)
    before = _snapshot(a)
    fields = data.model_fields_set
    self_edit = a.id == current_user.get("id")
    if "name" in fields:
        if not (data.name or "").strip():
            raise _err(422, "name", "이름을 입력해주세요")
        a.name = data.name.strip()
    if "phone_number" in fields:
        a.phone_number = (data.phone_number or "").strip() or None
    if "profile_image" in fields:
        a.profile_image = data.profile_image
    new_role = _role(data.role) if "role" in fields else a.role
    new_status = a.status
    if "status" in fields:
        if data.status not in _STATUSES:
            raise _err(422, "status", "상태는 활성 또는 비활성입니다")
        new_status = UserStatus[data.status]
    if self_edit and (new_role != a.role or new_status != a.status):
        raise HTTPException(status_code=400, detail="자기 자신의 역할·상태는 바꿀 수 없습니다. 다른 최고 관리자에게 요청하세요.")
    _guard_last_super(db, a, losing=new_role != AdminRole.SUPER_ADMIN or new_status != UserStatus.ACTIVE)
    a.role, a.status = new_role, new_status
    a.updated_at = get_kst_now()
    db.commit()
    db.refresh(a)
    after = _snapshot(a)
    changed = {k for k in after if after[k] != before[k]}
    if changed:
        action = "STATUS_CHANGE" if changed == {"status"} else "UPDATE"
        audit.record(action, "ADMIN", a.id, before={k: before[k] for k in changed}, after={k: after[k] for k in changed})
    return _row(a)


@router.put("/admins/{admin_id}/password")
async def update_admin_password_by_admin(admin_id: int, data: AdminPasswordUpdate, db: Session = Depends(get_db),
                                         current_user: dict = Depends(get_admin_user)):
    """다른 관리자의 비밀번호 재설정 (감사 로그에는 값 없이 사실만)"""
    a = _get(db, admin_id)
    _check_password(data.new_password, "new_password")
    a.password = hashlib.sha256(data.new_password.encode()).hexdigest()
    a.updated_at = get_kst_now()
    db.commit()
    audit.record("UPDATE", "ADMIN_PASSWORD", a.id, after={"password": "(재설정)"})
    return {"message": "비밀번호가 변경되었습니다", "success": True}


@router.delete("/admins/{admin_id}")
async def delete_admin(admin_id: int, reason: str | None = Query(None, max_length=500), db: Session = Depends(get_db),
                       current_user: dict = Depends(get_admin_user)):
    """관리자 삭제 (soft delete)"""
    if admin_id == current_user.get("id"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="자기 자신은 삭제할 수 없습니다")
    a = _get(db, admin_id)
    _guard_last_super(db, a, losing=True)
    before = {"email": a.email, **_snapshot(a)}
    a.deleted_at = get_kst_now()
    a.status = UserStatus.DELETED
    db.commit()
    audit.record("DELETE", "ADMIN", admin_id, before=before, reason=reason)
    return {"message": "관리자가 삭제되었습니다", "success": True}
