"""관리자 비밀번호 해시 전환 — 솔트 없는 SHA-256 → PBKDF2-SHA256 (로그인 성공 시 자동 전환).

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
from models import Admin, AdminRole, UserStatus  # noqa: E402
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
