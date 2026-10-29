"""백오피스 라운딩 보강 (backoffice #23) — 목록 기간·정렬, 팀 멤버 지정, 총타 일괄 저장.

SQLite 메모리 DB.
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
from models import (AdminAuditLog, Club, ClubStatus, Guest, Meeting, MeetingParticipant, Team, TeamMember, User,  # noqa: E402
                    UserScoreHistory, UserStatus)
from models.region import Sido  # noqa: E402
from routers import admin as admin_pkg  # noqa: E402
from routers.admin.deps import get_admin_user  # noqa: E402
from schemas import MeetingType  # noqa: E402
from utils import activity  # noqa: E402
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
    club = Club(name="강남 골프회", sido_code="11", status=ClubStatus.ACTIVE, member_count=2)
    db.add(club)
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
    yield db, club, as_role
    db.close()


def make_round(db, club, name, at, status="SCHEDULED"):
    m = Meeting(name=name, club_id=club.id, meeting_type=MeetingType.ROUND, meeting_time=at, status=status, max_participants=8)
    db.add(m)
    db.commit()
    return m


def make_people(db, meeting, n_users=2, guest=True):
    out = []
    for i in range(n_users):
        u = User(email=f"u{meeting.id}_{i}@t.run", nickname=f"u{meeting.id}_{i}", realname=f"회원{i}", status=UserStatus.ACTIVE)
        db.add(u)
        db.flush()
        p = MeetingParticipant(meeting_id=meeting.id, user_id=u.id, participant_type="USER", status="CONFIRMED")
        db.add(p)
        out.append(p)
    if guest:
        g = Guest(name="게스트")
        db.add(g)
        db.flush()
        p = MeetingParticipant(meeting_id=meeting.id, guest_id=g.id, participant_type="GUEST", status="CONFIRMED")
        db.add(p)
        out.append(p)
    db.commit()
    return out


def audits(db, **kw):
    db.expire_all()
    q = db.query(AdminAuditLog)
    for k, v in kw.items():
        q = q.filter(getattr(AdminAuditLog, k) == v)
    return q.order_by(AdminAuditLog.id).all()


def test_rounding_list_today_range_sort_and_confirmed(env):
    db, club, as_role = env
    now = get_kst_now().replace(tzinfo=None, hour=10, minute=0, second=0, microsecond=0)
    today = make_round(db, club, "오늘 라운딩", now)
    make_round(db, club, "지난주", now - timedelta(days=7))
    make_round(db, club, "다음주", now + timedelta(days=7))
    ps = make_people(db, today)
    ps[0].status = "PENDING"
    db.commit()
    client = as_role("MEETING_ADMIN")

    r = client.get(f"{A}/meetings/rounding", params={"date": "today"}).json()
    assert [m["name"] for m in r["data"]] == ["오늘 라운딩"]
    assert r["data"][0]["participant_count"] == 3 and r["data"][0]["confirmed_count"] == 2

    names = [m["name"] for m in client.get(f"{A}/meetings/rounding").json()["data"]]
    assert names == ["다음주", "오늘 라운딩", "지난주"]
    names = [m["name"] for m in client.get(f"{A}/meetings/rounding", params={"sort": "meeting_time"}).json()["data"]]
    assert names == ["지난주", "오늘 라운딩", "다음주"]

    d = lambda dt: dt.strftime("%Y-%m-%d")  # noqa: E731
    r = client.get(f"{A}/meetings/rounding", params={"date_from": d(now - timedelta(days=7)), "date_to": d(now)}).json()
    assert {m["name"] for m in r["data"]} == {"지난주", "오늘 라운딩"}
    assert client.get(f"{A}/meetings/rounding", params={"sort": "name"}).status_code == 422


def test_scores_bulk_partial_failure_and_audit(env):
    db, club, as_role = env
    m = make_round(db, club, "토요 라운딩", datetime(2026, 10, 25, 7, 0))
    u1, u2, guest = make_people(db, m)
    db.add(UserScoreHistory(user_id=u1.user_id, meeting_id=m.id, gross_score=95, net_score=95, handicap_used=0, played_at=m.meeting_time))
    db.commit()
    client = as_role("MEETING_ADMIN")

    r = client.put(f"{A}/meetings/{m.id}/scores/bulk", json={"items": [
        {"participant_id": u1.id, "score": 90},
        {"participant_id": u2.id, "score": 300},
        {"participant_id": guest.id, "score": 88},
        {"participant_id": 9999, "score": 88},
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["succeeded"] == [u1.id]
    assert {f["id"] for f in body["failed"]} == {u2.id, guest.id, 9999}
    assert "게스트" in next(f["message"] for f in body["failed"] if f["id"] == guest.id)

    db.expire_all()
    assert db.query(UserScoreHistory).filter_by(user_id=u1.user_id).one().gross_score == 90
    (ok,) = audits(db, target_type="SCORE", result="SUCCESS")
    assert ok.before_json == {"score": 95} and ok.after_json == {"score": 90} and ok.target_id == str(u1.id)
    assert len(audits(db, target_type="SCORE", result="FAIL")) == 3

    # 단건 API 도 같은 로직
    assert client.put(f"{A}/meetings/{m.id}/scores/{u2.id}", json={"score": 101}).json()["score"] == 101
    assert client.put(f"{A}/meetings/{m.id}/scores/{guest.id}", json={"score": 101}).status_code == 400


def test_team_members_assign_moves_between_teams(env):
    db, club, as_role = env
    m = make_round(db, club, "토요 라운딩", datetime(2026, 10, 25, 7, 0))
    u1, u2, guest = make_people(db, m)
    t1, t2 = Team(name="1조", meeting_id=m.id), Team(name="2조", meeting_id=m.id)
    db.add_all([t1, t2])
    db.flush()
    db.add(TeamMember(team_id=t1.id, user_id=u1.user_id, order=1))
    db.add(TeamMember(team_id=t1.id, user_id=u2.user_id, order=2))
    db.commit()
    client = as_role("MEETING_ADMIN")

    r = client.put(f"{A}/meetings/{m.id}/teams/{t2.id}/members", json={"participant_ids": [guest.id, u2.id]})
    assert r.status_code == 200, r.text
    assert r.json()["moved_from_teams"] == [t1.id]

    teams = {t["name"]: t for t in client.get(f"{A}/meetings/{m.id}/teams").json()}
    assert [x["participant_id"] for x in teams["1조"]["members"]] == [u1.id]
    assert [x["participant_id"] for x in teams["2조"]["members"]] == [guest.id, u2.id]
    assert teams["2조"]["members"][0]["is_guest"] is True

    (row,) = audits(db, target_type="TEAM", action="UPDATE")
    assert row.after_json == {"participant_ids": [guest.id, u2.id]}

    other = make_round(db, club, "다른 라운딩", datetime(2026, 10, 26, 7, 0))
    (stranger,) = make_people(db, other, n_users=1, guest=False)
    assert client.put(f"{A}/meetings/{m.id}/teams/{t2.id}/members", json={"participant_ids": [stranger.id]}).status_code == 422
    assert client.put(f"{A}/meetings/{m.id}/teams/{t2.id}/members", json={"participant_ids": []}).status_code == 200
    db.expire_all()
    assert db.query(TeamMember).filter_by(team_id=t2.id).count() == 0


def test_round_write_needs_meetings_write(env):
    db, club, as_role = env
    m = make_round(db, club, "토요 라운딩", datetime(2026, 10, 25, 7, 0))
    (u1,) = make_people(db, m, n_users=1, guest=False)
    client = as_role("SUPPORT_ADMIN")
    r = client.put(f"{A}/meetings/{m.id}/scores/bulk", json={"items": [{"participant_id": u1.id, "score": 90}]})
    assert r.status_code == 403
