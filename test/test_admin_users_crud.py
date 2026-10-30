"""관리자 회원 등록·수정·삭제 (backoffice F-031) — 필드 에러, 감사 스냅샷, 최근 로그인.

SQLite 메모리 DB. 활동 로그는 별도 세션이라 utils.activity.session_factory 를 같은 DB 로 바꾼다.
"""

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
from models import AdminAuditLog, User, UserActivityLog, UserStatus  # noqa: E402
from routers import admin as admin_pkg  # noqa: E402
from routers.admin.deps import get_admin_user  # noqa: E402
from utils import activity  # noqa: E402
from utils.passwords import verify_password  # noqa: E402
from utils.activity import ActivityContextMiddleware  # noqa: E402
from utils.audit import AuditMiddleware  # noqa: E402
from utils.datetime_utils import get_kst_now  # noqa: E402

A = "/api/v1/admin"
NEW = {"email": "new@teeup.run", "password": "golf123!", "realname": "김골프", "nickname": "newbie", "phone_number": "010-1111-2222",
       "birthdate": "1990-05-01", "gender": "MALE", "average_score_init": 92}


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(activity, "session_factory", Session)
    db = Session()

    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(ActivityContextMiddleware)
    app.include_router(admin_pkg.router, prefix="/api/v1")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_admin_user] = lambda: {"id": 1, "email": "owner@teeup.run", "name": "운영", "role": "SUPER_ADMIN", "status": "ACTIVE"}
    yield db, TestClient(app)
    db.close()


def audits(db, **kw):
    db.expire_all()
    q = db.query(AdminAuditLog)
    for k, v in kw.items():
        q = q.filter(getattr(AdminAuditLog, k) == v)
    return q.order_by(AdminAuditLog.id).all()


def test_create_user_hashes_password_and_audits_without_it(env):
    db, client = env
    r = client.post(f"{A}/users", json={**NEW, "reason": "오프라인 가입"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["nickname"] == "newbie" and body["status"] == "ACTIVE" and body["handicap_init"] == 20.0
    assert body["birthdate"] == "1990-05-01" and body["provider"] == "LOCAL"

    user = db.get(User, body["id"])
    assert user.password.startswith("pbkdf2_sha256$") and verify_password("golf123!", user.password)
    (row,) = audits(db, action="CREATE")
    assert row.target_id == str(user.id) and row.reason == "오프라인 가입"
    assert "password" not in row.after_json and row.after_json["email"] == "new@teeup.run"
    (sign,) = db.query(UserActivityLog).filter_by(user_id=user.id).all()
    assert sign.event_type == "SIGNUP" and sign.meta_json["by_admin"] is True


@pytest.mark.parametrize("patch, field, code", [
    ({"password": "short1!"}, "password", 422),
    ({"password": "nospecial123"}, "password", 422),
    ({"email": "not-an-email"}, "email", 422),
    ({"realname": "김 골프"}, "realname", 422),
    ({"gender": "X"}, "gender", 422),
    ({"birthdate": "1990/05/01"}, "birthdate", 422),
])
def test_create_validation_returns_field_errors(env, patch, field, code):
    _, client = env
    r = client.post(f"{A}/users", json={**NEW, **patch})
    assert r.status_code == code
    assert r.json()["detail"][0]["field"] == field


def test_duplicates_are_409_per_field(env):
    _, client = env
    assert client.post(f"{A}/users", json=NEW).status_code == 201
    for field, value in (("email", NEW["email"]), ("nickname", NEW["nickname"]), ("phone_number", NEW["phone_number"])):
        other = {**NEW, "email": "x@teeup.run", "nickname": "x", "phone_number": "010-9999-0000", field: value}
        r = client.post(f"{A}/users", json=other)
        assert r.status_code == 409 and r.json()["detail"][0]["field"] == field


def test_update_records_only_changed_fields(env):
    db, client = env
    uid = client.post(f"{A}/users", json=NEW).json()["id"]
    r = client.put(f"{A}/users/{uid}", json={"nickname": "golfking", "realname": "김골프", "average_score_init": 100, "reason": "본인 요청"})
    assert r.status_code == 200, r.text
    assert r.json()["nickname"] == "golfking" and r.json()["handicap_init"] == 28.0
    (row,) = audits(db, action="UPDATE")
    assert row.before_json == {"nickname": "newbie", "average_score_init": 92, "handicap_init": 20.0}
    assert row.after_json == {"nickname": "golfking", "average_score_init": 100, "handicap_init": 28.0}
    assert row.reason == "본인 요청"

    # 직접 넣은 핸디캡은 평균스코어로 덮지 않는다
    r = client.put(f"{A}/users/{uid}", json={"average_score_init": 110, "handicap_init": 15})
    assert r.json()["handicap_init"] == 15.0


def test_status_only_update_is_status_change(env):
    db, client = env
    uid = client.post(f"{A}/users", json=NEW).json()["id"]
    r = client.put(f"{A}/users/{uid}", json={"status": "DEACTIVATED", "reason": "민원"})
    assert r.status_code == 200 and r.json()["status"] == "DEACTIVATED" and r.json()["deactivated_at"]
    (row,) = audits(db, action="STATUS_CHANGE")
    assert row.after_json == {"status": "DEACTIVATED"}
    assert client.put(f"{A}/users/{uid}", json={"status": "DELETED"}).status_code == 422


def test_update_duplicate_nickname_and_deleted_user(env):
    db, client = env
    uid = client.post(f"{A}/users", json=NEW).json()["id"]
    client.post(f"{A}/users", json={**NEW, "email": "b@teeup.run", "nickname": "taken", "phone_number": None})
    r = client.put(f"{A}/users/{uid}", json={"nickname": "taken"})
    assert r.status_code == 409 and r.json()["detail"][0]["field"] == "nickname"

    assert client.delete(f"{A}/users/{uid}", params={"reason": "탈퇴 요청"}).status_code == 200
    (row,) = audits(db, action="DELETE")
    assert row.reason == "탈퇴 요청"
    assert client.put(f"{A}/users/{uid}", json={"nickname": "again"}).status_code == 404


def test_detail_and_list_show_last_login(env):
    db, client = env
    uid = client.post(f"{A}/users", json=NEW).json()["id"]
    now = get_kst_now().replace(tzinfo=None)
    db.add(UserActivityLog(user_id=uid, event_type="LOGIN", summary="로그인", meta_json={"device": "ios"}, created_at=now))
    db.commit()

    d = client.get(f"{A}/users/{uid}").json()
    assert d["last_login_device"] == "ios" and d["last_login_at"]
    assert d["average_score_init"] == 92 and d["status"] == UserStatus.ACTIVE.value
    items = client.get(f"{A}/users").json()["data"]
    assert items[0]["last_login_at"]
