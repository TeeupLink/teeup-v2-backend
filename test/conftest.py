"""
pytest fixtures for API testing
"""
import os
import tempfile

# ── 테스트 DB 격리 ─────────────────────────────────────────────
# database.py 는 임포트되는 순간 settings.DATABASE_URL 로 전역 엔진을 만든다.
# 테스트 파일마다 "임포트 전에 DATABASE_URL 을 바꾸는" 패턴을 쓰지만, 한 프로세스에서
# 전체를 돌리면 가장 먼저 database 를 임포트한 파일의 값이 끝까지 쓰인다.
# test_admin_* 가 setdefault("sqlite://") 로 먼저 임포트하면 QueuePool + 메모리 sqlite
# 조합이 되어 연결마다 빈 DB 가 열리고, 전역 엔진에 테이블을 만드는 test_expenses /
# test_scores_null_timestamp 가 "no such table" 로 깨졌다.
# 어떤 테스트 파일보다 먼저 읽히는 conftest 에서 파일 기반 임시 sqlite 하나로 고정한다.
# (셸에 운영 DATABASE_URL 이 있어도 테스트가 그 DB 를 건드리지 않도록 덮어쓴다.)
_TEST_DB_DIR = tempfile.mkdtemp(prefix="teeup-pytest-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TEST_DB_DIR, "test.db")

import pytest  # noqa: E402
import requests  # noqa: E402

# API 기본 URL
BASE_URL = "http://localhost:8002/api/v1"

@pytest.fixture
def token():
    """로그인하여 토큰을 반환하는 fixture"""
    response = requests.post(f"{BASE_URL}/auth/login", json={
        "email": "leader@teeup.run",
        "password": "test123"
    })
    
    if response.status_code == 200:
        data = response.json()
        return data['access_token']
    else:
        pytest.skip("로그인 실패 - 테스트 데이터가 없습니다")

@pytest.fixture
def admin_token():
    """관리자 토큰을 반환하는 fixture"""
    response = requests.post(f"{BASE_URL}/auth/login", json={
        "email": "admin@teeup.run",
        "password": "admin123"
    })
    
    if response.status_code == 200:
        data = response.json()
        return data['access_token']
    else:
        pytest.skip("관리자 로그인 실패 - 테스트 데이터가 없습니다")

@pytest.fixture
def club_id(token):
    """테스트용 클럽 ID를 반환하는 fixture"""
    # 기존 클럽이 있는지 확인
    response = requests.get(f"{BASE_URL}/clubs", headers={"Authorization": f"Bearer {token}"})
    
    if response.status_code == 200:
        clubs = response.json()
        if clubs and len(clubs) > 0:
            return clubs[0]['id']
    
    # 클럽이 없으면 생성
    club_data = {
        "name": "테스트 골프클럽",
        "type": "REGULAR",
        "description": "테스트용 골프클럽입니다",
        "member_count": 10,
        "location": "서울시 강남구",
        "contact_info": "010-1234-5678",
        "additional_info": "매주 토요일 정기 라운딩"
    }
    
    response = requests.post(f"{BASE_URL}/clubs", json=club_data, headers={"Authorization": f"Bearer {token}"})
    
    if response.status_code in [200, 201]:
        data = response.json()
        return data['id']
    else:
        pytest.skip("클럽 생성 실패")


# ── 예전 "실서버" 테스트를 in-process 로 돌리는 하네스 ─────────────────────
# test_auth / test_clubs / test_meetings / test_members 는 원래 localhost:8002 에 띄운 서버와
# 시드 계정(admin@teeup.run, leader1@teeup.run …, 비밀번호 test1234)을 전제로 requests 를 썼다.
# 서버가 없으면 전부 ConnectionError 였다.
#
# 이 하네스는 그 모듈의 `requests` 를 바꿔 끼워 같은 호출을 main.app 의 TestClient 로 보내고,
# 테스트마다 새 메모리 sqlite 에 시드 계정을 넣는다. 테스트 본문(호출 URL·기대값)은 그대로 둔다.
#
# 실제 서버를 상대로 돌리고 싶으면 TEEUP_LIVE_API_URL=http://host:port 를 주면 된다.
# 그때는 바꿔 끼우지 않고, 테스트 안의 http://localhost:8002 만 그 주소로 바꿔 보낸다.
LEGACY_SERVER = "http://localhost:8002"
SEED_PASSWORD = "test1234"
SEED_ADMINS = ["admin@teeup.run"]
SEED_USERS = [
    "leader1@teeup.run",
    "manager1@teeup.run",
    "member1@teeup.run",
    "user1@teeup.run",
    "user2@teeup.run",
    "user3@teeup.run",
]


class _RequestsShim:
    """requests 의 get/post/put/patch/delete 만 흉내 내는 얇은 어댑터."""

    exceptions = requests.exceptions

    def __init__(self, send):
        self._send = send

    def _call(self, method, url, **kwargs):
        kwargs.pop("timeout", None)
        # 테스트가 쓰인 뒤 auth 라우터가 X-Client-Type(web/ios/android) 헤더를 필수로 바꿨다
        # (e5549ea). 테스트는 응답 본문으로 토큰을 받으므로 앱 클라이언트(ios)로 보낸다.
        headers = {"X-Client-Type": "ios", **(kwargs.pop("headers", None) or {})}
        return self._send(method, url, headers=headers, **kwargs)

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def post(self, url, **kw):
        return self._call("POST", url, **kw)

    def put(self, url, **kw):
        return self._call("PUT", url, **kw)

    def patch(self, url, **kw):
        return self._call("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self._call("DELETE", url, **kw)


def _seed_accounts(Session):
    import hashlib

    from models import Admin, User
    from models.region import Gungu, Sido
    from utils.passwords import hash_password

    db = Session()
    try:
        # 클럽 생성은 sido_code / gungu_codes 를 검증한다 (서울 / 강남구)
        db.add_all([Sido(code="11", name="서울특별시"), Gungu(code="11680", name="강남구")])
        for email in SEED_ADMINS:
            db.add(Admin(email=email, password=hash_password(SEED_PASSWORD, iterations=1000), name=email.split("@")[0]))
        legacy_hash = hashlib.sha256(SEED_PASSWORD.encode()).hexdigest()
        for email in SEED_USERS:
            nick = email.split("@")[0]
            db.add(User(email=email, password=legacy_hash, nickname=nick, realname=nick, needs_terms_agreement=False,
                        terms_agreement=True, privacy_policy=True, privacy_collection=True))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def legacy_api(request, monkeypatch):
    """예전 실서버 테스트 모듈에서 `pytestmark = pytest.mark.usefixtures("legacy_api")` 로 쓴다."""
    live = os.environ.get("TEEUP_LIVE_API_URL")
    if live:
        live = live.rstrip("/")

        def send_live(method, url, **kw):
            return requests.request(method, url.replace(LEGACY_SERVER, live, 1), **kw)

        monkeypatch.setattr(request.module, "requests", _RequestsShim(send_live))
        yield
        return

    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import database
    import models  # noqa: F401  (모든 모델을 Base.metadata 에 등록)
    from config import settings
    from database import Base, get_db
    from main import app
    from utils import activity

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    _seed_accounts(Session)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(database, "SessionLocal", Session)
    monkeypatch.setattr(activity, "session_factory", Session)
    monkeypatch.setattr(settings, "CSRF_PROTECTION_ENABLED", False, raising=False)
    # 테스트는 .env 를 읽지 않으므로 JWT 비밀키가 비어 있다. 테스트 전용 키를 넣는다.
    from utils.jwt_auth import jwt_auth

    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "pytest-only-secret", raising=False)
    monkeypatch.setattr(jwt_auth, "secret_key", "pytest-only-secret")
    monkeypatch.setitem(app.dependency_overrides, get_db, _db)

    client = TestClient(app, base_url=LEGACY_SERVER)

    def send_inprocess(method, url, **kw):
        # requests.get/post 는 호출마다 쿠키가 비어 있다. TestClient 는 쿠키를 들고 다니므로 맞춰 비운다.
        client.cookies.clear()
        return client.request(method, url, **kw)

    monkeypatch.setattr(request.module, "requests", _RequestsShim(send_inprocess))
    yield client
    client.close()
    engine.dispose()
