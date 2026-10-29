"""백오피스 관리자·프로필·검색·알림 벨 보강 (backoffice #27).

SQLite 메모리 DB. 요청자는 관리자 1번(최고 관리자)으로 둔다.
"""

import hashlib
import os
from datetime import datetime

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
from models import Admin, AdminAuditLog, AdminNotification, AdminRole, Club, ClubStatus, Meeting, Notification, User, UserStatus  # noqa: E402
from models.region import Sido  # noqa: E402
from routers import admin as admin_pkg  # noqa: E402
from routers.admin.deps import get_admin_user  # noqa: E402
from schemas import MeetingType  # noqa: E402
from utils import activity, admin_notify, audit, notification_service  # noqa: E402
from utils.audit import AuditMiddleware  # noqa: E402
from utils.passwords import verify_password  # noqa: E402

A = "/api/v1/admin"
PW = "Teeup!2026"


def sha(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(activity, "session_factory", Session)
    db = Session()
    db.add(Sido(code="11", name="서울"))
    db.add_all([
        Admin(id=1, email="owner@teeup.run", password=sha(PW), name="대표", role=AdminRole.SUPER_ADMIN, status=UserStatus.ACTIVE),
        Admin(id=2, email="club@teeup.run", password=sha(PW), name="클럽팀", role=AdminRole.CLUB_ADMIN, status=UserStatus.ACTIVE),
        Admin(id=3, email="cs@teeup.run", password=sha(PW), name="CS", role=AdminRole.SUPPORT_ADMIN, status=UserStatus.ACTIVE),
        Admin(id=4, email="old@teeup.run", password=sha(PW), name="퇴사", role=AdminRole.SUPPORT_ADMIN, status=UserStatus.DEACTIVATED),
    ])
    db.commit()

    who = {"id": 1, "role": "SUPER_ADMIN"}
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
    def _me():
        # 실제 get_admin_user 처럼 감사 로그 행위자를 정한다
        me = {"id": who["id"], "email": "x@teeup.run", "name": "x", "role": who["role"], "status": "ACTIVE", "type": "admin"}
        audit.set_actor(me)
        return me

    app.dependency_overrides[get_admin_user] = _me

    def as_admin(admin_id, role):
        who.update(id=admin_id, role=role)
        return TestClient(app)

    yield db, as_admin
    db.close()


def audits(db, **kw):
    db.expire_all()
    q = db.query(AdminAuditLog)
    for k, v in kw.items():
        q = q.filter(getattr(AdminAuditLog, k) == v)
    return q.order_by(AdminAuditLog.id).all()


def test_create_requires_role_strong_password_unique_email(env):
    db, as_admin = env
    c = as_admin(1, "SUPER_ADMIN")
    body = {"email": "New@Teeup.run", "name": "신규", "password": PW}

    r = c.post(f"{A}/admins", json=body)  # 예전에는 역할을 빼면 최고 관리자가 됐다
    assert r.status_code == 422 and r.json()["detail"][0]["field"] == "role"
    r = c.post(f"{A}/admins", json={**body, "role": "CONTENT_ADMIN", "password": "abcdef"})
    assert r.status_code == 422 and r.json()["detail"][0]["field"] == "password"
    r = c.post(f"{A}/admins", json={**body, "role": "CONTENT_ADMIN", "email": "CLUB@teeup.run"})
    assert r.status_code == 409 and r.json()["detail"][0]["field"] == "email"

    r = c.post(f"{A}/admins", json={**body, "role": "CONTENT_ADMIN"})
    assert r.status_code == 201 and r.json()["email"] == "new@teeup.run" and r.json()["role"] == "CONTENT_ADMIN"
    assert audits(db, action="CREATE", target_type="ADMIN", result="SUCCESS")[-1].after_json["role"] == "CONTENT_ADMIN"
    assert as_admin(2, "CLUB_ADMIN").get(f"{A}/admins").status_code == 403


def test_update_guards_self_and_last_super(env):
    db, as_admin = env
    c = as_admin(1, "SUPER_ADMIN")
    assert c.put(f"{A}/admins/2", json={"role": "WIZARD"}).status_code == 422
    assert c.put(f"{A}/admins/2", json={"status": "DELETED"}).status_code == 422
    assert c.put(f"{A}/admins/1", json={"role": "CLUB_ADMIN"}).status_code == 400  # 자기 자신

    r = c.put(f"{A}/admins/3", json={"status": "DEACTIVATED"})
    assert r.status_code == 200 and r.json()["status"] == "DEACTIVATED"
    row = audits(db, action="STATUS_CHANGE", target_type="ADMIN")[0]
    assert row.before_json == {"status": "ACTIVE"} and row.after_json == {"status": "DEACTIVATED"}

    # 2번을 최고 관리자로 올린 뒤 2번이 1번을 강등하면, 2번이 남으므로 된다
    c.put(f"{A}/admins/2", json={"role": "SUPER_ADMIN"})
    c2 = as_admin(2, "SUPER_ADMIN")
    assert c2.put(f"{A}/admins/1", json={"role": "USER_ADMIN"}).status_code == 200
    # 이제 2번이 마지막 최고 관리자 — 1번(최고 관리자 아님)이 2번을 강등·삭제할 수는 없다
    db.query(Admin).filter(Admin.id == 1).update({Admin.role: AdminRole.SUPER_ADMIN, Admin.status: UserStatus.DEACTIVATED})
    db.commit()
    c1 = as_admin(1, "SUPER_ADMIN")
    assert c1.put(f"{A}/admins/2", json={"role": "CLUB_ADMIN"}).status_code == 409
    assert c1.delete(f"{A}/admins/2").status_code == 409


def test_list_role_filter_and_last_login(env):
    db, as_admin = env
    db.add(AdminAuditLog(request_id="r", actor_admin_id=2, actor_email="club@teeup.run", action="LOGIN", method="POST", path="/login",
                         status_code=200, result="SUCCESS", ip="1.1.1.1", created_at=datetime(2026, 10, 28, 9, 0)))
    db.commit()
    c = as_admin(1, "SUPER_ADMIN")
    rows = {a["id"]: a for a in c.get(f"{A}/admins", params={"limit": 50}).json()["data"]}
    assert rows[2]["last_login_at"].startswith("2026-10-28T09:00") and rows[3]["last_login_at"] is None
    assert [a["id"] for a in c.get(f"{A}/admins", params={"role": "SUPPORT_ADMIN"}).json()["data"]] == [4, 3]


def test_password_reset_and_delete_are_audited_without_values(env):
    db, as_admin = env
    c = as_admin(1, "SUPER_ADMIN")
    assert c.put(f"{A}/admins/3/password", json={"new_password": "short"}).status_code == 422
    assert c.put(f"{A}/admins/3/password", json={"new_password": "Golf#2026x"}).status_code == 200
    db.expire_all()
    assert verify_password("Golf#2026x", db.get(Admin, 3).password) and not db.get(Admin, 3).password.startswith("Golf")
    row = audits(db, target_type="ADMIN_PASSWORD")[0]
    assert "Golf" not in str(row.after_json)
    assert c.delete(f"{A}/admins/3", params={"reason": "퇴사"}).status_code == 200
    d = audits(db, action="DELETE", target_type="ADMIN")[0]
    assert d.reason == "퇴사" and d.before_json["email"] == "cs@teeup.run"


def test_profile_real_role_and_password_field_errors(env):
    db, as_admin = env
    c = as_admin(3, "SUPPORT_ADMIN")
    me = c.get(f"{A}/profile").json()
    assert me["role"] == "SUPPORT_ADMIN" and me["email"] == "cs@teeup.run"  # 예전에는 모두 "ADMIN"

    assert c.put(f"{A}/profile", json={"name": "고객지원", "phone_number": "01012345678", "role": "SUPER_ADMIN"}).json()["role"] == "SUPPORT_ADMIN"

    def pw(cur, new, conf):
        return c.put(f"{A}/password", json={"current_password": cur, "new_password": new, "confirm_password": conf})

    assert pw(PW, "weak", "weak").json()["detail"][0]["field"] == "new_password"
    assert pw(PW, "Golf#2026x", "Golf#2026y").json()["detail"][0]["field"] == "confirm_password"
    r = pw("wrong", "Golf#2026x", "Golf#2026x")
    assert r.status_code == 400 and r.json()["detail"][0]["field"] == "current_password"
    assert pw(PW, "Golf#2026x", "Golf#2026x").status_code == 200
    db.expire_all()
    assert verify_password("Golf#2026x", db.get(Admin, 3).password) and not db.get(Admin, 3).password.startswith("Golf")


def test_search_is_scoped_by_role_and_links_new_routes(env):
    db, as_admin = env
    club = Club(name="강남 골프회", sido_code="11", status=ClubStatus.ACTIVE)
    db.add(club)
    db.flush()
    db.add_all([User(email="gangnam@t.run", nickname="강남러", status=UserStatus.ACTIVE),
                Meeting(name="강남 송년회", club_id=club.id, meeting_type=MeetingType.SOCIAL, status="SCHEDULED"),
                Meeting(name="강남 라운딩", club_id=club.id, meeting_type=MeetingType.ROUND, status="SCHEDULED")])
    db.commit()

    r = as_admin(1, "SUPER_ADMIN").get(f"{A}/search", params={"q": "강남"}).json()
    assert r["clubs"][0]["link"] == f"/clubs/{club.id}"
    assert {m["link"].split("/")[1] for m in r["meetings"]} == {"socials", "rounds"}

    r = as_admin(3, "SUPPORT_ADMIN").get(f"{A}/search", params={"q": "강남"}).json()
    assert len(r["users"]) == 1 and r["clubs"] == [] and r["meetings"] == [] and r["admins"] == []
    r = as_admin(2, "CLUB_ADMIN").get(f"{A}/search", params={"q": "teeup"}).json()
    assert r["admins"] == []


def test_admin_alerts_go_to_bell_for_handlers_only(env):
    db, as_admin = env
    # 회원 id 가 관리자 id 와 겹쳐도 회원 알림으로 가지 않는다 (예전 버그)
    db.add(User(id=3, email="member3@t.run", nickname="회원3", status=UserStatus.ACTIVE))
    db.commit()
    notification_service.create_inquiry_admin_notification(db=db, inquiry_title="로그인 안 됨", inquiry_type="ACCOUNT", user_name="회원3", inquiry_id=12)
    db.expire_all()
    assert db.query(Notification).count() == 0
    rows = db.query(AdminNotification).order_by(AdminNotification.admin_id).all()
    assert [(n.admin_id, n.type, n.link) for n in rows] == [(1, "INQUIRY", "/inquiries/12"), (3, "INQUIRY", "/inquiries/12")]  # 비활성 4번 제외

    assert admin_notify.notify("JOIN_REQUEST", "강남 골프회 가입신청", link="/clubs/1/join-requests") == 2  # 최고 + 클럽
    assert admin_notify.notify("BILLING_FAILURE", "정기결제 실패") == 2  # 최고 + 고객지원(조회)
    assert admin_notify.notify("UNKNOWN", "x") == 0

    c = as_admin(3, "SUPPORT_ADMIN")
    assert c.get(f"{A}/notifications/unread-count").json() == {"count": 2}
