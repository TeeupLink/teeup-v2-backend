"""
Rate Limiting 미들웨어
"""
import time
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
import logging

logger = logging.getLogger(__name__)

class RateLimiterMiddleware(BaseHTTPMiddleware):
    """Rate Limiting 미들웨어"""
    
    def __init__(self, app, requests_per_minute: int = 60, requests_per_hour: int = 1000):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.requests_per_hour = requests_per_hour
        self.requests: dict[str, list] = {}
        self.cleanup_interval = 300  # 5분마다 정리
    
    async def dispatch(self, request: Request, call_next):
        """Rate Limiting 검증"""
        logger.debug(f"Rate Limiter: {request.method} {request.url.path}")
        try:
            # 클라이언트 IP 추출
            client_ip = self._get_client_ip(request)
            current_time = time.time()
            logger.debug(f"Client IP: {client_ip}")
            
            # 요청 기록 정리
            self._cleanup_old_requests(current_time)
            
            # Rate Limiting 검증
            if not self._check_rate_limit(client_ip, current_time, request.url.path):
                logger.warning(f"Rate limit exceeded for IP: {client_ip}, Path: {request.url.path}")
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content={"detail": "요청 한도를 초과했습니다. 잠시 후 다시 시도해주세요."}
                )
            
            # 요청 기록
            self._record_request(client_ip, current_time)
            
            return await call_next(request)
            
        except Exception as e:
            logger.error(f"Rate Limiting 검증 중 오류: {str(e)}")
            return await call_next(request)
    
    def _get_client_ip(self, request: Request) -> str:
        """클라이언트 IP 추출"""
        # X-Forwarded-For 헤더 확인 (프록시 환경)
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        
        # X-Real-IP 헤더 확인
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip
        
        # 직접 연결
        return request.client.host if request.client else "unknown"
    
    def _check_rate_limit(self, client_ip: str, current_time: float, request_path: str = None) -> bool:
        """Rate Limiting 검증"""
        if client_ip not in self.requests:
            return True
        
        # API별 Rate Limit 적용
        if request_path:
            per_minute, per_hour = APIRateLimiter.get_rate_limit(request_path)
        else:
            per_minute, per_hour = self.requests_per_minute, self.requests_per_hour
        
        client_requests = self.requests[client_ip]
        
        # 1분 내 요청 수 확인
        minute_ago = current_time - 60
        recent_requests = [req_time for req_time in client_requests if req_time > minute_ago]
        
        if len(recent_requests) >= per_minute:
            return False
        
        # 1시간 내 요청 수 확인
        hour_ago = current_time - 3600
        hourly_requests = [req_time for req_time in client_requests if req_time > hour_ago]
        
        if len(hourly_requests) >= per_hour:
            return False
        
        return True
    
    def _record_request(self, client_ip: str, current_time: float):
        """요청 기록"""
        if client_ip not in self.requests:
            self.requests[client_ip] = []
        
        self.requests[client_ip].append(current_time)
    
    def _cleanup_old_requests(self, current_time: float):
        """오래된 요청 기록 정리"""
        # 1시간 이상 된 요청 기록 제거
        cutoff_time = current_time - 3600
        
        for client_ip in list(self.requests.keys()):
            self.requests[client_ip] = [
                req_time for req_time in self.requests[client_ip] 
                if req_time > cutoff_time
            ]
            
            # 빈 리스트 제거
            if not self.requests[client_ip]:
                del self.requests[client_ip]

# API별 Rate Limiting 설정
class APIRateLimiter:
    """API별 Rate Limiting 설정"""
    
    # API별 요청 제한 설정 (개발 환경용 완화된 설정)
    RATE_LIMITS = {
        "/api/v1/auth/login": {"per_minute": 30, "per_hour": 200},
        "/api/v1/auth/register": {"per_minute": 20, "per_hour": 100},
        "/api/v1/auth/refresh": {"per_minute": 50, "per_hour": 500},
        "/api/v1/auth/logout": {"per_minute": 30, "per_hour": 200},
        "/api/v1/payments": {"per_minute": 20, "per_hour": 100},
        "/api/v1/upload": {"per_minute": 20, "per_hour": 100},
        "default": {"per_minute": 120, "per_hour": 2000}
    }
    
    @classmethod
    def get_rate_limit(cls, path: str) -> tuple[int, int]:
        """경로별 Rate Limit 반환"""
        for api_path, limits in cls.RATE_LIMITS.items():
            if path.startswith(api_path):
                return limits["per_minute"], limits["per_hour"]
        
        default = cls.RATE_LIMITS["default"]
        return default["per_minute"], default["per_hour"]
