import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    FRONTEND_BASE_URL: str = ""
    API_VERSION: str = ""
    # Database Configuration
    DATABASE_URL: str = ""
    DB_POOL_SIZE: int = 40
    DB_MAX_OVERFLOW: int = 80
    DB_POOL_TIMEOUT: int = 60
    DB_POOL_RECYCLE: int = 1800
    DB_POOL_PRE_PING: bool = True

    # JWT Configuration
    JWT_SECRET_KEY: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 120
    JWT_WEB_REFRESH_EXPIRE_DAYS: int = 7
    JWT_APP_REFRESH_EXPIRE_DAYS: int = 365

    # Environment
    ENVIRONMENT: str = "development"

    # 리버스 프록시 뒤에서 돌 때의 설정.
    # uvicorn 의 forwarded_allow_ips 기본값은 "127.0.0.1" 인데, 컨테이너는
    # 도커 게이트웨이 IP 로 요청을 받는다. 그래서 nginx 가 보낸
    # X-Forwarded-Proto: https 가 무시되고, FastAPI 가 만드는 307 리다이렉트
    # Location 이 http:// 로 나갔다. iOS 는 ATS 로 http 리다이렉트를 막아
    # 앱에서 "Network request failed" 가 된다. (/clubs, /notices, /teams)
    # 컨테이너 포트는 127.0.0.1 에만 공개돼 있어 프록시 외에는 닿지 못한다.
    FORWARDED_ALLOW_IPS: str = "*"

    # 프록시가 X-Forwarded-Proto 를 아예 안 보내는 경우까지 막는다.
    # 같은 호스트로 가는 Location 이 http:// 이면 https:// 로 올린다.
    # None 이면 development 가 아닐 때만 켠다.
    FORCE_HTTPS_LOCATION: bool | None = None

    # Server Configuration
    BACKEND_PORT: str = ""  # 백엔드 서버 포트 (환경 변수 필수)
    # Cloud Run 이 주입하는 포트. 플랫폼이 정해 주는 값이라 기본값을 두지
    # 않는다. 로컬·compose 에는 이 변수가 없고 BACKEND_PORT 가 쓰인다.
    PORT: str = ""

    # Database Auto Creation
    AUTO_CREATE_TABLES: bool | None = (
        None  # None이면 개발 환경에서만 자동 생성, True/False로 명시적 제어 가능
    )

    # CORS
    CORS_ORIGINS: str = ""

    # Auth Cookies
    AUTH_ACCESS_COOKIE_NAME: str = "teeup_access_token"
    AUTH_REFRESH_COOKIE_NAME: str = "teeup_refresh_token"
    AUTH_COOKIE_DOMAIN: str = ""
    AUTH_COOKIE_SAMESITE: str = "lax"
    AUTH_COOKIE_SECURE: bool | None = None

    @property
    def cors_origins_list(self) -> list[str]:
        """Convert CORS origins string to list"""
        if not self.CORS_ORIGINS:
            return []
        return [
            origin.strip() for origin in self.CORS_ORIGINS.split(",")
            if origin.strip()
        ]

    # Loadtest Internal OAuth Mock
    ENABLE_LOADTEST_AUTH_MOCK: bool = False
    LOADTEST_AUTH_MOCK_ALLOWED_IPS: str = "127.0.0.1,::1"

    @property
    def auth_cookie_secure(self) -> bool:
        if self.AUTH_COOKIE_SECURE is not None:
            return self.AUTH_COOKIE_SECURE
        return self.ENVIRONMENT.lower() != "development"

    @property
    def loadtest_auth_mock_allowed_ips_list(self) -> list[str]:
        """Convert loadtest oauth mock allowed IPs to list"""
        if not self.LOADTEST_AUTH_MOCK_ALLOWED_IPS:
            return []
        return [
            ip.strip() for ip in self.LOADTEST_AUTH_MOCK_ALLOWED_IPS.split(",")
            if ip.strip()
        ]

    # File Upload
    upload_dir: str = "../../uploads"  # 프로젝트 루트의 uploads 폴더
    max_file_size: int = 10 * 1024 * 1024  # 10MB

    # Toss Payments Configuration
    TOSS_PAYMENTS_CLIENT_KEY: str = ""
    TOSS_PAYMENTS_SECRET_KEY: str = ""
    TOSS_PAYMENTS_BASE_URL: str = "https://api.tosspayments.com/v1"

    # Google OAuth Configuration
    GOOGLE_WEB_CLIENT_ID: str = ""
    GOOGLE_WEB_REDIRECT_URI: str = ""  # OAuth 리다이렉트 URI (환경 변수 필수)

    GOOGLE_ANDROID_CLIENT_ID: str = ""
    GOOGLE_ANDROID_REDIRECT_URI: str = ""  # OAuth 리다이렉트 URI (환경 변수 필수)

    GOOGLE_IOS_CLIENT_ID: str = ""
    GOOGLE_IOS_REDIRECT_URI: str = ""  # OAuth 리다이렉트 URI (환경 변수 필수)
    GOOGLE_IOS_REDIRECT_URI: str = ""  # OAuth 리다이렉트 URI (환경 변수 필수)

    GOOGLE_CLIENT_SECRET: str = ""

    # Sign in with Apple
    APPLE_BUNDLE_ID: str = ""  # 네이티브 iOS 앱 번들 ID (identity token의 aud)
    APPLE_SERVICE_ID: str = ""  # 웹 Sign in with Apple용 Service ID (미사용 시 공란)
    GOOGLE_DRIVE_CLIENT_ID: str = ""
    GOOGLE_DRIVE_REDIRECT_URI: str = (
        ""  # 드라이브 전용 토큰 발급 콜백(프런트) (환경 변수 필수)
    )
    GOOGLE_DRIVE_CLUB_FOLDER_ID: str = (
        ""  # 클럽 등록 파일 업로드 대상 구글 드라이브 폴더 ID
    )
    GOOGLE_DRIVE_NOTICE_FOLDER_ID: str = (
        ""  # 공지사항 첨부파일 업로드 대상 구글 드라이브 폴더 ID
    )
    # CSRF 검증 스위치.
    #
    # 예전에는 ENVIRONMENT != "development" 이면 자동으로 켜졌다. 그런데 집
    # 서버가 development 로 떠 있어서 운영에서 한 번도 켜진 적이 없고, 그
    # 상태에 맞춰 클라이언트가 만들어졌다. RN 앱과 배포된 웹 번들에는
    # X-CSRF-Token 을 보내는 코드가 아예 없다(백오피스에만 있다).
    #
    # 그래서 ENVIRONMENT 를 production 으로 올리는 것만으로 검증이 켜지면,
    # 스토어에 올라간 앱이 쓰기 요청마다 403 을 받는다. 문서 차단·보안 쿠키
    # 같은 다른 운영 설정과 분리해 둔다.
    #
    # 켜기 전에 두 가지가 선행되어야 한다.
    #   1. 클라이언트가 /api/v1/auth/csrf-token 을 받아 헤더로 실어 보낼 것
    #   2. 토큰 저장소를 공유 저장소로 옮길 것. 지금은 프로세스 메모리
    #      dict 라 인스턴스가 둘 이상이면 발급한 곳에서만 통과한다
    CSRF_PROTECTION_ENABLED: bool = False

    # 정적 파일 서빙 (Cloud Run 용).
    # 집 서버에서는 게이트웨이 nginx 가 했고, Cloud Run 은 앞단이 없어서
    # 앱이 직접 한다. 비어 있으면 미들웨어를 달지 않으므로 기존 배포에는
    # 아무 영향이 없다.
    STATIC_CLIENT_DIR: str = ""
    STATIC_ADMIN_DIR: str = ""
    ADMIN_HOSTS: str = "admin.teeup.kr"
    ADMIN_BASIC_USER: str = ""
    ADMIN_BASIC_PASSWORD: str = ""

    # OAuth 토큰 저장 경로(절대경로): 백엔드 폴더의 token.json
    GOOGLE_TOKEN_FILE: str = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "token.json"))

    # Email Configuration (SMTP)
    MAIL_USERNAME: str = ""  # Gmail 주소
    MAIL_PASSWORD: str = ""  # Gmail App Password (OAuth2 대신 사용)
    MAIL_FROM: str = ""  # 발신자 이메일 (기본값: MAIL_USERNAME)
    MAIL_SERVER: str = "smtp.gmail.com"  # SMTP 서버 주소
    MAIL_PORT: int = 587  # SMTP 포트 (587: STARTTLS, 465: SSL/TLS)
    MAIL_STARTTLS: bool = True  # STARTTLS 사용 여부
    MAIL_SSL_TLS: bool = False  # SSL/TLS 사용 여부 (MAIL_PORT가 465일 때 True)
    FRONTEND_BASE_URL: str = ""  # 프론트엔드 기본 URL (환경 변수 필수)

    # 공공 API
    ADMIN_REGION_API_URL: str = ""
    DATA_GO_KR_API_KEY: str = ""

    # Firebase Push
    FIREBASE_SERVICE_ACCOUNT_FILE: str = ""

    @property
    def database_url(self) -> str:
        """SQLAlchemy database URL"""
        return self.DATABASE_URL

    @property
    def frontend_base_url(self) -> str:
        """프론트엔드 기본 URL (환경 변수 필수, 하드코딩된 도메인 제거)"""
        if not self.FRONTEND_BASE_URL:
            raise ValueError("FRONTEND_BASE_URL 환경 변수가 설정되지 않았습니다.")
        return self.FRONTEND_BASE_URL

    @property
    def force_https_location(self) -> bool:
        """리다이렉트 Location 을 https 로 올릴지."""
        if self.FORCE_HTTPS_LOCATION is not None:
            return self.FORCE_HTTPS_LOCATION
        return self.ENVIRONMENT.lower() != "development"

    @property
    def backend_port(self) -> int:
        """백엔드 서버가 들을 포트 (환경 변수 필수).

        PORT 가 BACKEND_PORT 보다 우선한다. Cloud Run 은 PORT 를 주입하고
        컨테이너가 반드시 그 포트를 듣기를 요구한다. 다른 포트를 들으면
        시작 확인에 실패해 배포가 롤백된다. 둘 중 하나만 있으면 그것을 쓴다.
        """
        source = "PORT" if self.PORT else "BACKEND_PORT"
        raw = self.PORT or self.BACKEND_PORT
        if not raw:
            raise ValueError("PORT 또는 BACKEND_PORT 환경 변수가 설정되지 않았습니다.")
        try:
            port = int(raw)
            if port <= 0:
                raise ValueError(f"{source}가 유효하지 않습니다: {raw}")
            return port
        except ValueError as e:
            if "유효하지 않습니다" in str(e):
                raise
            raise ValueError(f"{source}가 유효하지 않습니다: {raw}")

    class Config:
        # 프로젝트 루트의 .env 파일을 절대경로로 지정 (실행 위치 무관)
        env_file = os.path.abspath(
            os.path.join(os.path.dirname(__file__), ".env"))
        case_sensitive = False
        extra = "ignore"  # 추가 필드 허용


# Global settings instance
settings = Settings()
