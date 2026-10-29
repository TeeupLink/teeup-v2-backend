"""백오피스 클럽 보강 (backoffice #24) — 목록 집계·필터·정렬, 역할 변경 제한, 리더 교체, 내보내기, 상태·삭제 감사."""

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
from models import (AdminAuditLog, Club, ClubMembership, ClubRole, ClubStatus, Meeting, MembershipStatus, User,  # noqa: E402
                    UserActivityLog, UserStatus)
from models.region import Sido  # noqa: E402
from routers import admin as admin_pkg  # noqa: E402
from routers.admin.deps import get_admin_user  # noqa: E402
from schemas import MeetingType  # noqa: E402
from utils import activity  # noqa: E402
from utils.audit import AuditMiddleware  # noqa: E402

A = "/api/v1/admin"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(activity, "session_factory", Session)
    db = Session()
    db.add_all([Sido(code="11", name="서울"), Sido(code="26", name="부산")])
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


def club_with(db, name, sido="11", type_="REGULAR", members=2, pending=0, at=None):
    c = Club(name=name, sido_code=sido, type=type_, status=ClubStatus.ACTIVE, member_count=members, created_at=at or datetime(2026, 1, 1))
    db.add(c)
    db.flush()
    users = []
    for i in range(members + pending):
        u = User(email=f"{name}{i}@t.run", nickname=f"{name}{i}", realname=f"실명{i}", status=UserStatus.ACTIVE)
        db.add(u)
        db.flush()
        role = ClubRole.LEADER if i == 0 else ClubRole.MEMBER
        st = MembershipStatus.ACTIVE if i < members else MembershipStatus.PENDING
        db.add(ClubMembership(club_id=c.id, user_id=u.id, role=role, status=st))
        users.append(u)
    db.commit()
    return c, users


def audits(db, **kw):
    db.expire_all()
    q = db.query(AdminAuditLog)
    for k, v in kw.items():
        q = q.filter(getattr(AdminAuditLog, k) == v)
    return q.order_by(AdminAuditLog.id).all()


def test_list_counts_filters_sort(env):
    db, as_role = env
    club_with(db, "강남", members=3, pending=2, at=datetime(2026, 3, 1))
    club_with(db, "부산", sido="26", type_="IRREGULAR", members=1, at=datetime(2026, 2, 1))
    gone, _ = club_with(db, "삭제", members=1)
    gone.deleted_at = datetime(2026, 5, 1)
    db.commit()
    client = as_role("CLUB_ADMIN")

    live = {"include_deleted": False}
    rows = client.get(f"{A}/clubs", params=live).json()["data"]
    assert [r["name"] for r in rows] == ["강남", "부산"]
    g = rows[0]
    assert g["member_count"] == 3 and g["pending_count"] == 2 and g["leader_nickname"] == "강남0"
    assert [r["name"] for r in client.get(f"{A}/clubs", params={**live, "sido_code": "26"}).json()["data"]] == ["부산"]
    assert [r["name"] for r in client.get(f"{A}/clubs", params={**live, "type": "IRREGULAR"}).json()["data"]] == ["부산"]
    assert [r["name"] for r in client.get(f"{A}/clubs", params={**live, "sort": "member_count"}).json()["data"]] == ["부산", "강남"]
    assert len(client.get(f"{A}/clubs").json()["data"]) == 2  # 기본값은 삭제 제외 (교체 배포 #33 뒤)
    assert len(client.get(f"{A}/clubs", params={"include_deleted": True}).json()["data"]) == 3
    assert client.get(f"{A}/clubs", params={"sort": "description"}).status_code == 422


def test_role_change_to_leader_is_swap_never_two_leaders(env):
    db, as_role = env
    c, (leader, member) = club_with(db, "강남")
    client = as_role("CLUB_ADMIN")
    r = client.put(f"{A}/clubs/{c.id}/members/{member.id}/role", json={"new_role": "MANAGER", "reason": "총무"})
    assert r.status_code == 200
    (row,) = audits(db, target_type="CLUB_MEMBERSHIP", action="UPDATE")
    assert row.before_json == {"role": "MEMBER"} and row.after_json == {"role": "MANAGER"} and row.reason == "총무"

    # 기존 백오피스 방식: 역할 API 로 LEADER 지정 → 기존 리더는 자동으로 MANAGER
    assert client.put(f"{A}/clubs/{c.id}/members/{member.id}/role", json={"new_role": "LEADER"}).status_code == 200
    db.expire_all()
    roles = {m.user_id: m.role for m in db.query(ClubMembership).filter_by(club_id=c.id).all()}
    assert roles == {leader.id: ClubRole.MANAGER, member.id: ClubRole.LEADER}


def test_leader_change_is_atomic_swap(env):
    db, as_role = env
    c, (leader, member, _) = club_with(db, "강남", pending=1)
    pending_user = db.query(ClubMembership).filter_by(club_id=c.id, status=MembershipStatus.PENDING).one().user_id
    client = as_role("CLUB_ADMIN")
    assert client.post(f"{A}/clubs/{c.id}/leader", json={"new_leader_id": pending_user}).status_code == 400

    r = client.post(f"{A}/clubs/{c.id}/leader", json={"new_leader_id": member.id, "reason": "리더 요청"})
    assert r.status_code == 200, r.text
    db.expire_all()
    roles = {m.user_id: m.role for m in db.query(ClubMembership).filter_by(club_id=c.id).all()}
    assert roles[leader.id] == ClubRole.MANAGER and roles[member.id] == ClubRole.LEADER
    assert sum(1 for v in roles.values() if v == ClubRole.LEADER) == 1
    (row,) = audits(db, target_type="CLUB", action="UPDATE")
    assert row.after_json == {"leader_user_ids": [member.id]} and row.meta_json["action"] == "LEADER_CHANGE"

    d = client.get(f"{A}/clubs/{c.id}").json()
    assert d["leader"]["id"] == member.id and d["pending_count"] == 1


def test_remove_member_audit_activity_and_leader_guard(env):
    db, as_role = env
    c, (leader, member) = club_with(db, "강남")
    client = as_role("CLUB_ADMIN")
    assert client.delete(f"{A}/clubs/{c.id}/members/{leader.id}").status_code == 400
    assert client.delete(f"{A}/clubs/{c.id}/members/{member.id}", params={"reason": "비매너"}).status_code == 200
    (row,) = audits(db, target_type="CLUB_MEMBERSHIP", action="DELETE")
    assert row.reason == "비매너" and row.before_json["user_id"] == member.id
    db.expire_all()
    (act,) = db.query(UserActivityLog).filter_by(user_id=member.id).all()
    assert act.event_type == "CLUB_LEAVE" and act.meta_json["by_admin"] is True
    assert db.get(Club, c.id).member_count == 1


def test_status_change_validated_and_audited(env):
    db, as_role = env
    c, _ = club_with(db, "강남")
    db.add(Meeting(name="r", club_id=c.id, meeting_type=MeetingType.ROUND, meeting_time=datetime(2026, 11, 1)))
    db.commit()
    client = as_role("CLUB_ADMIN")
    assert client.put(f"{A}/clubs/{c.id}", json={"status": "PENDING"}).status_code == 400
    assert client.put(f"{A}/clubs/{c.id}", json={"status": "INACTIVE", "reason": "운영 중단"}).status_code == 200
    (row,) = audits(db, target_type="CLUB", action="STATUS_CHANGE")
    assert row.before_json == {"status": "ACTIVE"} and row.after_json == {"status": "INACTIVE"} and row.reason == "운영 중단"
    d = client.get(f"{A}/clubs/{c.id}").json()
    assert d["round_count"] == 1 and d["social_count"] == 0 and d["last_meeting_at"].startswith("2026-11-01")


def test_notice_search_and_category_delete_guard(env):
    from models import ClubNotice, Regulation, RegulationCategory

    db, as_role = env
    c, (leader, _) = club_with(db, "강남")
    db.add_all([ClubNotice(club_id=c.id, title="11월 정기 라운딩", content="장소 공지", author_id=leader.id),
                ClubNotice(club_id=c.id, title="회비 안내", content="11월분 입금", author_id=leader.id)])
    full, empty = RegulationCategory(club_id=c.id, name="회칙", order=1), RegulationCategory(club_id=c.id, name="빈 카테고리", order=2)
    db.add_all([full, empty])
    db.flush()
    db.add(Regulation(club_id=c.id, category_id=full.id, title="제1조", content="목적", status="ACTIVE", created_by=0))
    db.commit()
    client = as_role("CLUB_ADMIN")

    titles = [n["title"] for n in client.get(f"{A}/clubs/{c.id}/notices", params={"q": "11월"}).json()["data"]]
    assert sorted(titles) == ["11월 정기 라운딩", "회비 안내"]
    assert [n["title"] for n in client.get(f"{A}/clubs/{c.id}/notices", params={"q": "회비"}).json()["data"]] == ["회비 안내"]

    r = client.delete(f"{A}/clubs/{c.id}/regulations/categories/{full.id}")
    assert r.status_code == 409 and "1개" in r.json()["detail"]
    assert client.delete(f"{A}/clubs/{c.id}/regulations/categories/{empty.id}").status_code == 200
    db.expire_all()
    assert db.query(Regulation).count() == 1


def test_leader_cannot_be_demoted_after_cutover(env):
    db, as_role = env
    c, (leader, member) = club_with(db, "강남")
    client = as_role("CLUB_ADMIN")
    r = client.put(f"{A}/clubs/{c.id}/members/{leader.id}/role", json={"new_role": "MEMBER"})
    assert r.status_code == 409 and "리더 교체" in r.json()["detail"]
    db.expire_all()
    assert db.query(ClubMembership).filter_by(club_id=c.id, user_id=leader.id).one().role == ClubRole.LEADER
    # 다른 멤버의 역할 변경은 그대로 된다
    assert client.put(f"{A}/clubs/{c.id}/members/{member.id}/role", json={"new_role": "MANAGER"}).status_code == 200
