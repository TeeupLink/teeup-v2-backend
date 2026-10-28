"""
백오피스 인증 API (로그인, 로그아웃, 토큰, 프로필 기본)
"""
import secrets
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
import logging

from database import get_db
from utils import audit
from utils.jwt_auth import jwt_auth
from .permissions import permissions_for
from .deps import (
    get_admin_user,
    verify_admin_credentials,
    AdminLoginRequest,
    AdminLoginResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-auth"])


@router.post("/login", response_model=AdminLoginResponse)
async def admin_login(login_data: AdminLoginRequest, db: Session = Depends(get_db)):
    """백오피스 로그인 - JWT 기반"""
    logger.info(f"Admin login attempt: {login_data.email}")
    audit.set_actor(email=login_data.email)
    try:
        admin = verify_admin_credentials(login_data.email, login_data.password, db)
        role = getattr(admin, "role", None)
        audit.set_actor({"id": admin.id, "email": admin.email, "role": role.value if hasattr(role, "value") else role})
        token_payload = {"id": admin.id, "email": admin.email, "name": admin.name}
        # 관리자 액세스 토큰: 8시간 유효 (업무 시간 동안 세션 유지)
        access_token = jwt_auth.create_access_token(token_payload, expires_delta=timedelta(hours=8))
        refresh_token = jwt_auth.create_refresh_token(token_payload)
        return AdminLoginResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            user={"id": admin.id, "email": admin.email, "name": admin.name, "type": "admin"},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"백오피스 로그인 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="로그인 처리 중 오류가 발생했습니다")


@router.post("/generate-client-token")
async def generate_client_token(
    current_user: dict = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """클라이언트 페이지 접근을 위한 일회용 임시 토큰 생성"""
    try:
        import string
        token_jti = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
        user_data = {
            "id": current_user["id"],
            "email": current_user["email"],
            "nickname": current_user.get("name"),
            "role": "ADMIN",
            "admin_view": True,
            "type": "admin_temp",
            "jti": token_jti,
        }
        temp_token = jwt_auth.create_access_token(user_data, expires_delta=timedelta(minutes=5))
        return {"temp_token": temp_token, "expires_in": 300}
    except Exception as e:
        logger.error(f"임시 클라이언트 토큰 생성 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="임시 토큰 생성 중 오류가 발생했습니다")


@router.post("/logout")
async def admin_logout(
    current_user: dict = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """백오피스 로그아웃"""
    try:
        logger.info(f"Admin logout: {current_user['email']}")
        return {"message": "로그아웃되었습니다"}
    except Exception as e:
        logger.error(f"어드민 로그아웃 중 오류: {str(e)}")
        return {"message": "로그아웃되었습니다"}


@router.get("/me")
async def get_current_admin(current_user: dict = Depends(get_admin_user)):
    """현재 관리자 정보 조회"""
    try:
        return {
            "id": current_user["id"],
            "email": current_user["email"],
            "name": current_user.get("name", ""),
            "nickname": current_user.get("name", ""),
            "phone_number": current_user.get("phone_number"),
            "role": current_user.get("role"),
            # 프론트 메뉴·라우트 가드가 같은 권한 표를 쓰도록 내려준다
            "permissions": permissions_for(current_user.get("role")),
            "status": current_user.get("status", "ACTIVE"),
            "created_at": current_user.get("created_at"),
            "updated_at": current_user.get("updated_at"),
        }
    except Exception as e:
        logger.error(f"관리자 정보 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="관리자 정보 조회 중 오류가 발생했습니다")


@router.get("/settings")
async def get_admin_settings(current_user: dict = Depends(get_admin_user)):
    """관리자 설정 조회"""
    try:
        return {
            "theme": "light",
            "language": "ko",
            "notifications": {"email": True, "push": True},
            "dashboard": {"refresh_interval": 30, "default_view": "grid"},
        }
    except Exception as e:
        logger.error(f"관리자 설정 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="관리자 설정 조회 중 오류가 발생했습니다")
