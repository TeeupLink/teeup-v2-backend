"""관리자 id 를 회원 id 로 쓰지 않는다 — 모임 개설자·자동 참가자·정산 작성자.

관리자(admins.id)와 회원(users.id)은 번호가 겹친다. 예전에는 관리자가 만든 공개 라운딩·소셜에
같은 번호의 회원이 개설자·참가자로 들어갔다.
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
from models import Club, ClubMembership, ClubRole, ClubStatus, Meeting, MeetingParticipant, MembershipStatus, User, UserStatus  # noqa: E402
from models.region import Sido  # noqa: E402
from routers import admin as admin_pkg  # noqa: E402
from routers.admin.deps import get_admin_user  # noqa: E402
from routers.admin.meetings import _expense_owner  # noqa: E402
from schemas import MeetingType  # noqa: E402
from utils.audit import AuditMiddleware  # noqa: E402

A = "/api/v1/admin"
ADMIN_ID = 1


@pytest.fixture
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Sido(code="11", name="서울"))
    # 관리자와 같은 번호(1)의 회원 — 예전 버그에서 이 회원이 끌려 들어갔다
    bystander = User(id=ADMIN_ID, email="bystander@t.run", nickname="bystander", status=UserStatus.ACTIVE)
    leader = User(email="leader@t.run", nickname="leader", status=UserStatus.ACTIVE)
    club = Club(name="강남 골프회", sido_code="11", status=ClubStatus.ACTIVE, member_count=1)
    db.add_all([bystander, leader, club])
    db.flush()
    db.add(ClubMembership(club_id=club.id, user_id=leader.id, role=ClubRole.LEADER, status=MembershipStatus.ACTIVE))
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
    app.dependency_overrides[get_admin_user] = lambda: {"id": ADMIN_ID, "email": "owner@teeup.run", "name": "운영", "role": "SUPER_ADMIN", "status": "ACTIVE"}
    yield db, club, leader, TestClient(app)
    db.close()


def test_public_round_has_no_admin_as_creator_or_participant(env):
    db, club, _, client = env
    at = datetime(2026, 11, 7, 7, 0)
    r = client.post(f"{A}/meetings/rounding", json={
        "club_id": club.id, "name": "공개 라운딩", "meeting_time": at.isoformat(),
        "application_deadline": (at - timedelta(days=2)).isoformat(), "tee_times": ["07:00"],
    })
    assert r.status_code == 200, r.text
    db.expire_all()
    m = db.get(Meeting, r.json()["id"])
    assert m.created_by is None
    assert db.query(MeetingParticipant).filter_by(meeting_id=m.id).count() == 0


def test_social_has_no_admin_as_creator_or_participant(env):
    db, club, _, client = env
    r = client.post(f"{A}/meetings/event", json={"club_id": club.id, "name": "송년회", "meeting_time": "2026-12-20T19:00:00"})
    assert r.status_code == 200, r.text
    db.expire_all()
    mid = r.json()["id"] if "id" in r.json() else r.json()["data"]["id"]
    m = db.get(Meeting, mid)
    assert m.created_by is None
    assert db.query(MeetingParticipant).filter_by(meeting_id=m.id, user_id=ADMIN_ID).count() == 0


def test_expense_owner_falls_back_to_participant_then_leader(env):
    db, club, leader, _ = env
    m = Meeting(name="m", club_id=club.id, meeting_type=MeetingType.ROUND, meeting_time=datetime(2026, 11, 7))
    db.add(m)
    db.commit()
    assert _expense_owner(db, m) == leader.id

    member = User(email="m@t.run", nickname="m", status=UserStatus.ACTIVE)
    db.add(member)
    db.flush()
    db.add(MeetingParticipant(meeting_id=m.id, user_id=member.id, participant_type="USER", status="CONFIRMED"))
    db.commit()
    assert _expense_owner(db, m) == member.id

    other = Club(name="빈 클럽", sido_code="11", status=ClubStatus.ACTIVE, member_count=0)
    db.add(other)
    db.flush()
    empty = Meeting(name="e", club_id=other.id, meeting_type=MeetingType.ROUND, meeting_time=datetime(2026, 11, 7))
    db.add(empty)
    db.commit()
    with pytest.raises(Exception) as e:
        _expense_owner(db, empty)
    assert "정산 작성자" in str(e.value.detail)
