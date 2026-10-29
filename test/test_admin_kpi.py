"""백오피스 KPI (backoffice #32). SQLite 메모리 DB, 오늘 날짜를 고정한다."""

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
from models import Club, ClubStatus, Meeting, User, UserActivityLog, UserStatus  # noqa: E402
from models.region import Sido  # noqa: E402
from routers import admin as admin_pkg  # noqa: E402
from routers.admin import kpi as kpi_mod  # noqa: E402
from routers.admin.deps import get_admin_user  # noqa: E402
from schemas import MeetingType  # noqa: E402

A = "/api/v1/admin"
TODAY = datetime(2026, 10, 29, 12, 0)  # 목요일


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(kpi_mod, "get_kst_now", lambda: TODAY)
    db = Session()
    db.add(Sido(code="11", name="서울"))
    users = []
    # 10/5(월) 주 가입 3명, 10/12 주 가입 1명, 게스트 1명(제외)
    for i, (nick, at) in enumerate([("a", "2026-10-05"), ("b", "2026-10-06"), ("c", "2026-10-07"), ("d", "2026-10-13"), ("guest_x", "2026-10-13")]):
        u = User(email=f"{nick}@t.run", nickname=nick, status=UserStatus.ACTIVE, created_at=datetime.fromisoformat(at + "T09:00:00"))
        db.add(u)
        users.append(u)
    db.flush()
    a, b, c, d, _ = users

    def login(u, day, h=10):
        db.add(UserActivityLog(user_id=u.id, event_type="LOGIN", summary="로그인", created_at=datetime.fromisoformat(f"{day}T{h:02d}:00:00")))

    # 활동 로그는 10/10 부터 쌓였다
    login(a, "2026-10-10")
    login(a, "2026-10-13")
    login(a, "2026-10-13", 20)  # 같은 날 두 번 → 한 번
    login(b, "2026-10-14")
    login(d, "2026-10-28")
    club = Club(name="강남", sido_code="11", status=ClubStatus.ACTIVE, created_at=datetime(2026, 10, 8))
    db.add(club)
    db.flush()
    for day, t, st in [("2026-10-11", MeetingType.ROUND, "COMPLETED"), ("2026-10-18", MeetingType.ROUND, "CANCELED"), ("2026-10-25", MeetingType.SOCIAL, "COMPLETED")]:
        db.add(Meeting(name="m", club_id=club.id, meeting_type=t, status=st, meeting_time=datetime.fromisoformat(day + "T07:00:00")))
    db.commit()

    who = {"role": "SUPER_ADMIN"}
    app = FastAPI()
    app.include_router(admin_pkg.router, prefix="/api/v1")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_admin_user] = lambda: {"id": 1, "email": "o@t.run", "role": who["role"], "status": "ACTIVE", "type": "admin"}

    def as_role(role):
        who["role"] = role
        return TestClient(app)

    yield as_role
    db.close()


def test_weekly_series_counts_and_no_data_before_logs(env):
    c = env("SUPER_ADMIN")
    r = c.get(f"{A}/kpi", params={"from": "2026-10-05", "to": "2026-10-25", "interval": "week"}).json()
    assert r["data_since"] == "2026-10-10"
    weeks = {s["start"]: s for s in r["series"]}
    # 10/5 주: 가입 3, 클럽 1, 라운딩 1(10/11). 로그 시작(10/10)이 주 중간이라 활성 회원은 "데이터 없음"
    w1 = weeks["2026-10-05"]
    assert (w1["signups"], w1["clubs"], w1["rounds"], w1["active_users"]) == (3, 1, 1, None)
    w2 = weeks["2026-10-12"]
    assert w2["signups"] == 1 and w2["active_users"] == 2 and w2["dau"] == round(2 / 7, 1)  # a(10/13 두 번=1), b(10/14)
    assert weeks["2026-10-19"]["rounds"] == 0  # 취소 라운딩 제외
    assert r["totals"]["signups"] == 4 and r["totals"]["socials"] == 1  # 게스트 제외, 10/25 소셜은 기간 끝날 포함
    assert r["totals"]["revenue"] is None and r["totals"]["dau"] is None  # 기간 시작이 로그 이전


def test_daily_after_logs_and_mau_and_previous(env):
    c = env("SUPER_ADMIN")
    r = c.get(f"{A}/kpi", params={"from": "2026-10-13", "to": "2026-10-28"}).json()
    days = {s["start"]: s for s in r["series"]}
    assert days["2026-10-13"]["active_users"] == 1 and days["2026-10-15"]["active_users"] == 0
    assert r["totals"]["mau"] == 3  # 10/13~28 안: a, b, d
    assert r["previous"]["mau"] is None  # 이전 기간(9/27~10/12)은 로그 이전이 섞임


def test_retention_cohorts(env):
    c = env("SUPER_ADMIN")
    r = c.get(f"{A}/kpi").json()
    cohorts = {x["week_start"]: x for x in r["retention"]}
    oct5 = cohorts["2026-10-05"]
    assert oct5["size"] == 3
    # 1주차(10/12~18): a, b 로그인 → 66.7%. 2주차(10/19~25): 0%. 3주차(10/26~11/1): 아직 안 지남 → null
    assert oct5["weeks"][:3] == [66.7, 0.0, None]
    assert cohorts["2026-09-28"]["weeks"][0] is None  # 1주차(10/5~11)가 로그 시작 이전


def test_validation_and_permission(env):
    c = env("SUPER_ADMIN")
    assert c.get(f"{A}/kpi", params={"from": "2026-10-10", "to": "2026-10-01"}).status_code == 422
    assert c.get(f"{A}/kpi", params={"from": "2025-01-01", "to": "2026-10-01"}).status_code == 422
    assert c.get(f"{A}/kpi", params={"interval": "year"}).status_code == 422
    assert env("CLUB_ADMIN").get(f"{A}/kpi").status_code == 403
