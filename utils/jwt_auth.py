"""
JWT 토큰 인증 유틸리티
"""
import jwt
from datetime import datetime, timedelta, timezone
from utils.datetime_utils import get_kst_now
from typing import Any
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from config import settings
import logging
from database import get_db
from models import RefreshTokenBlacklist

# HTTP Bearer 보안 스키마
security = HTTPBearer(auto_error=False)

logger = logging.getLogger(__name__)

ACCOUNT_TYPES = frozenset({"user", "admin"})


def resolve_account_type(payload: dict[str, Any]) -> str:
    """JWT payload에서 계정 종류(user/admin) 추출. type 클레임은 access/refresh 용도."""
    account_type = payload.get("account_type")
    if account_type in ACCOUNT_TYPES:
        return account_type
    legacy_type = payload.get("type")
    if legacy_type in ACCOUNT_TYPES:
        return legacy_type
    return "user"


def _prepare_token_payload(data: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """
    입력 data의 type(user/admin)을 account_type으로 분리.
    password_reset·admin_temp 등 특수 type은 그대로 반환.
    """
    to_encode = data.copy()
    raw_type = to_encode.pop("type", None)
    if raw_type in ACCOUNT_TYPES:
        to_encode["account_type"] = raw_type
        return to_encode, None
    return to_encode, raw_type


class JWTAuth:
    """JWT 토큰 인증 클래스"""
    
    def __init__(self):
        self.secret_key = settings.JWT_SECRET_KEY
        self.algorithm = settings.JWT_ALGORITHM
        self.expire_minutes = settings.JWT_EXPIRE_MINUTES
    
    def create_access_token(self, data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
        """액세스 토큰 생성"""
        try:
            to_encode, special_type = _prepare_token_payload(data)
            
            if expires_delta:
                expire = get_kst_now() + expires_delta
            else:
                expire = get_kst_now() + timedelta(minutes=self.expire_minutes)
            
            # 특수 토큰(password_reset 등)은 type 유지, 일반 로그인은 access
            if special_type is not None:
                to_encode["type"] = special_type
            elif "type" not in to_encode:
                to_encode["type"] = "access"
            
            to_encode.update({
                "exp": expire,
                "iat": get_kst_now()
            })
            
            encoded_jwt = jwt.encode(to_encode, self.secret_key, algorithm=self.algorithm)
            logger.info(f"액세스 토큰 생성 완료 - user_id: {data.get('id')}")
            return encoded_jwt
            
        except Exception as e:
            logger.error(f"액세스 토큰 생성 실패: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="토큰 생성에 실패했습니다"
            )
    
    def create_refresh_token(self, data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
        """리프레시 토큰 생성"""
        try:
            import secrets
            import string
            
            to_encode, _special_type = _prepare_token_payload(data)
            refresh_delta = expires_delta or timedelta(days=settings.JWT_WEB_REFRESH_EXPIRE_DAYS)
            expire = get_kst_now() + refresh_delta
            
            # JTI (JWT ID) 생성 - 토큰 무효화를 위한 고유 식별자
            jti = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
            
            to_encode.update({
                "exp": expire,
                "iat": get_kst_now(),
                "type": "refresh",
                "jti": jti  # JWT ID 추가
            })
            
            encoded_jwt = jwt.encode(to_encode, self.secret_key, algorithm=self.algorithm)
            logger.info(f"리프레시 토큰 생성 완료 - user_id: {data.get('id')}, jti: {jti}")
            return encoded_jwt
            
        except Exception as e:
            logger.error(f"리프레시 토큰 생성 실패: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="리프레시 토큰 생성에 실패했습니다"
            )
    
    def verify_token(self, token: str, token_type: str = "access", db: Session | None = None) -> dict[str, Any]:
        """토큰 검증"""
        try:
            # 토큰 형식 검증 (JWT는 3개 부분으로 나뉘어야 함)
            if not token or token == 'undefined' or token == 'null' or len(token.split('.')) != 3:
                logger.warning(f"잘못된 토큰 형식: {token[:20] if token else 'None'}...")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="토큰이 없거나 형식이 올바르지 않습니다"
                )
            
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])
            
            # 토큰 타입 확인 (admin_temp는 access로 변환되므로 access도 허용)
            token_payload_type = payload.get("type")
            if token_payload_type != token_type:
                # admin_temp 타입은 access로 변환되므로, access 타입 요청 시 admin_temp도 허용하지 않음
                # (이미 verify-admin-token에서 access 토큰으로 변환됨)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"잘못된 토큰 타입입니다. {token_type} 토큰이 필요합니다"
                )
            
            # 만료 시간 확인
            exp = payload.get("exp")
            if exp:
                # JWT의 exp는 UTC timestamp이므로 UTC timezone으로 변환
                exp_datetime = datetime.fromtimestamp(exp, tz=timezone.utc)
                # get_kst_now()를 UTC로 변환하여 비교
                current_utc = get_kst_now().astimezone(timezone.utc)
                if current_utc > exp_datetime:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="토큰이 만료되었습니다"
                    )
            
            # 리프레시 토큰의 경우 블랙리스트 확인
            if token_type == "refresh" and db:
                jti = payload.get("jti")
                if jti:
                    blacklist_entry = db.query(RefreshTokenBlacklist).filter(
                        RefreshTokenBlacklist.token_jti == jti
                    ).first()
                    
                    if blacklist_entry:
                        logger.warning(f"블랙리스트된 토큰으로 접근 시도: {jti}")
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="무효화된 토큰입니다"
                        )
            
            logger.info(f"토큰 검증 성공 - user_id: {payload.get('id')}")
            return payload
            
        except HTTPException:
            raise
        except jwt.ExpiredSignatureError:
            logger.warning("만료된 토큰으로 접근 시도")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="토큰이 만료되었습니다"
            )
        except jwt.InvalidTokenError as e:
            logger.warning(f"잘못된 토큰으로 접근 시도: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="유효하지 않은 토큰입니다"
            )
        except Exception as e:
            logger.error(f"토큰 검증 중 오류: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="토큰 검증에 실패했습니다"
            )
    
    def decode_token(self, token: str) -> dict[str, Any]:
        """토큰 디코딩 (검증 없이)"""
        try:
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm], options={"verify_exp": False})
            return payload
        except jwt.InvalidTokenError as e:
            logger.error(f"토큰 디코딩 실패: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="유효하지 않은 토큰입니다"
            )


# 전역 JWT 인증 인스턴스
jwt_auth = JWTAuth()
