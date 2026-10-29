"""백오피스 약관 버전 관리 (backoffice #30). SQLite 메모리 DB."""

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
from models import Admin, AdminAuditLog, Terms, TermsAgreement, User, UserStatus  # noqa: E402
from routers import admin as admin_pkg  # noqa: E402
from routers.admin.deps import get_admin_user  # noqa: E402
from schemas import TermsType  # noqa: E402
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
    db.add(Admin(id=1, email="content@teeup.run", password="x", name="콘텐츠팀"))
    v1 = Terms(type=TermsType.SERVICE, title="서비스 이용약관", content="<p>v1</p>", is_active=True, is_required=True,
               published_at=datetime(2026, 1, 1), created_at=datetime(2026, 1, 1), created_by=1)
    db.add(v1)
    db.flush()
    for i in range(3):
        u = User(email=f"u{i}@t.run", nickname=f"u{i}", status=UserStatus.ACTIVE)
        db.add(u)
        db.flush()
        db.add(TermsAgreement(user_id=u.id, terms_id=v1.id, agreed_at=datetime(2026, 2, 1)))
    db.commit()

    who = {"role": "CONTENT_ADMIN"}
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
        me = {"id": 1, "email": "content@teeup.run", "name": "콘텐츠팀", "role": who["role"], "status": "ACTIVE", "type": "admin"}
        audit.set_actor(me)
        return me

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_admin_user] = _me

    def as_role(role):
        who["role"] = role
        return TestClient(app)

    yield db, v1, as_role
    db.close()


def test_summary_and_versions_with_agreement_counts(env):
    db, v1, as_role = env
    c = as_role("CONTENT_ADMIN")
    items = {i["type"]: i for i in c.get(f"{A}/terms/versions").json()["items"]}
    assert items["SERVICE"]["active"]["agreement_count"] == 3 and items["SERVICE"]["active"]["version"] == 1
    assert items["MARKETING"]["active"] is None and items["MARKETING"]["default_title"] == "마케팅정보 수신동의"
    assert c.get(f"{A}/terms/versions", params={"type": "NOPE"}).status_code == 422
    assert as_role("SUPPORT_ADMIN").get(f"{A}/terms/versions").status_code == 403


def test_draft_edit_publish_keeps_old_version_text(env):
    db, v1, as_role = env
    c = as_role("CONTENT_ADMIN")
    assert c.post(f"{A}/terms/versions", json={"type": "SERVICE", "title": " ", "content": "<p></p>"}).status_code == 422
    d = c.post(f"{A}/terms/versions", json={"type": "SERVICE", "title": "서비스 이용약관", "content": "<p>v2 초안</p>", "is_required": True}).json()
    assert d["status"] == "DRAFT" and d["version"] == 2 and d["created_by_name"] == "콘텐츠팀"

    assert c.put(f"{A}/terms/versions/{d['id']}", json={"content": "<p>v2</p>"}).json()["content"] == "<p>v2</p>"
    # 게시된 버전은 고치거나 지울 수 없다
    assert c.put(f"{A}/terms/versions/{v1.id}", json={"content": "<p>몰래 수정</p>"}).status_code == 409
    assert c.delete(f"{A}/terms/versions/{v1.id}").status_code == 409

    p = c.post(f"{A}/terms/versions/{d['id']}/publish").json()
    assert p["status"] == "ACTIVE"
    db.expire_all()
    old = db.get(Terms, v1.id)
    assert old.is_active is False and old.content == "<p>v1</p>"  # 동의 기록이 가리키는 본문은 그대로
    versions = c.get(f"{A}/terms/versions", params={"type": "SERVICE"}).json()["items"]
    assert [(v["version"], v["status"]) for v in versions] == [(2, "ACTIVE"), (1, "RETIRED")]
    assert c.post(f"{A}/terms/versions/{d['id']}/publish").status_code == 409

    # 되돌리기: 지난 버전을 다시 게시
    assert c.post(f"{A}/terms/versions/{v1.id}/publish").json()["status"] == "ACTIVE"
    rows = [r for r in db.query(AdminAuditLog).filter(AdminAuditLog.target_type == "TERMS", AdminAuditLog.action == "STATUS_CHANGE").order_by(AdminAuditLog.id).all()]
    assert rows[-1].before_json == {"status": "RETIRED", "replaces": d["id"]}


def test_delete_draft_only(env):
    db, v1, as_role = env
    c = as_role("CONTENT_ADMIN")
    d = c.post(f"{A}/terms/versions", json={"type": "MARKETING", "title": "마케팅", "content": "<p>m</p>"}).json()
    assert c.delete(f"{A}/terms/versions/{d['id']}").status_code == 200
    assert db.query(Terms).filter(Terms.type == TermsType.MARKETING).count() == 0


def test_legacy_type_endpoints_no_longer_overwrite_or_create_on_read(env):
    db, v1, as_role = env
    c = as_role("CONTENT_ADMIN")
    # 조회만으로 행을 만들지 않는다
    r = c.get(f"{A}/terms/marketing").json()
    assert r["title"] == "마케팅정보 수신동의" and db.query(Terms).filter(Terms.type == TermsType.MARKETING).count() == 0

    assert c.put(f"{A}/terms/service", json={"title": "서비스 이용약관", "content": "<p>v1</p>"}).json()["message"] == "바뀐 내용이 없습니다"
    r = c.put(f"{A}/terms/service", json={"title": "서비스 이용약관", "content": "<p>개정</p>"}).json()
    db.expire_all()
    assert db.get(Terms, v1.id).content == "<p>v1</p>" and db.get(Terms, v1.id).is_active is False  # 예전에는 이 본문을 덮어썼다
    new = db.get(Terms, r["id"])
    assert new.is_active and new.content == "<p>개정</p>" and new.is_required is True
    assert c.get(f"{A}/terms/service").json()["content"] == "<p>개정</p>"
