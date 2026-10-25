"""
백오피스 관리자(Admin) CRUD API
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
import logging

from database import get_db
from models import Admin, UserStatus, Provider, AdminRole
from schemas import AdminResponse, AdminCreate, AdminUpdate, AdminPasswordUpdate, PaginatedResponse
from utils.datetime_utils import get_kst_now
from .deps import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-admins"])


@router.get("/admins", response_model=PaginatedResponse)
async def get_admin_list(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    search: str | None = Query(None),
    status_filter: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자 목록 조회"""
    try:
        query = db.query(Admin).filter(Admin.deleted_at.is_(None))
        if search:
            sp = f"%{search}%"
            query = query.filter((Admin.email.ilike(sp)) | (Admin.name.ilike(sp)))
        if status_filter:
            try:
                query = query.filter(Admin.status == UserStatus[status_filter])
            except KeyError:
                pass
        total = query.count()
        admins = query.order_by(Admin.created_at.desc()).offset((page - 1) * limit).limit(limit).all()
        data = [
            AdminResponse(id=a.id, email=a.email, name=a.name, profile_image=a.profile_image, phone_number=a.phone_number,
                          provider=a.provider.value if a.provider else None, status=a.status.value if a.status else None,
                          role=a.role.value if a.role else "SUPER_ADMIN",
                          created_at=a.created_at, updated_at=a.updated_at)
            for a in admins
        ]
        return {"data": data, "total": total, "page": page, "limit": limit, "total_pages": (total + limit - 1) // limit if total > 0 else 0}
    except Exception as e:
        logger.error(f"관리자 목록 조회 중 오류: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="관리자 목록 조회 중 오류가 발생했습니다")


@router.get("/admins/{admin_id}", response_model=AdminResponse)
async def get_admin_detail(admin_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """관리자 상세 조회"""
    admin = db.query(Admin).filter(Admin.id == admin_id, Admin.deleted_at.is_(None)).first()
    if not admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="관리자를 찾을 수 없습니다")
    return AdminResponse(id=admin.id, email=admin.email, name=admin.name, profile_image=admin.profile_image, phone_number=admin.phone_number,
                        provider=admin.provider.value if admin.provider else None, status=admin.status.value if admin.status else None,
                        role=admin.role.value if admin.role else "SUPER_ADMIN",
                        created_at=admin.created_at, updated_at=admin.updated_at)


@router.post("/admins", response_model=AdminResponse)
async def create_admin(data: AdminCreate, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """새 관리자 생성"""
    import hashlib
    existing = db.query(Admin).filter(Admin.email == data.email, Admin.deleted_at.is_(None)).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 존재하는 이메일입니다")
    role_val = AdminRole.SUPER_ADMIN
    if data.role:
        try:
            role_val = AdminRole[data.role]
        except KeyError:
            pass
    admin = Admin(email=data.email, password=hashlib.sha256(data.password.encode()).hexdigest(), name=data.name,
                  phone_number=data.phone_number, profile_image=data.profile_image, provider=Provider.LOCAL, status=UserStatus.ACTIVE, role=role_val)
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return AdminResponse(id=admin.id, email=admin.email, name=admin.name, profile_image=admin.profile_image, phone_number=admin.phone_number,
                        provider=admin.provider.value if admin.provider else None, status=admin.status.value if admin.status else None,
                        role=admin.role.value if admin.role else "SUPER_ADMIN",
                        created_at=admin.created_at, updated_at=admin.updated_at)


@router.put("/admins/{admin_id}", response_model=AdminResponse)
async def update_admin(admin_id: int, data: AdminUpdate, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """관리자 정보 수정"""
    admin = db.query(Admin).filter(Admin.id == admin_id, Admin.deleted_at.is_(None)).first()
    if not admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="관리자를 찾을 수 없습니다")
    if data.name is not None:
        admin.name = data.name
    if data.phone_number is not None:
        admin.phone_number = data.phone_number
    if data.profile_image is not None:
        admin.profile_image = data.profile_image
    if data.status is not None:
        try:
            admin.status = UserStatus[data.status]
        except KeyError:
            pass
    if data.role is not None:
        try:
            admin.role = AdminRole[data.role]
        except KeyError:
            pass
    admin.updated_at = get_kst_now()
    db.commit()
    db.refresh(admin)
    return AdminResponse(id=admin.id, email=admin.email, name=admin.name, profile_image=admin.profile_image, phone_number=admin.phone_number,
                        provider=admin.provider.value if admin.provider else None, status=admin.status.value if admin.status else None,
                        role=admin.role.value if admin.role else "SUPER_ADMIN",
                        created_at=admin.created_at, updated_at=admin.updated_at)


@router.put("/admins/{admin_id}/password")
async def update_admin_password_by_admin(admin_id: int, data: AdminPasswordUpdate, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """다른 관리자의 비밀번호 변경"""
    import hashlib
    admin = db.query(Admin).filter(Admin.id == admin_id, Admin.deleted_at.is_(None)).first()
    if not admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="관리자를 찾을 수 없습니다")
    admin.password = hashlib.sha256(data.new_password.encode()).hexdigest()
    admin.updated_at = get_kst_now()
    db.commit()
    return {"message": "비밀번호가 변경되었습니다", "success": True}


@router.delete("/admins/{admin_id}")
async def delete_admin(admin_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """관리자 삭제 (soft delete)"""
    if admin_id == current_user.get("id"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="자기 자신은 삭제할 수 없습니다")
    admin = db.query(Admin).filter(Admin.id == admin_id, Admin.deleted_at.is_(None)).first()
    if not admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="관리자를 찾을 수 없습니다")
    admin.deleted_at = get_kst_now()
    admin.status = UserStatus.DELETED
    db.commit()
    return {"message": "관리자가 삭제되었습니다", "success": True}
