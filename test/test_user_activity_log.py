"""회원 활동 로그 (backend #50) — 앱 이벤트 기록, 기록 실패 격리, 관리자 계정 제외, 타임라인 커서, 1년 정리.

SQLite 메모리 DB. 활동 로그는 별도 세션으로 남으므로 utils.activity.session_factory 를 같은 DB 로 바꿔 끼운다.
"""

import hashlib
import os
from datetime import timedelta

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
from models import Admin, Club, ClubMembership, ClubRole, ClubStatus, MembershipStatus, User, UserActivityLog, UserStatus  # noqa: E402
from models.region import Sido  # noqa: E402
from routers import admin as admin_pkg  # noqa: E402
from routers import auth as auth_router  # noqa: E402
from routers.admin.activities import mask_ip  # noqa: E402
from routers.admin.deps import get_admin_user  # noqa: E402
from utils import activity  # noqa: E402
from utils.activity import ActivityContextMiddleware  # noqa: E402
from utils.audit import AuditMiddleware  # noqa: E402
from utils.datetime_utils import get_kst_now  # noqa: E402

A = "/api/v1/admin"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(activity, "session_factory", Session)
    db = Session()
    db.add(Sido(code="11", name="서울"))
    db.commit()

    who = {"role": "SUPER_ADMIN"}
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(ActivityContextMiddleware)
    app.include_router(admin_pkg.router, prefix="/api/v1")
    app.include_router(auth_router.router, prefix="/api/v1")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_admin_user] = lambda: {"id": 1, "email": "owner@teeup.run", "name": "운영", "role": who["role"], "status": "ACTIVE"}

    def as_role(role):
        who["role"] = role
        return client

    client = TestClient(app)
    yield db, as_role
    db.close()


def acts(db, **kw):
    db.expire_all()
    q = db.query(UserActivityLog)
    for k, v in kw.items():
        q = q.filter(getattr(UserActivityLog, k) == v)
    return q.order_by(UserActivityLog.id).all()


def make_user(db, email="golfer@gmail.com", password=None):
    u = User(email=email, nickname=email.split("@")[0], status=UserStatus.ACTIVE,
             password=hashlib.sha256(password.encode()).hexdigest() if password else None)
    db.add(u)
    db.commit()
    return u


def test_local_login_records_device_version_and_ip(env, monkeypatch):
    db, as_role = env
    # 토큰 발급은 이 테스트 대상이 아니다 (테스트 환경엔 서명 키가 없다)
    monkeypatch.setattr(auth_router.jwt_auth, "create_access_token", lambda *a, **k: "a")
    monkeypatch.setattr(auth_router.jwt_auth, "create_refresh_token", lambda *a, **k: "r")
    u = make_user(db, password="pw-1234")
    r = as_role("SUPER_ADMIN").post(
        "/api/v1/auth/login",
        json={"email": u.email, "password": "pw-1234"},
        headers={"X-Client-Type": "ios", "X-App-Version": "1.0.2", "X-Forwarded-For": "211.234.10.12, 10.0.0.1"},
    )
    assert r.status_code == 200, r.text
    (row,) = acts(db)
    assert (row.user_id, row.event_type, row.summary) == (u.id, "LOGIN", "로그인")
    assert row.meta_json == {"device": "ios", "app_version": "1.0.2", "provider": "LOCAL"}
    assert row.ip == "211.234.10.12"


def test_failed_login_is_not_recorded(env):
    db, as_role = env
    u = make_user(db, password="pw-1234")
    as_role("SUPER_ADMIN").post("/api/v1/auth/login", json={"email": u.email, "password": "wrong"}, headers={"X-Client-Type": "ios"})
    assert acts(db) == []


def test_admin_approve_records_club_join_for_member(env):
    db, as_role = env
    u = make_user(db)
    c = Club(name="강남 골프회", sido_code="11", status=ClubStatus.ACTIVE, member_count=1)
    db.add(c)
    db.flush()
    m = ClubMembership(club_id=c.id, user_id=u.id, role=ClubRole.MEMBER, status=MembershipStatus.PENDING)
    db.add(m)
    db.commit()
    as_role("CLUB_ADMIN").post(f"{A}/join-requests/bulk", json={"ids": [m.id], "action": "approve"})
    (row,) = acts(db)
    assert (row.user_id, row.event_type, row.summary, row.target_type, row.target_id) == (u.id, "CLUB_JOIN", "강남 골프회 가입", "CLUB", c.id)


def test_admin_actor_and_unknown_type_are_skipped(env):
    db, _ = env
    admin = Admin(email="x@teeup.run", password="x", name="x")
    db.add(admin)
    db.commit()
    activity.log(admin, "LOGIN", "로그인")
    activity.log(1, "NOT_A_TYPE", "?")
    activity.log(None, "LOGIN", "로그인")
    assert acts(db) == []


def test_write_failure_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(activity, "session_factory", boom)
    activity.log(1, "LOGIN", "로그인")  # 예외가 밖으로 나오지 않아야 한다


def _seed(db, user_id, n, start):
    for i in range(n):
        db.add(UserActivityLog(user_id=user_id, event_type="LOGIN" if i % 2 else "CLUB_JOIN", summary=f"#{i}", created_at=start + timedelta(minutes=i), ip="211.234.10.12"))
    db.commit()


def test_timeline_cursor_paging_filters_and_masking(env):
    db, as_role = env
    u = make_user(db)
    other = make_user(db, "other@gmail.com")
    start = get_kst_now().replace(tzinfo=None) - timedelta(days=1)
    _seed(db, u.id, 5, start)
    _seed(db, other.id, 3, start)

    c = as_role("SUPPORT_ADMIN")
    p1 = c.get(f"{A}/users/{u.id}/activities", params={"size": 2}).json()
    assert [i["summary"] for i in p1["items"]] == ["#4", "#3"]
    assert p1["items"][0]["ip_masked"] == "211.***.***.12"
    p2 = c.get(f"{A}/users/{u.id}/activities", params={"size": 2, "cursor": p1["next_cursor"]}).json()
    assert [i["summary"] for i in p2["items"]] == ["#2", "#1"]
    p3 = c.get(f"{A}/users/{u.id}/activities", params={"size": 2, "cursor": p2["next_cursor"]}).json()
    assert [i["summary"] for i in p3["items"]] == ["#0"] and p3["next_cursor"] is None

    only_login = c.get(f"{A}/users/{u.id}/activities", params={"types": "LOGIN"}).json()
    assert {i["event_type"] for i in only_login["items"]} == {"LOGIN"} and len(only_login["items"]) == 2
    assert c.get(f"{A}/users/{u.id}/activities", params={"types": "NOPE"}).status_code == 422
    assert c.get(f"{A}/users/{u.id}/activities", params={"cursor": "garbage"}).status_code == 422
    assert c.get(f"{A}/users/99999/activities").status_code == 404
    assert as_role("CONTENT_ADMIN").get(f"{A}/users/{u.id}/activities").status_code == 403


def test_purge_removes_only_older_than_one_year(env):
    db, _ = env
    u = make_user(db)
    now = get_kst_now().replace(tzinfo=None)
    _seed(db, u.id, 3, now - timedelta(days=400))
    _seed(db, u.id, 2, now - timedelta(days=10))
    assert activity.purge_expired(db, batch=2) == 3
    assert len(acts(db)) == 2


def test_mask_ip():
    assert mask_ip("1.2.3.4") == "1.***.***.4"
    assert mask_ip("2001:db8:85a3::1") == "2001:db8:****"
    assert mask_ip(None) is None
