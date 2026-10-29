"""관리자 감사 로그 (backend #49) — 미들웨어 기록, 변경 전후 스냅샷, 일괄 건별 행, 가입신청 처리 이력,
조회 API, CSV 내보내기, 월간 집계·점검, 수정·삭제 차단.

SQLite 메모리 DB 로 실제 미들웨어·쿼리까지 돈다. 인증은 역할별 가짜 관리자로 바꿔 끼운다.
"""

import os
from datetime import datetime, timedelta

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
from models import (  # noqa: E402
    AdminAuditLog,
    Club,
    ClubMembership,
    ClubRole,
    ClubStatus,
    MembershipStatus,
    User,
    UserStatus,
)
from models.audit import AuditLogImmutableError  # noqa: E402
from models.region import Sido  # noqa: E402
from routers import admin as admin_pkg  # noqa: E402
from routers.admin.deps import get_admin_user  # noqa: E402
from utils.audit import AuditMiddleware, sanitize  # noqa: E402
from utils.datetime_utils import get_kst_now  # noqa: E402

A = "/api/v1/admin"


@pytest.fixture
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Sido(code="11", name="서울"))
    db.commit()

    who = {"role": "SUPER_ADMIN"}
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.include_router(admin_pkg.router, prefix="/api/v1")

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


def logs(db, **kw):
    db.expire_all()
    q = db.query(AdminAuditLog)
    for k, v in kw.items():
        q = q.filter(getattr(AdminAuditLog, k) == v)
    return q.order_by(AdminAuditLog.id).all()


def seed_pending(db, n=2, club_status=ClubStatus.ACTIVE):
    c = Club(name="강남 골프회", sido_code="11", status=club_status, member_count=1)
    db.add(c)
    db.flush()
    ids = []
    for i in range(n):
        u = User(email=f"u{i}@gmail.com", nickname=f"golfer{i}", realname=f"김민수{i}", status=UserStatus.ACTIVE)
        db.add(u)
        db.flush()
        m = ClubMembership(club_id=c.id, user_id=u.id, role=ClubRole.MEMBER, status=MembershipStatus.PENDING)
        db.add(m)
        db.flush()
        ids.append(m.id)
    db.commit()
    return c, ids


# ---------------------------------------------------------------- 기록


def test_bulk_reject_writes_one_row_per_item_with_snapshot(env):
    db, as_role = env
    c, (m1, m2) = seed_pending(db)
    r = as_role("CLUB_ADMIN").post(f"{A}/join-requests/bulk", json={"ids": [m1, m2, 999], "action": "reject", "reason": "정원 초과"})
    assert r.status_code == 200
    rows = logs(db)
    assert [(x.action, x.target_id, x.result) for x in rows] == [
        ("REJECT", str(m1), "SUCCESS"),
        ("REJECT", str(m2), "SUCCESS"),
        ("REJECT", "999", "FAIL"),
    ]
    assert len({x.request_id for x in rows}) == 1
    first = rows[0]
    assert first.actor_email == "owner@teeup.run" and first.actor_role == "CLUB_ADMIN"
    assert first.before_json["club_name"] == "강남 골프회"
    assert first.before_json["user_nickname"] == "golfer0"
    assert first.reason == "정원 초과"
    assert first.method == "POST" and first.path == f"{A}/join-requests/bulk"
    assert rows[2].meta_json["message"] == "가입 신청을 찾을 수 없습니다"


def test_history_shows_rejected_even_though_row_is_deleted(env):
    db, as_role = env
    c, (m1, m2) = seed_pending(db)
    c2 = as_role("CLUB_ADMIN")
    c2.post(f"{A}/join-requests/bulk", json={"ids": [m1], "action": "approve"})
    c2.post(f"{A}/join-requests/bulk", json={"ids": [m2], "action": "reject", "reason": "중복"})
    assert db.query(ClubMembership).filter(ClubMembership.id == m2).first() is None

    body = c2.get(f"{A}/join-requests/history").json()
    assert body["total"] == 2
    latest = body["items"][0]
    assert latest["action"] == "REJECT" and latest["membership_id"] == m2
    assert latest["club"]["name"] == "강남 골프회"
    assert latest["user"]["nickname"] == "golfer1"
    assert latest["reason"] == "중복"
    assert latest["processed_by"]["email"] == "owner@teeup.run"

    assert c2.get(f"{A}/join-requests/history", params={"action": "APPROVE"}).json()["total"] == 1
    assert c2.get(f"{A}/join-requests/history", params={"club_id": c.id}).json()["total"] == 2
    assert c2.get(f"{A}/join-requests/history", params={"club_id": c.id + 1}).json()["total"] == 0
    assert c2.get(f"{A}/join-requests/history", params={"q": "golfer1"}).json()["total"] == 1


def test_row_level_approve_is_recorded(env):
    db, as_role = env
    c, (m1,) = seed_pending(db, n=1)
    uid = db.query(ClubMembership).get(m1).user_id
    assert as_role("SUPER_ADMIN").put(f"{A}/clubs/{c.id}/members/{uid}/approve").status_code == 200
    (row,) = logs(db)
    assert (row.action, row.target_type, row.target_id) == ("APPROVE", "CLUB_MEMBERSHIP", str(m1))
    assert row.after_json == {"status": "ACTIVE"}


def test_mutation_without_explicit_record_gets_default_row(env):
    db, as_role = env
    u = User(email="x@gmail.com", nickname="x", status=UserStatus.ACTIVE)
    db.add(u)
    db.commit()
    as_role("SUPER_ADMIN").delete(f"{A}/users/{u.id}")
    (row,) = logs(db)
    assert (row.action, row.target_type, row.target_id) == ("DELETE", "USER", str(u.id))
    assert row.before_json["nickname"] == "x"

    # 기록 없는 경로: 기본 행 (POST 문의 답변 → CREATE INQUIRY)
    as_role("SUPER_ADMIN").post(f"{A}/inquiries/12345/responses", json={"content": "x"})
    last = logs(db)[-1]
    assert (last.action, last.target_type, last.target_id, last.result, last.status_code) == ("CREATE", "INQUIRY", "12345", "FAIL", 404)


def test_forbidden_attempt_is_recorded_as_fail(env):
    db, as_role = env
    as_role("SUPPORT_ADMIN").post(f"{A}/users/bulk", json={"ids": [1], "action": "deactivate"})
    (row,) = logs(db)
    assert row.result == "FAIL" and row.status_code == 403 and row.actor_role == "SUPPORT_ADMIN"


def test_get_is_not_recorded_but_pii_view_is(env):
    db, as_role = env
    u = User(email="pii@gmail.com", nickname="p", status=UserStatus.ACTIVE)
    db.add(u)
    db.commit()
    c = as_role("SUPPORT_ADMIN")
    c.get(f"{A}/users")
    assert logs(db) == []
    assert c.get(f"{A}/users/{u.id}").status_code == 200
    (row,) = logs(db)
    assert (row.action, row.target_type, row.target_id, row.method) == ("VIEW_PII", "USER", str(u.id), "GET")


def test_sanitize_removes_sensitive_keys_recursively():
    out = sanitize({"email": "a", "password": "p", "nested": {"refresh_token": "t", "ok": 1}, "list": [{"billing_key": "k"}]})
    assert out == {"email": "a", "password": "[REDACTED]", "nested": {"refresh_token": "[REDACTED]", "ok": 1}, "list": [{"billing_key": "[REDACTED]"}]}


def test_audit_rows_cannot_be_updated_or_deleted(env):
    db, as_role = env
    as_role("SUPER_ADMIN").post(f"{A}/users/bulk", json={"ids": [1], "action": "deactivate"})
    (row,) = logs(db)
    row.reason = "조작"
    with pytest.raises(AuditLogImmutableError):
        db.commit()
    db.rollback()
    db.delete(row)
    with pytest.raises(AuditLogImmutableError):
        db.commit()
    db.rollback()


# ---------------------------------------------------------------- 조회 API


def _add(db, **kw):
    base = dict(request_id="r", actor_admin_id=1, actor_email="owner@teeup.run", actor_role="SUPER_ADMIN", action="UPDATE",
                method="PUT", path=f"{A}/clubs/1", status_code=200, result="SUCCESS", ip="1.2.3.4", created_at=get_kst_now().replace(tzinfo=None))
    base.update(kw)
    db.add(AdminAuditLog(**base))
    db.commit()


def test_audit_logs_list_is_super_only_and_filters(env):
    db, as_role = env
    _add(db, action="VIEW_PII", target_type="USER", target_id="12")
    _add(db, action="UPDATE", target_type="CLUB", target_id="5", request_id="r2")
    _add(db, action="UPDATE", created_at=datetime(2020, 1, 1))  # 기본 기간(7일) 밖

    assert as_role("SUPPORT_ADMIN").get(f"{A}/audit-logs").status_code == 403
    c = as_role("SUPER_ADMIN")
    body = c.get(f"{A}/audit-logs").json()
    assert body["total"] == 2
    assert body["items"][0]["actor"]["email"] == "owner@teeup.run"
    assert c.get(f"{A}/audit-logs", params={"action": "VIEW_PII"}).json()["total"] == 1
    assert c.get(f"{A}/audit-logs", params=[("action", "VIEW_PII"), ("action", "UPDATE")]).json()["total"] == 2
    assert c.get(f"{A}/audit-logs", params={"target_type": "USER", "target_id": "12"}).json()["total"] == 1
    assert c.get(f"{A}/audit-logs", params={"date_from": "2019-12-01", "date_to": "2020-01-31"}).json()["total"] == 1
    assert c.get(f"{A}/audit-logs", params={"date_from": "2018-01-01", "date_to": "2020-01-31"}).status_code == 422


def test_audit_log_detail_includes_related_rows(env):
    db, as_role = env
    _add(db, request_id="same", target_id="1", before_json={"a": 1})
    _add(db, request_id="same", target_id="2")
    first = logs(db)[0]
    body = as_role("SUPER_ADMIN").get(f"{A}/audit-logs/{first.id}").json()
    assert body["before"] == {"a": 1}
    assert [r["target_id"] for r in body["related"]] == ["2"]
    assert as_role("SUPER_ADMIN").get(f"{A}/audit-logs/99999").status_code == 404


def test_export_requires_reason_and_is_itself_recorded(env):
    db, as_role = env
    _add(db, action="VIEW_PII")
    c = as_role("SUPER_ADMIN")
    assert c.get(f"{A}/audit-logs/export").status_code == 422
    r = c.get(f"{A}/audit-logs/export", params={"reason": "월간 점검"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.content.decode("utf-8-sig").strip().splitlines()
    assert lines[0].startswith("id,일시,관리자") and len(lines) == 2
    export = logs(db, action="EXPORT")[0]
    assert export.reason == "월간 점검" and export.meta_json["count"] == 1 and export.method == "GET"


def test_monthly_summary_flags_heavy_pii_viewers_and_review_once(env):
    db, as_role = env
    day = datetime(2026, 9, 10, 10)
    for i in range(101):
        _add(db, action="VIEW_PII", actor_admin_id=2, actor_email="cs@teeup.run", created_at=day + timedelta(seconds=i))
    _add(db, action="LOGIN_FAIL", actor_admin_id=None, actor_email="who@x", created_at=day)
    _add(db, action="UPDATE", created_at=day)
    _add(db, action="UPDATE", created_at=datetime(2026, 10, 1))  # 다음 달

    c = as_role("SUPER_ADMIN")
    body = c.get(f"{A}/audit-logs/monthly", params={"month": "2026-09"}).json()
    assert body["totals"] == {"view_pii": 101, "export": 0, "login_fail": 1, "changes": 1}
    cs = next(a for a in body["by_admin"] if a["admin_id"] == 2)
    assert cs["max_daily_view_pii"] == 101 and cs["flagged"] is True
    assert body["review"] is None

    r = c.post(f"{A}/audit-logs/monthly/2026-09/review", json={"memo": "cs 계정 확인함"})
    assert r.status_code == 200 and r.json()["review"]["memo"] == "cs 계정 확인함"
    assert c.post(f"{A}/audit-logs/monthly/2026-09/review", json={}).status_code == 409
    assert c.post(f"{A}/audit-logs/monthly/{get_kst_now():%Y-%m}/review", json={}).status_code == 400
    assert logs(db, action="REVIEW")[0].target_id == "2026-09"
