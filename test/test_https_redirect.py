"""리다이렉트 Location 이 https 로 나가는지 확인한다.

막으려는 버그 (2026-10-24 운영 장애):
    앱이 /api/v1/clubs, /api/v1/notices, /api/v1/teams 를 끝 슬래시 없이
    부른다. FastAPI 는 307 로 정식 경로(/clubs/ 등)를 알려 주는데, 그
    Location 이 http:// 로 나갔다. iOS 는 ATS 로 http 리다이렉트를 막아
    앱에서 "Network request failed" 만 보였다.

    원인은 두 겹이다.
      1) uvicorn 의 forwarded_allow_ips 기본값이 "127.0.0.1" 이라 도커
         게이트웨이로 들어온 X-Forwarded-Proto: https 를 무시했다.
      2) 프록시가 그 헤더를 안 보내는 경우에는 1) 을 고쳐도 소용없다.
    여기서는 2) 를 막는 HttpsLocationMiddleware 를 검증한다.

이 파일은 서버를 띄우지 않고 TestClient 로만 돈다.
"""

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import RedirectResponse

# main 을 임포트하지 않고 미들웨어만 가져온다 (DB 접속을 피한다)
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("BACKEND_PORT", "7777")

HOST = "api.example.com"


def _build_app(with_middleware: bool) -> FastAPI:
    from main import HttpsLocationMiddleware

    app = FastAPI()

    @app.get("/things/")
    def things():
        return {"ok": True}

    @app.get("/go-outside")
    def go_outside():
        # 외부 주소로 보내는 리다이렉트. 손대면 안 된다.
        return RedirectResponse("http://other.example.com/landing", status_code=307)

    if with_middleware:
        app.add_middleware(HttpsLocationMiddleware)
    return app


# base_url 을 http 로 둔다. 운영에서 났던 상황 그대로다: 바깥은 https 인데
# uvicorn 이 프록시 헤더를 무시해 요청 스킴을 http 로 보고 있었다.
@pytest.fixture(scope="module")
def client_with():
    return TestClient(_build_app(True), base_url=f"http://{HOST}")


@pytest.fixture(scope="module")
def client_without():
    return TestClient(_build_app(False), base_url=f"http://{HOST}")


def test_끝슬래시_리다이렉트가_https로_나간다(client_with):
    """앱이 부르는 /clubs, /notices, /teams 가 겪던 상황."""
    res = client_with.get("/things", follow_redirects=False)
    assert res.status_code == 307
    location = res.headers["location"]
    assert location.startswith("https://"), f"http 로 나갔다: {location}"
    assert location == f"https://{HOST}/things/"


def test_미들웨어가_없으면_http로_나간다(client_without):
    """테스트가 헛돌지 않는지 확인한다 (운영에서 실제로 났던 모습)."""
    res = client_without.get("/things", follow_redirects=False)
    assert res.status_code == 307
    assert res.headers["location"].startswith("http://")


def test_외부_주소는_건드리지_않는다(client_with):
    res = client_with.get("/go-outside", follow_redirects=False)
    assert res.headers["location"] == "http://other.example.com/landing"


def test_쿼리스트링이_유지된다(client_with):
    res = client_with.get("/things?page=1&size=5", follow_redirects=False)
    assert res.headers["location"] == f"https://{HOST}/things/?page=1&size=5"


def test_리다이렉트가_아닌_응답은_그대로(client_with):
    res = client_with.get("/things/")
    assert res.status_code == 200
    assert "location" not in res.headers


def test_설정_기본값은_development_에서만_꺼진다():
    from config import Settings

    assert Settings(ENVIRONMENT="production", BACKEND_PORT="7777").force_https_location is True
    assert Settings(ENVIRONMENT="development", BACKEND_PORT="7777").force_https_location is False
    # 명시하면 그 값을 따른다
    assert Settings(
        ENVIRONMENT="production", BACKEND_PORT="7777", FORCE_HTTPS_LOCATION=False
    ).force_https_location is False
