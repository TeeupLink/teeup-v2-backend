"""번호 공간이 다른 id 를 섞지 않는다 — 정산 대상(회원·게스트·참가자), 관리자 알림(관리자·회원).

일부러 번호가 겹치는 회원·게스트·관리자를 만들어 두고, 엉뚱한 사람에게 붙지 않는지 본다.
"""

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
from models import (AdminNotification, Club, ClubStatus, ExpenseItemParticipant, Guest, Meeting, MeetingParticipant,  # noqa: E402
                    Notification, User, UserStatus)
from models.enums import NotificationStatus, NotificationType  # noqa: E402
from models.region import Sido  # noqa: E402
from routers import admin as admin_pkg  # noqa: E402
from routers.admin.deps import get_admin_user  # noqa: E402
from schemas import MeetingType  # noqa: E402
from utils.audit import AuditMiddleware  # noqa: E402

A = "/api/v1/admin"
N = 5  # 겹치는 번호


@pytest.fixture
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Sido(code="11", name="서울"))
    club = Club(name="강남 골프회", sido_code="11", status=ClubStatus.ACTIVE, member_count=1)
    db.add(club)
    db.commit()

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
    app.dependency_overrides[get_admin_user] = lambda: {"id": N, "email": "owner@teeup.run", "name": "운영", "role": "SUPER_ADMIN", "status": "ACTIVE"}
    yield db, club, TestClient(app)
    db.close()


def colliding_meeting(db, club, kind, ids=(None, None)):
    """회원 id 5 와 게스트 id 5 가 함께 참가한 모임. ids 로 참가자 id 를 고정할 수 있다"""
    db.add(User(id=N, email="u5@t.run", nickname="회원5", status=UserStatus.ACTIVE))
    db.add(Guest(id=N, name="게스트5"))
    m = Meeting(name="m", club_id=club.id, meeting_type=kind, meeting_time=datetime(2026, 11, 7, 7))
    db.add(m)
    db.flush()
    pu = MeetingParticipant(id=ids[0], meeting_id=m.id, user_id=N, participant_type="USER", status="CONFIRMED")
    pg = MeetingParticipant(id=ids[1], meeting_id=m.id, guest_id=N, participant_type="GUEST", status="CONFIRMED")
    db.add_all([pu, pg])
    db.commit()
    return m, pu, pg


def shares(db):
    db.expire_all()
    rows = db.query(ExpenseItemParticipant).all()
    out = {}
    for r in rows:
        k = ("g", r.guest_id) if r.guest_id else ("u", r.user_id)
        out[k] = out.get(k, 0) + float(r.amount)
    return out


def test_round_settlement_by_participant_ids_keeps_member_and_guest_apart(env):
    db, club, client = env
    m, pu, pg = colliding_meeting(db, club, MeetingType.ROUND)
    pids = [pu.id, pg.id]
    r = client.post(f"{A}/meetings/{m.id}/settlement/rounding", json={
        "green_fee": 100000, "caddy_fee": 0, "cart_fee": 0, "other_fee": 30000, "total_cost": 130000,
        "green_fee_participant_ids": pids, "caddy_fee_participant_ids": pids, "cart_fee_participant_ids": pids,
        "other_expense_items": [{"title": "식사", "amount": 30000, "participant_ids": pids}],
    })
    assert r.status_code == 200, r.text
    assert shares(db) == {("u", N): 65000.0, ("g", N): 65000.0}


def test_round_settlement_rejects_ambiguous_legacy_ids(env):
    db, club, client = env
    m, _, _ = colliding_meeting(db, club, MeetingType.ROUND)
    r = client.post(f"{A}/meetings/{m.id}/settlement/rounding", json={
        "green_fee": 100000, "total_cost": 100000,
        "green_fee_participants": [N], "caddy_fee_participants": [N], "cart_fee_participants": [N],
    })
    assert r.status_code == 400
    assert "participant_ids" in r.json()["detail"]
    assert shares(db) == {}


def test_social_settlement_targets_are_participant_ids_only(env):
    db, club, client = env
    # 게스트 참가자의 id 가 5 → 예전 조회(id·user_id·guest_id 중 하나)는 회원 5 의 참가 행을 먼저 집었다
    m, pu, pg = colliding_meeting(db, club, MeetingType.SOCIAL, ids=(N - 1, N))
    r = client.post(f"{A}/meetings/{m.id}/settlement/social", json={
        "expense_items": [{"title": "식사", "amount": 50000}], "settlement_targets": [pg.id], "total_cost": 50000,
    })
    assert r.status_code == 200, r.text
    assert shares(db) == {("g", N): 50000.0}


def test_admin_bell_never_touches_member_notifications(env):
    db, _, client = env
    db.add(User(id=N, email="u5@t.run", nickname="회원5", status=UserStatus.ACTIVE))
    member_note = Notification(user_id=N, type=list(NotificationType)[0], title="회원 알림", content="개인 내용", status=NotificationStatus.UNREAD)
    db.add(member_note)
    db.add(AdminNotification(admin_id=N, type="INQUIRY", title="새 문의", link="/inquiries/1"))
    db.commit()

    items = client.get(f"{A}/notifications").json()
    assert [i["title"] for i in items] == ["새 문의"]
    assert client.get(f"{A}/notifications/unread-count").json() == {"count": 1}
    assert client.put(f"{A}/notifications/{member_note.id}/read").status_code in (200, 404)
    assert client.put(f"{A}/notifications/read-all").json()["success"] is True

    db.expire_all()
    assert db.get(Notification, member_note.id).status == NotificationStatus.UNREAD
    assert client.get(f"{A}/notifications/unread-count").json() == {"count": 0}
