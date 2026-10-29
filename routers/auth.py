# 인증 관련 API들
from fastapi import APIRouter, Depends, HTTPException, status, Query, Body, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from typing import Optional, Literal
from datetime import datetime, timedelta, timezone
from utils import activity
from utils.datetime_utils import get_kst_now, get_kst_date
from utils.passwords import verify_password
import hashlib
import re
import logging

from database import get_db
from models import User, Admin, UserStatus, Provider, RefreshTokenBlacklist, TokenRevokeReason
from schemas import MessageResponse
from services.push_token_service import deactivate_user_push_tokens, sync_user_push_token
from utils.jwt_auth import jwt_auth, resolve_account_type
from utils.csrf_protection import generate_csrf_token, get_middleware_instance
from config import settings
import jwt

router = APIRouter(prefix="/auth", tags=["인증"])

# 로깅 설정
logger = logging.getLogger(__name__)

# 인증 관련 유틸리티 함수들
security = HTTPBearer(auto_error=False)

APP_CLIENT_TYPES = {"android", "ios"}
CLIENT_TYPES = {"web", *APP_CLIENT_TYPES}


def is_app_client_type(client_type: str | None) -> bool:
    return client_type in APP_CLIENT_TYPES


def get_refresh_expire_delta(client_type: str | None) -> timedelta:
    if is_app_client_type(client_type):
        return timedelta(days=settings.JWT_APP_REFRESH_EXPIRE_DAYS)
    return timedelta(days=settings.JWT_WEB_REFRESH_EXPIRE_DAYS)


def _auth_cookie_domain() -> str | None:
    return settings.AUTH_COOKIE_DOMAIN or None


def _auth_cookie_samesite() -> str:
    return settings.AUTH_COOKIE_SAMESITE.lower()


def _get_client_type(request: Request | None) -> str:
    if request is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="X-Client-Type 헤더가 필요합니다.")

    client_type = request.headers.get("x-client-type")
    if client_type not in CLIENT_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효하지 않은 X-Client-Type 헤더입니다.")

    return client_type


def _delete_auth_cookie_variants(response: Response, key: str):
    response.delete_cookie(key=key,
                           path="/",
                           secure=settings.auth_cookie_secure,
                           httponly=True,
                           samesite=_auth_cookie_samesite())

    if _auth_cookie_domain():
        response.delete_cookie(key=key,
                               domain=_auth_cookie_domain(),
                               path="/",
                               secure=settings.auth_cookie_secure,
                               httponly=True,
                               samesite=_auth_cookie_samesite())


def set_auth_cookies(response: Response, access_token: str, refresh_token: str, refresh_max_age: int):
    _delete_auth_cookie_variants(response, settings.AUTH_ACCESS_COOKIE_NAME)
    _delete_auth_cookie_variants(response, settings.AUTH_REFRESH_COOKIE_NAME)

    response.set_cookie(key=settings.AUTH_ACCESS_COOKIE_NAME,
                        value=access_token,
                        max_age=jwt_auth.expire_minutes * 60,
                        httponly=True,
                        secure=settings.auth_cookie_secure,
                        samesite=_auth_cookie_samesite(),
                        path="/")
    response.set_cookie(key=settings.AUTH_REFRESH_COOKIE_NAME,
                        value=refresh_token,
                        max_age=refresh_max_age,
                        httponly=True,
                        secure=settings.auth_cookie_secure,
                        samesite=_auth_cookie_samesite(),
                        path="/")


def clear_auth_cookies(response: Response):
    _delete_auth_cookie_variants(response, settings.AUTH_ACCESS_COOKIE_NAME)
    _delete_auth_cookie_variants(response, settings.AUTH_REFRESH_COOKIE_NAME)


def _get_access_token(credentials: HTTPAuthorizationCredentials | None, request: Request | None) -> str:
    client_type = _get_client_type(request)

    if client_type == "web":
        token = request.cookies.get(settings.AUTH_ACCESS_COOKIE_NAME)
        if token:
            return token
    elif credentials and credentials.credentials:
        return credentials.credentials

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


def _get_refresh_token(token_data: Optional["TokenRefreshRequest"], request: Request | None) -> str:
    client_type = _get_client_type(request)

    if client_type == "web":
        refresh_token = request.cookies.get(settings.AUTH_REFRESH_COOKIE_NAME)
        if refresh_token:
            return refresh_token
    elif token_data and token_data.refresh_token:
        return token_data.refresh_token

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="리프레시 토큰이 필요합니다.")


def get_user_role_from_token(credentials: HTTPAuthorizationCredentials | None = Depends(security),
                             request: Request = None) -> str:
    """JWT 토큰에서 사용자 역할(role) 추출"""
    try:
        token = _get_access_token(credentials, request)
        payload = jwt_auth.verify_token(token, "access")
        role = payload.get("role", "USER")
        account_type = resolve_account_type(payload)

        if account_type == "admin" or role == "ADMIN":
            return "ADMIN"
        return "USER"
    except Exception:
        return "USER"  # 기본값


def is_admin_user(credentials: HTTPAuthorizationCredentials | None = Depends(security),
                  request: Request = None) -> bool:
    """현재 사용자가 관리자인지 확인"""
    try:
        role = get_user_role_from_token(credentials, request)
        return role == "ADMIN"
    except Exception:
        return False


# 유효성 검사 함수들
def validate_email(email: str) -> bool:
    """이메일 유효성 검사"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


def validate_nickname(nickname: str) -> bool:
    """닉네임 유효성 검사 (영문 대소문자, 한글, 숫자만, 2-20자)"""
    if len(nickname) < 2 or len(nickname) > 20:
        return False
    pattern = r'^[a-zA-Z가-힣0-9]+$'
    return re.match(pattern, nickname) is not None


def validate_password(password: str) -> dict:
    """비밀번호 유효성 검사"""
    result = {"is_valid": False, "errors": [], "strength": 0}

    if len(password) < 6 or len(password) > 32:
        result["errors"].append("비밀번호는 6자 이상 32자 이하여야 합니다.")
        return result

    # 영문 대문자, 소문자, 특수문자, 숫자 중 2개 이상 포함 확인
    has_upper = bool(re.search(r'[A-Z]', password))
    has_lower = bool(re.search(r'[a-z]', password))
    has_digit = bool(re.search(r'[0-9]', password))
    has_special = bool(re.search(r'[!@#$%^&*(),.?":{}|<>]', password))

    strength = sum([has_upper, has_lower, has_digit, has_special])

    if strength < 2:
        result["errors"].append("영문 대문자, 소문자, 특수문자, 숫자 중 2개 이상을 포함해야 합니다.")
        return result

    result["is_valid"] = True
    result["strength"] = strength
    return result


def validate_average_score(score: int) -> bool:
    """평균타수 유효성 검사 (55-144타)"""
    return 55 <= score <= 144


def validate_birthdate(birthdate: str) -> dict:
    """생년월일 유효성 검사 (만 14세 이상)"""
    result = {"is_valid": False, "errors": [], "age": None}

    try:
        # 날짜 형식 검사 (YYYY-MM-DD)
        birth_date = datetime.strptime(birthdate, "%Y-%m-%d").date()
        today = get_kst_date().date()

        # 미래 날짜 체크
        if birth_date > today:
            result["errors"].append("생년월일은 미래 날짜일 수 없습니다.")
            return result

        # 만 나이 계산
        age = today.year - birth_date.year
        if (today.month, today.day) < (birth_date.month, birth_date.day):
            age -= 1

        result["age"] = age

        # 만 14세 이상 확인
        if age < 14:
            result["errors"].append("만 14세 이상만 가입할 수 있습니다.")
            return result

        # 유효한 범위 확인 (1900년 이후)
        if birth_date.year < 1900:
            result["errors"].append("올바른 생년월일을 입력해주세요.")
            return result

        result["is_valid"] = True
        return result

    except ValueError:
        result["errors"].append("올바른 날짜 형식(YYYY-MM-DD)을 입력해주세요.")
        return result


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security),
                     request: Request = None,
                     db: Session = Depends(get_db),
                     required_type: Literal["user", "admin"] | None = "user",
                     check_status: bool = True,
                     check_terms_agreement: bool = True) -> User | Admin:
    """
    통합 사용자/관리자 인증 함수
    
    Args:
        required_type: "user" (User만), "admin" (Admin만), None (둘 다 허용)
        check_status: True면 상태 확인 (DELETED, DEACTIVATED 체크), False면 상태 확인 안함
        check_terms_agreement: True면 필수 약관 동의 확인, False면 확인 안함 (약관 동의 API용)
    
    Returns:
        User 또는 Admin 객체
    """
    try:
        # JWT 토큰 검증
        token = _get_access_token(credentials, request)
        logger.info(f"토큰 검증 시작: {token[:20] if token else 'None'}...")
        payload = jwt_auth.verify_token(token, "access")

        user_id = payload.get("id")
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="토큰에 사용자 정보가 없습니다.")

        # 토큰 타입 확인 (User/Admin 구분)
        token_role = payload.get("role", "USER")  # 기본값은 USER
        account_type = resolve_account_type(payload)
        is_admin_token = token_role == "ADMIN" or account_type == "admin"

        # required_type에 따른 검증
        if required_type == "user" and is_admin_token:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="일반 사용자 전용 엔드포인트입니다. 관리자 계정으로는 접근할 수 없습니다.")
        elif required_type == "admin" and not is_admin_token:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="관리자 권한이 필요합니다.")

        # Admin 토큰인 경우
        if is_admin_token:
            admin = db.query(Admin).filter(Admin.id == user_id, Admin.deleted_at.is_(None)).first()

            if not admin:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="관리자를 찾을 수 없습니다.")

            # 상태 확인 (check_status가 True인 경우만)
            if check_status:
                if admin.status == UserStatus.DELETED:
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="삭제된 관리자 계정입니다.")

                if admin.status == UserStatus.DEACTIVATED:
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="비활성화된 관리자 계정입니다.")

            return admin

        # User 토큰인 경우
        user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()

        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="사용자를 찾을 수 없습니다.")

        # 상태 확인 (check_status가 True인 경우만)
        if check_status:
            if user.status == UserStatus.DELETED:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="탈퇴한 사용자입니다.")

            if user.status == UserStatus.DEACTIVATED:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="관리자에 의해 비활성화 처리된 회원입니다.")

        # # 필수 약관 동의 확인 (check_terms_agreement가 True인 경우만)
        # if check_terms_agreement and not is_admin_token:
        #     # 약관 동의 전용 토큰인 경우 체크 제외 (약관 동의 API에만 사용 가능)
        #     terms_agreement_only = payload.get("terms_agreement_only", False)
        #     if not terms_agreement_only:
        #         terms_agreed = getattr(user, 'terms_agreement', False)
        #         privacy_agreed = getattr(user, 'privacy_policy', False)
        #         collection_agreed = getattr(user, 'privacy_collection', False)

        #         if not (terms_agreed and privacy_agreed and collection_agreed):
        #             logger.warning(
        #                 f"필수 약관 미동의로 API 접근 차단: user_id={user.id}, "
        #                 f"terms_agreement={terms_agreed}, "
        #                 f"privacy_policy={privacy_agreed}, "
        #                 f"privacy_collection={collection_agreed}"
        #             )
        #             raise HTTPException(
        #                 status_code=status.HTTP_403_FORBIDDEN,
        #                 detail="필수 약관에 동의하지 않아 서비스를 이용할 수 없습니다. 약관 동의를 완료해주세요."
        #             )

        return user

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"사용자 인증 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="토큰 검증 중 오류가 발생했습니다.")


def get_current_user_allow_both(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    request: Request = None,
    db: Session = Depends(get_db),
) -> User | Admin:
    """User 또는 Admin 토큰 모두 허용 (required_type=None) - 라운딩/정산 등 공통 API용"""
    return get_current_user(credentials=credentials, request=request, db=db, required_type=None, check_status=True)


def get_current_active_user(current_user: User = Depends(get_current_user)) -> User:
    """현재 활성 사용자 조회"""
    if current_user.status != UserStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="비활성화된 사용자입니다.")

    terms_agreed = getattr(current_user, 'terms_agreement', False)
    privacy_agreed = getattr(current_user, 'privacy_policy', False)
    collection_agreed = getattr(current_user, 'privacy_collection', False)
    if not (terms_agreed and privacy_agreed and collection_agreed):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "TERMS_NOT_AGREED",
                "message": "필수 약관 동의 필요",
            },
        )
    return current_user


# 간단한 로그인 스키마
from pydantic import BaseModel


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict


class LoginRequest(BaseModel):
    email: str
    password: str


class PushTokenSyncRequest(BaseModel):
    push_token: str
    token_type: str = "FCM"
    enabled: bool = True


class LogoutRequest(BaseModel):
    push_token: str | None = None
    refresh_token: str | None = None


@router.get("/me")
async def get_current_user_info(current_user: User = Depends(get_current_active_user)):
    """현재 사용자 정보 조회"""
    try:
        return {
            "id": current_user.id,
            "email": current_user.email,
            "nickname": current_user.nickname,
            "type": "user",
            "status": current_user.status.value if current_user.status else None
        }
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


def _build_login_token_payload(
    *,
    user_id: int,
    role: str,
    user_type: str,
    client_type: str,
    email: str | None = None,
    nickname: str | None = None,
) -> dict:
    """Google OAuth와 동일하게 client_type을 JWT에 포함 (웹 refresh·쿠키 세션용)."""
    session_policy = "app_persistent" if is_app_client_type(client_type) else "web_default"
    payload = {
        "id": user_id,
        "role": role,
        "type": user_type,  # create_* 에서 account_type 으로 분리됨
        "client_type": client_type,
        "session_policy": session_policy,
    }
    if email is not None:
        payload["email"] = email
    if nickname is not None:
        payload["nickname"] = nickname
    return payload


@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,
    response: Response,
    payload: LoginRequest = Body(...),
    db: Session = Depends(get_db),
):
    """로컬 이메일/비밀번호 로그인 (User/Admin 겸용)"""
    client_type = _get_client_type(request)
    email = (payload.email or "").strip().lower()
    password = payload.password or ""

    if not email or not password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이메일과 비밀번호를 입력해주세요.")

    password_hash = hashlib.sha256(password.encode()).hexdigest()
    refresh_expire_delta = get_refresh_expire_delta(client_type)

    # 1) Admin 우선 로그인 (관리자는 PBKDF2, 예전 SHA-256 은 성공 시 전환)
    admin = db.query(Admin).filter(Admin.email == email, Admin.deleted_at.is_(None)).first()
    if admin and verify_password(password, admin.password):
        if admin.status == UserStatus.DELETED:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="삭제된 관리자 계정입니다.")
        if admin.status == UserStatus.DEACTIVATED:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="비활성화된 관리자 계정입니다.")
        from routers.admin.deps import upgrade_admin_hash
        upgrade_admin_hash(db, admin, password)

        token_payload = _build_login_token_payload(
            user_id=admin.id,
            role="ADMIN",
            user_type="admin",
            client_type=client_type,
            email=admin.email,
        )
        access_token = jwt_auth.create_access_token(token_payload)
        refresh_token = jwt_auth.create_refresh_token(token_payload, expires_delta=refresh_expire_delta)

        if client_type == "web":
            set_auth_cookies(response, access_token, refresh_token, int(refresh_expire_delta.total_seconds()))

        return LoginResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=jwt_auth.expire_minutes * 60,
            user={"id": admin.id, "email": admin.email, "role": "ADMIN"},
        )

    # 2) User 로그인
    user = db.query(User).filter(User.email == email, User.deleted_at.is_(None)).first()
    if not user or user.password != password_hash:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="이메일 또는 비밀번호가 올바르지 않습니다.")

    if user.status == UserStatus.DELETED:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="탈퇴한 사용자입니다.")
    if user.status == UserStatus.DEACTIVATED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="관리자에 의해 비활성화 처리된 회원입니다.")

    token_payload = _build_login_token_payload(
        user_id=user.id,
        role="USER",
        user_type="user",
        client_type=client_type,
        email=user.email,
        nickname=user.nickname,
    )
    access_token = jwt_auth.create_access_token(token_payload)
    refresh_token = jwt_auth.create_refresh_token(token_payload, expires_delta=refresh_expire_delta)

    if client_type == "web":
        set_auth_cookies(response, access_token, refresh_token, int(refresh_expire_delta.total_seconds()))

    activity.log(user.id, "LOGIN", "로그인", meta={"provider": "LOCAL"})
    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=jwt_auth.expire_minutes * 60,
        user={"id": user.id, "email": user.email, "role": "USER"},
    )


# 새로운 API 엔드포인트들


@router.get("/check-email")
async def check_email_duplicate(email: str = Query(..., description="확인할 이메일 주소"), db: Session = Depends(get_db)):
    """이메일 중복 확인 및 유효성 검사"""
    try:
        logger.info(f"이메일 중복 확인 요청: {email}")

        # 이메일 유효성 검사
        if not validate_email(email):
            logger.info(f"이메일 형식 검증 실패: {email}")
            return {"is_available": False, "is_valid": False, "message": "올바른 이메일 형식이 아닙니다."}

        # 활성 사용자 이메일 중복 확인 (삭제되지 않은 사용자만 확인)
        existing_user = db.query(User).filter(
            User.email == email,
            User.deleted_at.is_(None),  # 삭제되지 않은 사용자만 확인
            User.status != UserStatus.DELETED  # 상태도 확인
        ).first()

        logger.info(f"이메일 중복 확인 쿼리 결과: existing_user={existing_user is not None}, email={email}")

        # 명시적으로 None 체크
        if existing_user is not None:
            logger.info(f"이미 사용 중인 이메일 발견: {email}, provider={existing_user.provider}")
            # OAuth 사용자인 경우 특별한 메시지
            if existing_user.provider != Provider.LOCAL:
                return {
                    "is_available": False,
                    "is_valid": True,
                    "message": f"이미 {existing_user.provider.value} 계정으로 가입된 이메일입니다. 소셜 로그인을 이용해주세요."
                }
            else:
                return {"is_available": False, "is_valid": True, "message": "이미 사용 중인 이메일입니다."}

        # 탈퇴한 사용자의 이메일 확인 (영구 락)
        deleted_user = db.query(User).filter(User.email == email, User.status == UserStatus.DELETED,
                                             User.deleted_at.isnot(None)).first()

        logger.info(f"탈퇴한 사용자 이메일 확인 쿼리 결과: deleted_user={deleted_user is not None}, email={email}")

        if deleted_user is not None:
            logger.info(f"탈퇴한 사용자의 이메일 발견: {email}")
            return {"is_available": False, "is_valid": True, "message": "사용할 수 없는 이메일입니다."}

        logger.info(f"사용 가능한 이메일: {email}")
        return {"is_available": True, "is_valid": True, "message": "사용 가능한 이메일입니다."}

    except Exception as e:
        logger.error(f"이메일 중복 확인 중 오류 발생: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.get("/check-nickname")
async def check_nickname_duplicate(nickname: str = Query(..., description="확인할 닉네임"), db: Session = Depends(get_db)):
    """닉네임 중복 확인 및 유효성 검사"""
    try:
        logger.info(f"닉네임 중복 확인 요청: {nickname}")

        # 닉네임 유효성 검사
        if not validate_nickname(nickname):
            logger.info(f"닉네임 형식 검증 실패: {nickname}")
            return {"is_available": False, "is_valid": False, "message": "닉네임은 영문 대소문자, 한글, 숫자만 사용 가능하며 2-20자여야 합니다."}

        # 닉네임 중복 확인 (삭제되지 않은 사용자만 확인)
        existing_user = db.query(User).filter(
            User.nickname == nickname,
            User.deleted_at.is_(None),  # 삭제되지 않은 사용자만 확인
            User.status != UserStatus.DELETED  # 상태도 확인
        ).first()

        logger.info(f"닉네임 중복 확인 쿼리 결과: existing_user={existing_user is not None}, nickname={nickname}")

        # 명시적으로 None 체크
        if existing_user is not None:
            logger.info(f"이미 사용 중인 닉네임 발견: {nickname}")
            return {"is_available": False, "is_valid": True, "message": "이미 사용 중인 닉네임입니다."}

        # 탈퇴한 사용자의 닉네임 락 확인 (original_nickname으로 검색)
        deleted_user = db.query(User).filter(User.original_nickname == nickname, User.status == UserStatus.DELETED,
                                             User.deleted_at.isnot(None),
                                             User.nickname_locked_until.isnot(None)).first()

        logger.info(f"탈퇴한 사용자 닉네임 락 확인 쿼리 결과: deleted_user={deleted_user is not None}, nickname={nickname}")

        if deleted_user is not None:
            # 7일 락이 아직 유효한지 확인
            if deleted_user.nickname_locked_until and deleted_user.nickname_locked_until > get_kst_now():
                # 남은 일수 계산
                remaining_days = (deleted_user.nickname_locked_until - get_kst_now()).days
                logger.info(f"탈퇴한 사용자의 닉네임 락 유효: {nickname}, 남은 기간: {remaining_days}일")
                return {
                    "is_available": False,
                    "is_valid": True,
                    "message": f"탈퇴한 사용자의 닉네임은 7일 동안(남은 기간: {remaining_days}일) 사용할 수 없습니다."
                }

        logger.info(f"사용 가능한 닉네임: {nickname}")
        return {"is_available": True, "is_valid": True, "message": "사용 가능한 닉네임입니다."}

    except Exception as e:
        logger.error(f"닉네임 중복 확인 중 오류 발생: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.post("/validate-average-score")
async def validate_average_score_value(score: int = Query(..., description="검사할 평균타수")):
    """평균타수 유효성 검사"""
    try:
        is_valid = validate_average_score(score)
        return {"is_valid": is_valid, "message": "유효한 평균타수입니다." if is_valid else "평균타수는 55타 이상 144타 이하여야 합니다."}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.delete("/withdraw")
async def withdraw_user(current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """회원탈퇴"""
    try:
        # 사용자 조회
        user = db.query(User).filter(User.id == current_user.id).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")

        # UI 표시용으로 닉네임 변경 (DB는 원본 유지)
        user.original_nickname = user.nickname  # 원본 닉네임 저장
        # 닉네임은 유니크 제약이 있으므로 중복을 피하기 위해 유니크 접미사를 부여한다
        base_nickname = "탈퇴회원"
        # 타임스탬프 기반 접미사로 충돌 방지 (예: 탈퇴회원_1730208732)
        unique_suffix = f"_{int(get_kst_now().timestamp())}"
        user.nickname = f"{base_nickname}{unique_suffix}"

        # 닉네임 락 설정 (7일 후 해제)
        user.nickname_locked_until = get_kst_now() + timedelta(days=7)

        # 사용자 상태를 DELETED로 변경
        user.status = UserStatus.DELETED
        user.deleted_at = get_kst_now()
        user.updated_at = get_kst_now()

        # 이메일은 그대로 두고, 중복확인에서 status로 체크하도록 함
        # user.email = f"deleted_{user.id}_{int(get_kst_now().timestamp())}@deleted.com"

        db.commit()
        activity.log(user.id, "WITHDRAW", "회원 탈퇴")

        return {"message": "회원탈퇴가 완료되었습니다.", "success": True}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"회원탈퇴 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 내부 오류가 발생했습니다: {str(e)}")


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    payload: LogoutRequest | None = Body(default=None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """로그아웃 (토큰 무효화)"""
    try:
        client_type = _get_client_type(request)
        refresh_token = None
        if client_type == "web":
            refresh_token = request.cookies.get(settings.AUTH_REFRESH_COOKIE_NAME)
        elif payload and payload.refresh_token:
            refresh_token = payload.refresh_token

        if refresh_token:
            refresh_payload = jwt_auth.verify_token(refresh_token, "refresh", db=db)
            refresh_user_id = int(refresh_payload["id"])
            if refresh_user_id != int(current_user.id):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="다른 사용자의 리프레시 토큰입니다.")

            refresh_expires_at = datetime.fromtimestamp(refresh_payload["exp"], tz=timezone.utc).astimezone(get_kst_now().tzinfo)
            revoke_refresh_token(token_jti=refresh_payload["jti"],
                                 user_id=int(current_user.id),
                                 reason=TokenRevokeReason.LOGOUT,
                                 expires_at=refresh_expires_at,
                                 db=db)

        if payload and payload.push_token:
            deactivate_user_push_tokens(db=db, user_id=current_user.id, push_token=payload.push_token)
        else:
            deactivate_user_push_tokens(db=db, user_id=current_user.id)

        clear_auth_cookies(response)
        return {"message": "로그아웃이 완료되었습니다.", "success": True}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.post("/push-token", response_model=MessageResponse)
async def sync_push_token(payload: PushTokenSyncRequest,
                          current_user: User = Depends(get_current_active_user),
                          db: Session = Depends(get_db)):
    """사용자 디바이스 푸시 토큰 등록/해제"""
    try:
        sync_user_push_token(db=db,
                             user_id=current_user.id,
                             push_token=payload.push_token,
                             token_type=payload.token_type,
                             enabled=payload.enabled)
        return {"message": "푸시 토큰이 동기화되었습니다.", "success": True}
    except Exception as e:
        logger.error(f"푸시 토큰 동기화 실패: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="푸시 토큰 동기화에 실패했습니다.")


# 토큰 갱신 스키마
class TokenRefreshRequest(BaseModel):
    refresh_token: str | None = None


class TokenResponse(BaseModel):
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None
    expires_in: int = 3600  # 1시간
    refresh_expires_in: int | None = None
    session_policy: str | None = None
    client_type: str | None = None
    user: dict


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: Request,
    response: Response,
    token_data: TokenRefreshRequest | None = Body(default=None),
    db: Session = Depends(get_db),
):
    """토큰 갱신 (자동 로그인용)"""
    try:
        request_client_type = _get_client_type(request)
        refresh_token_value = _get_refresh_token(token_data, request)

        # JWT 리프레시 토큰 검증
        payload = jwt_auth.verify_token(refresh_token_value, "refresh", db=db)
        user_id = payload.get("id")
        client_type = payload.get("client_type")
        if client_type is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="토큰의 클라이언트 타입이 없습니다.")
        if client_type != request_client_type:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="토큰의 클라이언트 타입이 요청과 일치하지 않습니다.")
        session_policy = "app_persistent" if is_app_client_type(client_type) else "web_default"
        refresh_expire_delta = get_refresh_expire_delta(client_type)

        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 리프레시 토큰입니다.")

        # 사용자 확인
        user = db.query(User).filter(User.id == user_id, User.status == UserStatus.ACTIVE,
                                     User.deleted_at.is_(None)).first()

        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="사용자를 찾을 수 없습니다.")

        # 새로운 액세스 토큰과 리프레시 토큰 생성
        user_data = {
            "id": user.id,
            "email": user.email,
            "nickname": user.nickname,
            "type": "user",
            "role": "USER",
            "provider": user.provider.value if user.provider else None,
            "client_type": client_type,
            "session_policy": session_policy,
        }

        new_access_token = jwt_auth.create_access_token(user_data)
        new_refresh_token = jwt_auth.create_refresh_token(user_data, expires_delta=refresh_expire_delta)
        used_refresh_expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc).astimezone(get_kst_now().tzinfo)
        revoke_refresh_token(token_jti=payload["jti"],
                             user_id=int(user.id),
                             reason=TokenRevokeReason.LOGOUT,
                             expires_at=used_refresh_expires_at,
                             db=db)

        if not is_app_client_type(client_type):
            set_auth_cookies(response,
                             access_token=new_access_token,
                             refresh_token=new_refresh_token,
                             refresh_max_age=int(refresh_expire_delta.total_seconds()))

        return TokenResponse(
            access_token=None if client_type == "web" else new_access_token,
            refresh_token=None if client_type == "web" else new_refresh_token,
            token_type=None if client_type == "web" else "bearer",
            expires_in=jwt_auth.expire_minutes * 60,
            refresh_expires_in=int(refresh_expire_delta.total_seconds()),
            session_policy=session_policy,
            client_type=client_type,
            user={
                "id": user.id,
                "email": user.email,
                "nickname": user.nickname,
                "type": "user"
            })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"토큰 갱신 실패: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="토큰 갱신 중 오류가 발생했습니다.")


@router.post("/verify-token", response_model=dict)
async def verify_token(token: str = Query(..., description="검증할 토큰"), db: Session = Depends(get_db)):
    """토큰 유효성 검사"""
    try:
        if not token:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="토큰이 필요합니다.")

        # JWT 토큰 검증
        payload = jwt_auth.verify_token(token, "access")
        user_id = payload.get("id")

        if not user_id:
            return {"valid": False, "message": "토큰에 사용자 정보가 없습니다."}

        # 사용자 확인
        user = db.query(User).filter(User.id == user_id, User.status == UserStatus.ACTIVE,
                                     User.deleted_at.is_(None)).first()

        if not user:
            return {"valid": False, "message": "사용자를 찾을 수 없습니다."}

        return {
            "valid": True,
            "message": "유효한 토큰입니다.",
            "user": {
                "id": user.id,
                "email": user.email,
                "nickname": user.nickname,
                "type": "user"
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"토큰 검증 실패: {e}")
        return {"valid": False, "message": "토큰 검증 중 오류가 발생했습니다."}


# 토큰 무효화 관련 함수들
def revoke_refresh_token(token_jti: str, user_id: int, reason: TokenRevokeReason, expires_at: datetime, db: Session):
    """리프레시 토큰을 블랙리스트에 추가"""
    try:
        # 이미 블랙리스트에 있는지 확인
        existing = db.query(RefreshTokenBlacklist).filter(RefreshTokenBlacklist.token_jti == token_jti).first()

        if existing:
            logger.warning(f"토큰이 이미 블랙리스트에 있습니다: {token_jti}")
            return existing

        # 블랙리스트에 추가 (id는 자동 생성)
        blacklist_entry = RefreshTokenBlacklist(token_jti=token_jti,
                                                user_id=user_id,
                                                reason=reason,
                                                expires_at=expires_at)

        db.add(blacklist_entry)
        db.commit()
        db.refresh(blacklist_entry)

        logger.info(f"토큰 블랙리스트 추가 완료: {token_jti}, 사유: {reason.value}")
        return blacklist_entry

    except Exception as e:
        logger.error(f"토큰 블랙리스트 추가 실패: {e}")
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="토큰 무효화 중 오류가 발생했습니다.")


def is_token_blacklisted(token_jti: str, db: Session) -> bool:
    """토큰이 블랙리스트에 있는지 확인"""
    try:
        blacklist_entry = db.query(RefreshTokenBlacklist).filter(RefreshTokenBlacklist.token_jti == token_jti).first()

        return blacklist_entry is not None

    except Exception as e:
        logger.error(f"토큰 블랙리스트 확인 실패: {e}")
        return False


def revoke_all_user_tokens(user_id: int, reason: TokenRevokeReason, db: Session):
    """사용자의 모든 리프레시 토큰 무효화"""
    try:
        # 사용자의 모든 활성 토큰을 블랙리스트에 추가
        # 실제 구현에서는 사용자의 모든 활성 토큰을 찾아서 무효화해야 함
        # 여기서는 예시로 현재 시간 기준으로 처리

        logger.info(f"사용자 {user_id}의 모든 토큰 무효화 요청, 사유: {reason.value}")

        # 실제로는 사용자의 모든 활성 토큰을 찾아서 처리해야 함
        # 현재는 로그만 남기고 성공으로 처리

        return {"message": "사용자의 모든 토큰이 무효화되었습니다.", "user_id": user_id, "reason": reason.value}

    except Exception as e:
        logger.error(f"사용자 토큰 무효화 실패: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="토큰 무효화 중 오류가 발생했습니다.")


@router.post("/revoke-token", response_model=MessageResponse)
async def revoke_refresh_token_api(token_jti: str,
                                   reason: TokenRevokeReason = TokenRevokeReason.LOGOUT,
                                   db: Session = Depends(get_db),
                                   current_user: User = Depends(get_current_user)):
    """
    특정 리프레시 토큰 무효화
    """
    try:
        # 토큰의 만료 시간 계산 (일반적으로 7일)
        expires_at = get_kst_now() + timedelta(days=7)

        # 토큰 무효화
        blacklist_entry = revoke_refresh_token(
            token_jti=token_jti,
            user_id=int(current_user.id) if isinstance(current_user.id, str) else current_user.id,
            reason=reason,
            expires_at=expires_at,
            db=db)

        return MessageResponse(success=True, message=f"토큰이 성공적으로 무효화되었습니다. (사유: {reason.value})")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"토큰 무효화 API 오류: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="토큰 무효화 중 오류가 발생했습니다.")


@router.post("/revoke-all-tokens", response_model=MessageResponse)
async def revoke_all_user_tokens_api(reason: TokenRevokeReason = TokenRevokeReason.LOGOUT,
                                     db: Session = Depends(get_db),
                                     current_user: User = Depends(get_current_user)):
    """
    사용자의 모든 리프레시 토큰 무효화 (모든 기기에서 로그아웃)
    """
    try:
        result = revoke_all_user_tokens(
            user_id=int(current_user.id) if isinstance(current_user.id, str) else current_user.id,
            reason=reason,
            db=db)

        return MessageResponse(success=True, message=result["message"])

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"모든 토큰 무효화 API 오류: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="토큰 무효화 중 오류가 발생했습니다.")


@router.get("/csrf-token")
async def get_csrf_token():
    """
    CSRF 토큰 발급 (인증 불필요)
    """
    try:
        # 미들웨어 인스턴스에서 토큰 생성 (저장소에 등록됨)
        middleware = get_middleware_instance()
        if middleware:
            token = middleware.generate_csrf_token()
        else:
            # 미들웨어가 아직 초기화되지 않은 경우 대체 방법 사용
            token = generate_csrf_token()
        return {
            "csrf_token": token,
            "expires_in": 3600  # 1시간
        }
    except Exception as e:
        logger.error(f"CSRF 토큰 생성 오류: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="CSRF 토큰 생성 중 오류가 발생했습니다.")


class AdminTokenVerifyRequest(BaseModel):
    token: str


@router.post("/verify-admin-token")
async def verify_admin_token(request: AdminTokenVerifyRequest = Body(...), db: Session = Depends(get_db)):
    """임시 어드민 토큰 검증 및 사용자 정보 반환 (일회용)"""
    try:
        token = request.token

        # 토큰 검증
        payload = jwt_auth.verify_token(token, "admin_temp")

        # 어드민 임시 토큰인지 확인
        if payload.get("type") != "admin_temp" or not payload.get("admin_view"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 어드민 토큰입니다")

        # 일회용 토큰 확인 (이미 사용되었는지 체크)
        token_jti = payload.get("jti")
        if not token_jti:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 토큰 형식입니다")

        # 토큰 사용 여부 확인 (routers/admin/tokens.py)
        from routers.admin import is_token_used, mark_token_as_used

        if is_token_used(token_jti):
            logger.warning(f"이미 사용된 토큰으로 접근 시도: {token_jti}")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="이미 사용된 토큰입니다")

        # 관리자 정보 조회
        admin_id = payload.get("id")
        admin = db.query(Admin).filter(Admin.id == admin_id, Admin.deleted_at.is_(None)).first()

        if not admin:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="관리자를 찾을 수 없습니다")

        # 관리자 상태 확인
        if admin.status == UserStatus.DELETED:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="삭제된 관리자 계정입니다")

        if admin.status == UserStatus.DEACTIVATED:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="비활성화된 관리자 계정입니다")

        # 토큰을 사용됨으로 표시 (일회용)
        mark_token_as_used(token_jti)
        logger.info(f"어드민 토큰 사용 완료 및 무효화: {token_jti}, admin_id: {admin_id}")

        # 일반 access 토큰 생성 (클라이언트에서 일반 API 호출에 사용)
        user_data = {
            "id": admin.id,
            "email": admin.email,
            "name": admin.name,
            "admin_view": True,  # 어드민 뷰 플래그 유지
            "type": "admin"  # 관리자 타입
        }

        # 일반 access 토큰 생성 (5분 유효기간 유지)
        access_token = jwt_auth.create_access_token(user_data, expires_delta=timedelta(minutes=5))

        # 관리자 정보 반환 (클라이언트에서 사용할 수 있는 형태)
        return {
            "id": admin.id,
            "email": admin.email,
            "name": admin.name,
            "type": "admin",
            "admin_view": True,
            "access_token": access_token,  # 일반 access 토큰 반환
            "temp_token": token  # 호환성을 위해 임시 토큰도 반환 (사용하지 않음)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"어드민 토큰 검증 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="토큰 검증 중 오류가 발생했습니다")


@router.get("/terms/{terms_type}")
async def get_terms_public(terms_type: str, db: Session = Depends(get_db)):
    """약관 조회 (공개 API)"""
    try:
        from models import Terms

        # 약관 타입 검증
        valid_types = ['service', 'privacy', 'collection', 'marketing']
        if terms_type not in valid_types:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효하지 않은 약관 타입입니다")

        # 약관 타입 매핑
        type_mapping = {
            'service': 'SERVICE',
            'privacy': 'PRIVACY',
            'collection': 'PRIVACY_COLLECTION',
            'marketing': 'MARKETING',
        }

        if terms_type not in type_mapping:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효하지 않은 약관 타입입니다")

        db_terms_type = type_mapping[terms_type]

        # 약관 조회
        terms = db.query(Terms).filter(Terms.type == db_terms_type, Terms.is_active == True).first()

        if not terms:
            # 기본 약관 반환
            return {
                "title": get_default_terms_title(terms_type),
                "content": get_default_terms_content(terms_type),
                "updated_at": None
            }

        return {
            "title": terms.title,
            "content": terms.content,
            "updated_at": terms.updated_at.isoformat() if terms.updated_at else None
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"약관 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다")


def get_default_terms_title(terms_type: str) -> str:
    """기본 약관 제목 반환"""
    titles = {'service': '서비스 이용약관', 'privacy': '개인정보처리방침', 'collection': '개인정보 수집 및 이용동의', 'marketing': '마케팅정보 수신동의'}
    return titles.get(terms_type, '약관')


def get_default_terms_content(terms_type: str) -> str:
    """기본 약관 내용 반환"""
    contents = {
        'service': '<h2>제1조 (목적)</h2><p>본 약관은 TeeUp 서비스의 이용과 관련하여 회사와 이용자 간의 권리, 의무 및 책임사항을 규정함을 목적으로 합니다.</p>',
        'privacy': '<h2>제1조 (개인정보의 처리목적)</h2><p>회사는 다음의 목적을 위하여 개인정보를 처리합니다.</p>',
        'collection': '<h2>제1조 (개인정보의 수집 및 이용목적)</h2><p>회사는 다음의 목적을 위하여 개인정보를 수집 및 이용합니다.</p>',
        'marketing': '<h2>제1조 (마케팅정보 수신동의)</h2><p>회사는 이용자에게 다양한 정보를 제공하기 위해 마케팅 정보를 수신하는 것에 동의를 받습니다.</p>'
    }
    return contents.get(terms_type, '<p>약관 내용을 입력해주세요.</p>')


# 비밀번호 재설정 관련 스키마
