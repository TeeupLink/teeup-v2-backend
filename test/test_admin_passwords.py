"""관리자·회원 비밀번호 해시 전환 — 솔트 없는 SHA-256 → PBKDF2-SHA256 (로그인 성공 시 자동 전환).

SQLite 메모리 DB.
"""

import hashlib
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("BACKEND_PORT", "7777")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import models  # noqa: E402,F401
from database import Base, get_db  # noqa: E402
from models import Admin, AdminRole, User, UserStatus  # noqa: E402
from routers import admin as admin_pkg  # noqa: E402
from routers import auth as auth_router  # noqa: E402
from utils import activity  # noqa: E402
from utils.jwt_auth import jwt_auth  # noqa: E402
from utils.passwords import ALGO, hash_password, needs_rehash, verify_password  # noqa: E402

PW = "Teeup!2026"
LEGACY = hashlib.sha256(PW.encode()).hexdigest()


def test_hash_is_salted_and_verifies():
    a, b = hash_password(PW), hash_password(PW)
    assert a != b and a.startswith(f"{ALGO}$")  # 같은 비밀번호도 솔트가 달라 결과가 다르다
    assert verify_password(PW, a) and not verify_password("Teeup!2027", a)
    assert verify_password(PW, LEGACY) and not verify_password("x", LEGACY)
    assert not verify_password(PW, None) and not verify_password(PW, "garbage") and not verify_password(PW, f"{ALGO}$x$y$z")
    assert needs_rehash(LEGACY) and not needs_rehash(a)
    assert needs_rehash(hash_password(PW, iterations=1000))


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(activity, "session_factory", Session)
    monkeypatch.setattr(jwt_auth, "secret_key", "test-secret-only-for-tests-0123456789")
    db = Session()
    db.add(Admin(id=1, email="owner@teeup.run", password=LEGACY, name="대표", role=AdminRole.SUPER_ADMIN, status=UserStatus.ACTIVE))
    db.add(User(id=7, email="golfer@teeup.run", password=LEGACY, nickname="골퍼", realname="김골퍼", status=UserStatus.ACTIVE))
    db.add(User(id=8, email="google@teeup.run", password=None, nickname="구글", realname="이구글", status=UserStatus.ACTIVE))
    db.commit()
    app = FastAPI()
    app.include_router(admin_pkg.router, prefix="/api/v1")
    app.include_router(auth_router.router, prefix="/api/v1")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    yield db, TestClient(app)
    db.close()


def stored(db):
    db.expire_all()
    return db.get(Admin, 1).password


def test_backoffice_login_upgrades_legacy_hash(env):
    db, client = env
    assert client.post("/api/v1/admin/login", json={"email": "owner@teeup.run", "password": "wrong"}).status_code == 401
    assert stored(db) == LEGACY  # 실패하면 바꾸지 않는다

    assert client.post("/api/v1/admin/login", json={"email": "owner@teeup.run", "password": PW}).status_code == 200
    new = stored(db)
    assert new.startswith(f"{ALGO}$") and verify_password(PW, new)
    # 바뀐 뒤에도 같은 비밀번호로 로그인된다
    assert client.post("/api/v1/admin/login", json={"email": "owner@teeup.run", "password": PW}).status_code == 200
    assert stored(db) == new


def test_shared_login_also_accepts_and_upgrades(env):
    db, client = env
    r = client.post("/api/v1/auth/login", json={"email": "owner@teeup.run", "password": PW}, headers={"X-Client-Type": "web"})
    assert r.status_code == 200, r.text
    assert stored(db).startswith(f"{ALGO}$")


def user_pw(db, uid=7):
    db.expire_all()
    return db.get(User, uid).password


def login(client, email, pw):
    return client.post("/api/v1/auth/login", json={"email": email, "password": pw}, headers={"X-Client-Type": "ios"})


def test_member_login_upgrades_legacy_hash(env):
    """회원도 로그인 성공 시 PBKDF2 로 바뀐다. 앱은 원문을 보내므로 앱 수정이 필요 없다"""
    db, client = env
    assert login(client, "golfer@teeup.run", "wrong").status_code == 401
    assert user_pw(db) == LEGACY

    r = login(client, "golfer@teeup.run", PW)
    assert r.status_code == 200, r.text
    new = user_pw(db)
    assert new.startswith(f"{ALGO}$") and verify_password(PW, new)
    assert login(client, "golfer@teeup.run", PW).status_code == 200
    assert user_pw(db) == new


def test_member_without_password_cannot_log_in(env):
    """소셜 가입 회원(비밀번호 없음)은 이메일 로그인이 안 된다"""
    db, client = env
    assert login(client, "google@teeup.run", "").status_code == 400
    assert login(client, "google@teeup.run", PW).status_code == 401
    assert user_pw(db, 8) is None


def test_admin_created_member_gets_pbkdf2(env):
    from services.admin_user_service import AdminUserCreate, create_user

    db, client = env
    u = create_user(db, AdminUserCreate(email="new@teeup.run", password=PW, realname="박신규", nickname="신규"))
    assert u.password.startswith(f"{ALGO}$")
    assert login(client, "new@teeup.run", PW).status_code == 200
