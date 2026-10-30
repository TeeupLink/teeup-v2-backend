"""클럽별 회원 메모(F-034)·약관 동의 이력(F-111). SQLite 메모리 DB."""

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
from models import AdminAuditLog, Club, ClubMembership, ClubStatus, Terms, TermsAgreement, User, UserStatus  # noqa: E402
from models.enums import TermsType  # noqa: E402
from models.region import Sido  # noqa: E402
from routers import admin as admin_pkg  # noqa: E402
from routers.admin.deps import get_admin_user  # noqa: E402
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
    u1, u2 = (User(email=f"u{i}@t.run", nickname=n, realname=f"실명{i}", status=UserStatus.ACTIVE) for i, n in enumerate(["골퍼", "새싹"]))
    club = Club(name="강남 골프회", sido_code="11", status=ClubStatus.ACTIVE)
    db.add_all([u1, u2, club])
    db.flush()
    db.add(ClubMembership(club_id=club.id, user_id=u1.id, note="회비 3월분 미납"))
    old = Terms(type=TermsType.SERVICE, title="이용약관 v1", content="a", is_active=False, is_required=True,
                published_at=datetime(2026, 1, 1), created_at=datetime(2026, 1, 1))
    cur = Terms(type=TermsType.SERVICE, title="이용약관 v2", content="b", is_active=True, is_required=True,
                published_at=datetime(2026, 9, 1), created_at=datetime(2026, 9, 1))
    priv = Terms(type=TermsType.PRIVACY, title="개인정보처리방침", content="c", is_active=True, is_required=True,
                 published_at=datetime(2026, 1, 1), created_at=datetime(2026, 1, 1))
    mkt = Terms(type=TermsType.MARKETING, title="마케팅", content="d", is_active=True, is_required=False,
                published_at=datetime(2026, 1, 1), created_at=datetime(2026, 1, 1))
    db.add_all([old, cur, priv, mkt])
    db.flush()
    db.add_all([
        TermsAgreement(user_id=u1.id, terms_id=old.id, agreed_at=datetime(2026, 2, 1), ip_address="1.2.3.4"),
        TermsAgreement(user_id=u1.id, terms_id=priv.id, agreed_at=datetime(2026, 2, 1)),
        TermsAgreement(user_id=u2.id, terms_id=old.id, agreed_at=datetime(2026, 3, 1)),
    ])
    db.commit()

    who = {"role": "USER_ADMIN"}
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
        me = {"id": 1, "email": "ops@teeup.run", "name": "운영", "role": who["role"], "status": "ACTIVE", "type": "admin"}
        audit.set_actor(me)
        return me

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_admin_user] = _me

    def as_role(role):
        who["role"] = role
        return TestClient(app)

    yield db, (u1, u2, club, old, cur, priv, mkt), as_role
    db.close()


def test_user_clubs_include_note_and_note_is_audited(env):
    db, (u1, u2, club, *_), as_role = env
    c = as_role("USER_ADMIN")
    (row,) = c.get(f"{A}/users/{u1.id}/clubs").json()["clubs"]
    assert row["note"] == "회비 3월분 미납"

    r = c.put(f"{A}/users/{u1.id}/clubs/{club.id}/note", json={"note": "  회비 완납  "})
    assert r.status_code == 200 and r.json()["note"] == "회비 완납"
    db.expire_all()
    assert db.query(ClubMembership).one().note == "회비 완납"
    (log,) = db.query(AdminAuditLog).filter_by(action="UPDATE", target_type="CLUB_MEMBER").all()
    assert log.before_json == {"note": "회비 3월분 미납"} and log.after_json == {"note": "회비 완납"}

    # 빈 값은 지우기
    assert c.put(f"{A}/users/{u1.id}/clubs/{club.id}/note", json={"note": " "}).json()["note"] is None
    # 클럽 회원이 아니면 404, 너무 길면 422
    assert c.put(f"{A}/users/{u2.id}/clubs/{club.id}/note", json={"note": "x"}).status_code == 404
    assert c.put(f"{A}/users/{u1.id}/clubs/{club.id}/note", json={"note": "x" * 2001}).status_code == 422


def test_support_admin_cannot_edit_note(env):
    _, (u1, _, club, *_), as_role = env
    assert as_role("SUPPORT_ADMIN").put(f"{A}/users/{u1.id}/clubs/{club.id}/note", json={"note": "x"}).status_code == 403


def test_user_terms_agreements_with_missing_required(env):
    _, (u1, _, _, old, cur, priv, mkt), as_role = env
    body = as_role("SUPPORT_ADMIN").get(f"{A}/users/{u1.id}/terms-agreements").json()
    by_terms = {i["terms_id"]: i for i in body["items"]}
    assert set(by_terms) == {old.id, priv.id}
    assert by_terms[old.id]["version"] == 1 and by_terms[old.id]["status"] == "RETIRED"
    assert by_terms[old.id]["ip_address"] == "1.2.3.4"
    # 예전 버전에만 동의했으니 게시중 필수 이용약관 v2 는 미동의. 선택 약관(마케팅)은 빠진다
    assert body["missing_required"] == [{"id": cur.id, "type": "SERVICE", "version": 2, "title": "이용약관 v2"}]
    assert as_role("SUPPORT_ADMIN").get(f"{A}/users/9999/terms-agreements").status_code == 404


def test_version_agreements_paged_and_searchable(env):
    _, (u1, u2, _, old, *_), as_role = env
    c = as_role("CONTENT_ADMIN")
    body = c.get(f"{A}/terms/versions/{old.id}/agreements").json()
    assert body["total"] == 2 and [i["nickname"] for i in body["items"]] == ["새싹", "골퍼"]
    assert [i["user_id"] for i in c.get(f"{A}/terms/versions/{old.id}/agreements", params={"q": "골퍼"}).json()["items"]] == [u1.id]
    assert c.get(f"{A}/terms/versions/{old.id}/agreements", params={"sort": "ip_address"}).status_code == 422
    assert c.get(f"{A}/terms/versions/9999/agreements").status_code == 404
    assert as_role("USER_ADMIN").get(f"{A}/terms/versions/{old.id}/agreements").status_code == 403
