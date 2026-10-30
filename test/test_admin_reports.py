"""백오피스 신고·차단 (backoffice #29). SQLite 메모리 DB."""

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
from models import AdminAuditLog, Club, ClubStatus, ContentReport, Meeting, User, UserBlock, UserStatus  # noqa: E402
from models.enums import ReportReason, ReportStatus, ReportTargetType  # noqa: E402
from models.region import Sido  # noqa: E402
from routers import admin as admin_pkg  # noqa: E402
from routers.admin.deps import get_admin_user  # noqa: E402
from schemas import MeetingType  # noqa: E402
from utils import activity, audit  # noqa: E402
from utils.audit import AuditMiddleware  # noqa: E402

A = "/api/v1/admin"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(activity, "session_factory", Session)
    db = Session()
    db.add(Sido(code="11", name="서울"))
    u1, u2, u3 = (User(email=f"u{i}@t.run", nickname=n, realname=f"실명{i}", status=UserStatus.ACTIVE) for i, n in enumerate(["신고자", "악플러", "다른신고자"]))
    club = Club(name="강남 골프회", sido_code="11", status=ClubStatus.ACTIVE)
    db.add_all([u1, u2, u3, club])
    db.flush()
    social = Meeting(name="송년회", club_id=club.id, meeting_type=MeetingType.SOCIAL, status="SCHEDULED")
    db.add(social)
    db.flush()
    rs = [
        ContentReport(reporter_id=u1.id, target_type=ReportTargetType.USER, target_id=u2.id, reason=ReportReason.ABUSE, description="욕설"),
        ContentReport(reporter_id=u3.id, target_type=ReportTargetType.USER, target_id=u2.id, reason=ReportReason.ABUSE),
        ContentReport(reporter_id=u1.id, target_type=ReportTargetType.MEETING, target_id=social.id, reason=ReportReason.SPAM, status=ReportStatus.RESOLVED),
        ContentReport(reporter_id=u1.id, target_type=ReportTargetType.CLUB, target_id=999, reason=ReportReason.FRAUD),
    ]
    db.add_all(rs)
    db.add(UserBlock(blocker_id=u1.id, blocked_id=u2.id))
    db.commit()

    who = {"role": "SUPPORT_ADMIN"}
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.include_router(admin_pkg.router, prefix="/api/v1")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def _me():
        me = {"id": 1, "email": "cs@teeup.run", "name": "CS", "role": who["role"], "status": "ACTIVE", "type": "admin"}
        audit.set_actor(me)
        return me

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_admin_user] = _me

    def as_role(role):
        who["role"] = role
        return TestClient(app)

    yield db, (u1, u2, u3, club, social, rs), as_role
    db.close()


def audits(db, **kw):
    db.expire_all()
    q = db.query(AdminAuditLog)
    for k, v in kw.items():
        q = q.filter(getattr(AdminAuditLog, k) == v)
    return q.order_by(AdminAuditLog.id).all()


def test_list_filters_targets_and_same_target_count(env):
    db, (u1, u2, u3, club, social, rs), as_role = env
    c = as_role("SUPPORT_ADMIN")
    r = c.get(f"{A}/reports", params=[("status", "PENDING"), ("status", "REVIEWING")]).json()
    assert r["total"] == 3
    by_id = {x["id"]: x for x in r["items"]}
    first = by_id[rs[0].id]
    assert first["target"] == {"type": "USER", "id": u2.id, "name": "악플러", "link": f"/users/{u2.id}", "status": "ACTIVE", "club_id": None, "exists": True}
    assert first["same_target_count"] == 2 and first["reporter"]["nickname"] == "신고자"
    assert by_id[rs[3].id]["target"]["exists"] is False  # 지워진 대상

    resolved = c.get(f"{A}/reports", params={"status": "RESOLVED"}).json()["items"][0]
    assert resolved["target"]["link"] == f"/socials/{social.id}"
    assert c.get(f"{A}/reports", params={"target_type": "USER", "q": "욕설"}).json()["total"] == 1
    assert c.get(f"{A}/reports", params={"status": "DONE"}).status_code == 422
    assert as_role("CLUB_ADMIN").get(f"{A}/reports").status_code == 403


def test_detail_shows_other_reports_on_same_target(env):
    db, (u1, u2, u3, club, social, rs), as_role = env
    d = as_role("SUPPORT_ADMIN").get(f"{A}/reports/{rs[0].id}").json()
    assert [x["id"] for x in d["same_target"]] == [rs[1].id]
    assert as_role("SUPPORT_ADMIN").get(f"{A}/reports/9999").status_code == 404


def test_close_requires_note_and_is_audited(env):
    db, (u1, u2, u3, club, social, rs), as_role = env
    c = as_role("SUPPORT_ADMIN")
    r = c.put(f"{A}/reports/{rs[0].id}", json={"status": "RESOLVED"})
    assert r.status_code == 422 and r.json()["detail"][0]["field"] == "admin_note"
    assert c.put(f"{A}/reports/{rs[0].id}", json={"status": "REVIEWING"}).json()["status"] == "REVIEWING"
    r = c.put(f"{A}/reports/{rs[0].id}", json={"status": "RESOLVED", "admin_note": "회원 7일 정지 처리"})
    assert r.status_code == 200 and r.json()["admin_note"] == "회원 7일 정지 처리"
    rows = audits(db, action="STATUS_CHANGE", target_type="REPORT")
    assert rows[-1].before_json["status"] == "REVIEWING" and rows[-1].after_json["status"] == "RESOLVED" and rows[-1].reason == "회원 7일 정지 처리"


def test_bulk_dismiss_reports_partial_failure(env):
    db, (u1, u2, u3, club, social, rs), as_role = env
    c = as_role("SUPPORT_ADMIN")
    r = c.post(f"{A}/reports/bulk", json={"ids": [rs[0].id, rs[1].id, 9999], "action": "DISMISSED", "reason": "근거 없음"}).json()
    assert r["succeeded"] == [rs[0].id, rs[1].id] and r["failed"][0]["id"] == 9999
    db.expire_all()
    assert {db.get(ContentReport, rs[0].id).status, db.get(ContentReport, rs[1].id).status} == {ReportStatus.DISMISSED}
    r = c.post(f"{A}/reports/bulk", json={"ids": [rs[3].id], "action": "DISMISSED"}).json()
    assert r["failed"] and r["succeeded"] == []  # 메모 없이는 기각 안 됨


def test_blocks_list_and_unblock_with_reason(env):
    db, (u1, u2, u3, club, social, rs), as_role = env
    c = as_role("SUPPORT_ADMIN")
    r = c.get(f"{A}/blocks", params={"user_id": u2.id}).json()
    assert r["total"] == 1 and r["items"][0]["blocker"]["nickname"] == "신고자" and r["items"][0]["blocked"]["nickname"] == "악플러"
    bid = r["items"][0]["id"]
    assert c.delete(f"{A}/blocks/{bid}").status_code == 422
    assert c.delete(f"{A}/blocks/{bid}", params={"reason": "차단한 회원이 해제 요청"}).status_code == 200
    assert db.query(UserBlock).count() == 0
    row = audits(db, action="DELETE", target_type="USER_BLOCK")[0]
    assert row.before_json["blocker_id"] == u1.id and row.reason == "차단한 회원이 해제 요청"


def test_app_report_does_not_create_inquiry(env):
    """신고는 신고 화면에서만 처리한다. 예전처럼 [신고] 1:1 문의가 함께 생기지 않는다."""
    from models import AdminNotification, Inquiry
    from routers.auth import get_current_user
    from routers.moderation import router as moderation_router

    db, (u1, u2, *_), _ = env
    app = FastAPI()
    app.include_router(moderation_router, prefix="/api/v1")

    def _db():
        s = sessionmaker(bind=db.get_bind())()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: u1
    r = TestClient(app).post("/api/v1/moderation/reports", json={"target_type": "USER", "target_id": u2.id, "reason": "SPAM"})
    assert r.status_code == 201, r.text
    db.expire_all()
    assert db.query(ContentReport).count() == 5
    assert db.query(Inquiry).count() == 0
    assert db.query(AdminNotification).count() == 0  # 권한 있는 관리자가 없어 알림 대상도 없음
