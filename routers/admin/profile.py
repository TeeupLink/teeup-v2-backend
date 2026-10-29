"""
백오피스 내 프로필 API (모든 관리자)
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from models import Admin
from utils import audit
from utils.datetime_utils import get_kst_now
from utils.passwords import hash_password, verify_password

from .admins import _check_password, _err, _last_logins, _row
from .deps import get_admin_user

router = APIRouter(tags=["admin-profile"])


def _me(db: Session, current_user: dict) -> Admin:
    admin_id = current_user.get("id")
    if not admin_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증이 필요합니다")
    a = db.query(Admin).filter(Admin.id == admin_id, Admin.deleted_at.is_(None)).first()
    if not a:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="관리자를 찾을 수 없습니다")
    return a


@router.get("/profile")
async def get_admin_profile(db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """내 프로필 (실제 역할 — 예전에는 모두 "ADMIN" 으로 내려왔다)"""
    a = _me(db, current_user)
    return _row(a, _last_logins(db, [a.id]).get(a.id))


@router.put("/profile")
async def update_admin_profile(profile_data: dict, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """이름·전화번호만 바꾼다. 역할·상태·이메일은 최고 관리자가 관리자 화면에서 바꾼다"""
    a = _me(db, current_user)
    before = {"name": a.name, "phone_number": a.phone_number}
    if "name" in profile_data:
        name = str(profile_data.get("name") or "").strip()
        if not name:
            raise _err(422, "name", "이름을 입력해주세요")
        a.name = name
    if "phone_number" in profile_data:
        a.phone_number = str(profile_data.get("phone_number") or "").strip() or None
    after = {"name": a.name, "phone_number": a.phone_number}
    a.updated_at = get_kst_now()
    db.commit()
    db.refresh(a)
    changed = [k for k in after if after[k] != before[k]]
    if changed:
        audit.record("UPDATE", "ADMIN", a.id, before={k: before[k] for k in changed}, after={k: after[k] for k in changed})
    return _row(a)


@router.put("/password")
async def change_admin_password(password_data: dict, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """내 비밀번호 변경. 오류는 칸별로 돌려준다"""
    current = str(password_data.get("current_password") or "")
    new = str(password_data.get("new_password") or "")
    confirm = str(password_data.get("confirm_password") or "")
    if not current:
        raise _err(422, "current_password", "현재 비밀번호를 입력해주세요")
    _check_password(new, "new_password")
    if new != confirm:
        raise _err(422, "confirm_password", "새 비밀번호와 확인이 일치하지 않습니다")
    a = _me(db, current_user)
    if not verify_password(current, a.password):
        raise _err(400, "current_password", "현재 비밀번호가 올바르지 않습니다")
    if current == new:
        raise _err(422, "new_password", "지금 비밀번호와 다른 비밀번호를 입력해주세요")
    a.password = hash_password(new)
    a.updated_at = get_kst_now()
    db.commit()
    audit.record("UPDATE", "ADMIN_PASSWORD", a.id, after={"password": "(본인 변경)"})
    return {"message": "비밀번호가 성공적으로 변경되었습니다", "success": True}
