"""
CSRF 방어 미들웨어 - 강화된 버전
"""
import secrets
import hashlib
import hmac
import time
from fastapi import Request, HTTPException, status
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware
import logging

from config import settings

logger = logging.getLogger(__name__)

# 전역 미들웨어 인스턴스 저장소
_middleware_instance = None

def get_middleware_instance():
    """미들웨어 인스턴스 반환"""
    return _middleware_instance

def set_middleware_instance(instance):
    """미들웨어 인스턴스 설정"""
    global _middleware_instance
    _middleware_instance = instance

class CSRFProtectionMiddleware(BaseHTTPMiddleware):
    """강화된 CSRF 방어 미들웨어"""
    
    def __init__(self, app, secret_key: str = None):
        super().__init__(app)
        self.secret_key = secret_key or "csrf_secret_key_change_in_production"
        self.exempt_methods = {"GET", "HEAD", "OPTIONS"}
        self.exempt_paths = {"/health", "/docs", "/redoc", "/openapi.json"}
        self.exempt_api_paths = {
            "/api/v1/auth/login",
            "/api/v1/auth/register", 
            "/api/v1/auth/refresh",
            "/api/v1/auth/push-token",
            "/api/v1/auth/logout",
            "/api/v1/auth/oauth/google/callback",
            "/api/v1/admin/login",
            "/api/v1/auth/csrf-token"  # CSRF 토큰 요청은 제외
        }
        # CSRF 토큰 저장소 (실제로는 Redis나 세션에 저장)
        self.csrf_tokens: dict[str, float] = {}
        self.token_expiry = 3600  # 1시간
        # 전역 인스턴스로 등록
        set_middleware_instance(self)
    
    async def dispatch(self, request: Request, call_next):
        """강화된 CSRF 검증 미들웨어"""
        logger.debug(f"CSRF Middleware: {request.method} {request.url.path}")
        # 명시적으로 켰을 때만 검증한다. 근거는 config.CSRF_PROTECTION_ENABLED
        # 주석에 있다. ENVIRONMENT 에 묶어 두면 운영 승격과 동시에 앱이 깨진다.
        if not settings.CSRF_PROTECTION_ENABLED:
            logger.debug("CSRF 검증 비활성 - 건너뜀")
            return await call_next(request)

        # GET, HEAD, OPTIONS 요청은 CSRF 검증 제외
        if request.method in self.exempt_methods:
            logger.debug(f"CSRF exempt method: {request.method}")
            return await call_next(request)
        
        # 특정 경로는 CSRF 검증 제외
        if request.url.path in self.exempt_paths:
            return await call_next(request)
        
        # 특정 API 경로는 CSRF 검증 제외 (인증 관련)
        if request.url.path in self.exempt_api_paths:
            return await call_next(request)

        try:
            # 만료된 토큰 정리
            self._cleanup_expired_tokens()
            
            # CSRF 토큰 검증
            csrf_token = request.headers.get("X-CSRF-Token")
            if not csrf_token:
                logger.warning(f"CSRF token missing for {request.method} {request.url.path}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="CSRF 토큰이 필요합니다"
                )
            
            # CSRF 토큰 유효성 검증
            if not self._validate_csrf_token(csrf_token, request):
                logger.warning(f"Invalid CSRF token for {request.method} {request.url.path}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="유효하지 않은 CSRF 토큰입니다"
                )
            
            # 토큰 사용 후 삭제 (일회성 토큰)
            self._consume_csrf_token(csrf_token)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"CSRF 검증 중 오류: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="CSRF 검증 중 오류가 발생했습니다"
            ) from e

        return await call_next(request)
    
    def _validate_csrf_token(self, token: str, request: Request) -> bool:
        """강화된 CSRF 토큰 유효성 검증"""
        try:
            # 토큰 형식 검증
            if not token or len(token) < 32:
                return False
            
            # 토큰이 저장소에 존재하는지 확인
            if token not in self.csrf_tokens:
                return False
            
            # 토큰 만료 시간 확인
            token_time = self.csrf_tokens[token]
            if time.time() - token_time > self.token_expiry:
                del self.csrf_tokens[token]
                return False
            
            # Referer 헤더 검증 (개발 환경 고려)
            referer = request.headers.get("Referer")
            if referer:
                host = request.headers.get("Host")
                if host:
                    # localhost나 127.0.0.1는 포트가 달라도 허용 (개발 환경)
                    is_localhost = host.startswith('localhost') or host.startswith('127.0.0.1') or 'localhost' in referer or '127.0.0.1' in referer
                    if not is_localhost and host not in referer:
                        logger.warning(f"Referer mismatch: {referer} vs {host}")
                        return False
            
            # Origin 헤더 검증 (더 안전함, 개발 환경 고려)
            origin = request.headers.get("Origin")
            if origin:
                host = request.headers.get("Host")
                if host:
                    # localhost나 127.0.0.1는 포트가 달라도 허용 (개발 환경)
                    is_localhost = host.startswith('localhost') or host.startswith('127.0.0.1') or 'localhost' in origin or '127.0.0.1' in origin
                    if not is_localhost and f"http://{host}" not in origin and f"https://{host}" not in origin:
                        logger.warning(f"Origin mismatch: {origin} vs {host}")
                        return False
            
            return True
            
        except Exception as e:
            logger.error(f"CSRF 토큰 검증 중 오류: {str(e)}")
            return False
    
    def _cleanup_expired_tokens(self):
        """만료된 토큰 정리"""
        current_time = time.time()
        expired_tokens = [
            token for token, timestamp in self.csrf_tokens.items()
            if current_time - timestamp > self.token_expiry
        ]
        for token in expired_tokens:
            del self.csrf_tokens[token]
    
    def _consume_csrf_token(self, token: str):
        """CSRF 토큰 사용 후 삭제 (일회성)"""
        if token in self.csrf_tokens:
            del self.csrf_tokens[token]
    
    def generate_csrf_token(self) -> str:
        """강화된 CSRF 토큰 생성"""
        # HMAC 기반 토큰 생성
        timestamp = str(int(time.time()))
        random_part = secrets.token_urlsafe(16)
        token_data = f"{timestamp}:{random_part}"
        
        # HMAC 서명 생성
        signature = hmac.new(
            self.secret_key.encode(),
            token_data.encode(),
            hashlib.sha256
        ).hexdigest()
        
        token = f"{token_data}:{signature}"
        
        # 토큰 저장
        self.csrf_tokens[token] = time.time()
        
        return token

# CSRF 토큰 생성 함수
def generate_csrf_token() -> str:
    """CSRF 토큰 생성 (외부 사용용)"""
    return secrets.token_urlsafe(32)
