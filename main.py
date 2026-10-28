# -*- coding: utf-8 -*-
import io
import sys
import locale
from typing import cast
from sqlalchemy import text

sys.stdout = cast(io.TextIOWrapper, sys.stdout)
sys.stderr = cast(io.TextIOWrapper, sys.stderr)
# Set default encoding to UTF-8
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

# Set Korean locale
try:
    locale.setlocale(locale.LC_ALL, "ko_KR.UTF-8")
except:
    try:
        locale.setlocale(locale.LC_ALL, "Korean_Korea.UTF-8")
    except:
        pass

from fastapi import FastAPI, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import (
    get_swagger_ui_html,
    get_redoc_html,
)

from sqlalchemy.orm import Session
import uvicorn
import logging

from config import settings
from database import get_db, init_database, test_connection
from routers import (auth, plans, payments, payment_methods, region, subscriptions, users, terms, notices, inquiries,
                     upload, admin, oauth, faq, moderation)
from routers.internal import loadtest_router
from routers.clubs import (
    base_router as clubs_router,
    notices_router as clubs_notices_router,
    regulations_router as club_regulations_router,
    members_router as clubs_members_router,
    fees_router as clubs_fees_router,
    stats_router as clubs_stats_router,
)
from routers.meetings import (
    base_router,
    participants_router,
    workflow_router,
    settlement_router,
    teams_router,
    scores_router,
    meeting_score_router,
    round_score_router,
    expenses_router,
    rounds_expenses_router,
    rounds_router,
    socials_router,
)
from utils.activity import ActivityContextMiddleware
from utils.audit import AuditMiddleware
from utils.csrf_protection import CSRFProtectionMiddleware

# from utils.rate_limiter import RateLimiterMiddleware  # 개발 환경에서는 비활성화

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger(__name__)

# 운영에서는 자동 문서를 닫는다. 259개 엔드포인트의 스키마가 통째로 공개된다.
# 집 서버에서는 nginx 가 /api/ 만 넘겨 줘서 가려져 있었지만, Cloud Run 에는
# 그 앞단이 없어 그대로 노출된다.
_IS_PRODUCTION = settings.ENVIRONMENT == "production"

app = FastAPI(
    title="TeeupLink API",
    description="골프 모임 관리 플랫폼 API",
    version="1.0.0",
    docs_url=None if _IS_PRODUCTION else "/api/docs",
    redoc_url=None if _IS_PRODUCTION else "/api/redoc",
    openapi_url=None if _IS_PRODUCTION else "/api/openapi.json",
    default_response_class=JSONResponse,
    openapi_tags=[
        {
            "name": "auth",
            "description": "인증 관련 API"
        },
        {
            "name": "users",
            "description": "사용자 관리 API"
        },
        {
            "name": "clubs",
            "description": "골프 클럽 관리 API"
        },
        {
            "name": "meetings",
            "description": "모임 관리 API"
        },
        {
            "name": "scores",
            "description": "점수 관리 API"
        },
        {
            "name": "payments",
            "description": "결제 관리 API"
        },
    ],
)
from starlette.middleware.base import BaseHTTPMiddleware


class RawRequestLogMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next):
        body = await request.body()

        request_dict = {
            "method": request.method,
            "url": str(request.url),
            "client": request.client.host if request.client else None,
            "headers": dict(request.headers),
            "body": body.decode("utf-8", errors="ignore"),
        }

        print("==== INCOMING REQUEST ====")
        print(request_dict)
        print("==========================")

        async def receive():
            return {"type": "http.request", "body": body}

        request._receive = receive  # 🔥 핵심

        return await call_next(request)


class HttpsLocationMiddleware(BaseHTTPMiddleware):
    """리다이렉트 Location 을 https 로 올린다.

    FastAPI 는 경로 끝 슬래시가 빠지면 307 로 정식 경로를 알려 준다.
    그 Location 은 요청 스킴을 그대로 따르는데, 프록시 뒤에서 스킴을
    http 로 보면 Location 도 http:// 가 된다. iOS 는 ATS 로 http
    리다이렉트를 차단하므로 앱에서 "Network request failed" 가 난다.
    (/clubs, /notices, /teams 가 실제로 그랬다)

    forwarded_allow_ips 설정만으로도 보통 해결되지만, 프록시가
    X-Forwarded-Proto 를 안 보내면 그것도 소용없다. 그래서 응답 쪽에서
    한 번 더 막는다. 같은 호스트로 가는 http:// 만 바꾸고 외부 주소는
    건드리지 않는다.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        location = response.headers.get("location")
        if location and location.startswith("http://"):
            host = request.headers.get("host")
            if host and location.startswith(f"http://{host}"):
                response.headers["location"] = "https://" + location[len("http://"):]
        return response


app.add_middleware(RawRequestLogMiddleware)

# 관리자 감사 로그 (backend #49). CSRF·CORS 안쪽에서 돌아 그 단계에서 막힌 요청은 남기지 않는다
app.add_middleware(AuditMiddleware)
app.add_middleware(ActivityContextMiddleware)

# CORS 설정 - 가장 먼저 추가해야 함 (미들웨어는 역순으로 실행됨)
# 개발 환경을 위한 localhost 기본값 (React Native: 8081, Admin: 3003)
default_dev_origins = ["http://localhost:8081", "http://localhost:3003", "http://127.0.0.1:8081", "http://127.0.0.1:3003"]
configured_origins = settings.cors_origins_list or []
# 개발 환경에서는 localhost 항상 포함, 프로덕션은 CORS_ORIGINS만 사용
if settings.ENVIRONMENT.lower() == "development":
    allowed_origins = list(dict.fromkeys(default_dev_origins + configured_origins))
else:
    allowed_origins = configured_origins if configured_origins else default_dev_origins
logger.info(f"CORS origins: {allowed_origins}")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# CSRF 보호 미들웨어 추가 (CORS 이후)
app.add_middleware(CSRFProtectionMiddleware)

# 가장 나중에 등록한 미들웨어가 가장 바깥에서 돈다. 라우터가 만든 307 응답을
# 다른 미들웨어가 손댄 뒤 마지막으로 확인하도록 여기에 둔다.
if settings.force_https_location:
    app.add_middleware(HttpsLocationMiddleware)

# # 한국어 로케일 및 인코딩 설정
# @app.middleware("http")
# async def add_korean_locale_header(request: Request, call_next):
#     response = await call_next(request)
#     response.headers["Content-Type"] = "application/json; charset=utf-8"
#     return response

# API 라우터 등록
app.include_router(auth.router, prefix="/api/v1")
app.include_router(oauth.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")

# 클럽 관련 라우터 - 기능별로 통합됨
app.include_router(clubs_router, prefix="/api/v1")  # /clubs - 기본 CRUD
app.include_router(clubs_notices_router, prefix="/api/v1")  # /clubs - 공지사항
app.include_router(club_regulations_router, prefix="/api/v1")  # /clubs - 규정
app.include_router(clubs_members_router, prefix="/api/v1")  # /clubs - 멤버 관리
app.include_router(clubs_fees_router, prefix="/api/v1")  # /clubs - 회비 관리
app.include_router(clubs_stats_router, prefix="/api/v1")  # /clubs - 통계

# 모임 관련 라우터 - 기능별로 통합됨
app.include_router(base_router, prefix="/api/v1")  # /meetings - 기본 CRUD
app.include_router(participants_router, prefix="/api/v1")  # /meetings - 참가자 관리
app.include_router(workflow_router, prefix="/api/v1")  # /meetings - 워크플로우
app.include_router(settlement_router, prefix="/api/v1")  # /meetings - 정산
app.include_router(teams_router, prefix="/api/v1")  # /teams - 팀 관리
app.include_router(scores_router, prefix="/api/v1")  # /scores - 점수 관리
app.include_router(meeting_score_router, prefix="/api/v1")  # /meetings - 모임 스코어 관리
app.include_router(round_score_router, prefix="/api/v1")  # /rounds - 라운딩 스코어 관리
app.include_router(expenses_router, prefix="/api/v1")  # /meetings - 비용 관리
# 앱(1.0, 1.0.1)은 /rounds/{id}/expenses 를 호출한다. 같은 핸들러를 그 경로에도 붙인다.
# rounds_router 보다 먼저 등록해 경로가 가려지지 않게 한다.
app.include_router(rounds_expenses_router, prefix="/api/v1")  # /rounds - 비용 관리 (앱 호환)
app.include_router(rounds_router, prefix="/api/v1")  # /rounds - 라운딩 전용
app.include_router(socials_router, prefix="/api/v1")  # /socials - 소셜 모임 전용
app.include_router(plans.router, prefix="/api/v1")
app.include_router(payments.router, prefix="/api/v1/payments")
app.include_router(payment_methods.router, prefix="/api/v1")
app.include_router(subscriptions.router, prefix="/api/v1")
app.include_router(terms.router, prefix="/api/v1")
app.include_router(notices.router, prefix="/api/v1")
app.include_router(inquiries.router, prefix="/api/v1")
app.include_router(moderation.router, prefix="/api/v1")  # /moderation - 신고·차단
app.include_router(upload.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(faq.client_router, prefix="/api/v1")
app.include_router(loadtest_router, prefix="/api/v1")

# 외부 데이터 관련 API 라우터
app.include_router(region.region_router, prefix="/api/v1")

# 정적 파일 서빙.
# 라우터를 모두 등록한 뒤에 단다. /api/ 로 시작하는 요청은 미들웨어가
# 손대지 않고 그대로 앱으로 넘긴다.
# STATIC_CLIENT_DIR / STATIC_ADMIN_DIR 이 비어 있으면 아무것도 하지 않으므로
# 집 서버 배포(게이트웨이 nginx 가 정적을 담당)에는 영향이 없다.
from static_serving import mount_static  # noqa: E402

mount_static(app, settings)

# 보안 미들웨어 설정
# Rate Limiting (개발 환경에서는 비활성화)
# app.add_middleware(RateLimiterMiddleware, requests_per_minute=120, requests_per_hour=2000)

# CSRF Protection (API 제외) - 임시 비활성화
# app.add_middleware(CSRFProtectionMiddleware, secret_key=settings.jwt_secret_key)


@app.get("/")
async def root():
    return {"message": "TeeupLink API Server"}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/health/db")
async def health_check_db(db: Session = Depends(get_db)):
    """Database health check"""
    try:
        # Simple query to test database connection
        db.execute(statement=text("SELECT 1"))
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return {"status": "unhealthy", "database": "disconnected", "error": str(e)}


@app.on_event("startup")
async def startup_event():
    """Initialize database on startup"""
    logger.info("Starting TeeupLink API Server...")

    # Initialize database
    if init_database():
        logger.info("Database initialization successful")
    else:
        logger.error("Database initialization failed")

    # Test database connection
    if test_connection():
        logger.info("Database connection test successful")
    else:
        logger.error("Database connection test failed")


# 루트 경로의 문서도 같은 이유로 운영에서는 달지 않는다. 위의 docs_url=None
# 만으로는 막히지 않는다. 이 세 개는 별도 라우트라 운영 분기를 우회한다.
if not _IS_PRODUCTION:

    @app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
    def swagger_ui():
        return get_swagger_ui_html(openapi_url="/openapi.json", title="FastAPI - Swagger UI")

    @app.get("/redoc", include_in_schema=False, response_class=HTMLResponse)
    def redoc_ui():
        return get_redoc_html(openapi_url="/openapi.json", title="FastAPI - ReDoc")

    @app.get("/openapi.json", include_in_schema=False)
    def openapi():
        return app.openapi()


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.backend_port,
        # 프록시가 넘긴 X-Forwarded-Proto 를 믿어야 리다이렉트가 https 로 나간다.
        proxy_headers=True,
        forwarded_allow_ips=settings.FORWARDED_ALLOW_IPS,
    )
