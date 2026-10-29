"""백오피스 공지·FAQ 보강 (backoffice #26).

SQLite 메모리 DB. 회원 알림 전송은 가짜로 바꿔 몇 명에게 보냈는지만 센다.
"""

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
from models import FAQ, Admin, AdminAuditLog, Club, ClubStatus, FAQCategory, Notice, User, UserStatus  # noqa: E402
from models.enums import NoticeType  # noqa: E402
from models.region import Sido  # noqa: E402
from routers import admin as admin_pkg  # noqa: E402
from routers.admin.deps import get_admin_user  # noqa: E402
from utils import activity, notification_service  # noqa: E402
from utils.audit import AuditMiddleware  # noqa: E402

A = "/api/v1/admin"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(activity, "session_factory", Session)
    sent = []
    monkeypatch.setattr(notification_service, "create_notice_notification", lambda **kw: sent.append(kw))
    db = Session()
    db.add(Sido(code="11", name="서울"))
    db.add(Admin(id=1, email="content@teeup.run", password="x", name="콘텐츠팀"))
    club = Club(name="강남 골프회", sido_code="11", status=ClubStatus.ACTIVE)
    db.add(club)
    for i in range(3):
        db.add(User(email=f"u{i}@t.run", nickname=f"u{i}", status=UserStatus.ACTIVE if i < 2 else UserStatus.DEACTIVATED))
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

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_admin_user] = lambda: {"id": 1, "email": "content@teeup.run", "name": "콘텐츠팀", "role": who["role"],
                                                        "status": "ACTIVE", "type": "admin"}

    def as_role(role):
        who["role"] = role
        return TestClient(app)

    yield db, club, sent, as_role
    db.close()


def audits(db, **kw):
    db.expire_all()
    q = db.query(AdminAuditLog)
    for k, v in kw.items():
        q = q.filter(getattr(AdminAuditLog, k) == v)
    return q.order_by(AdminAuditLog.id).all()


def notice(db, title, club=None, important=False, published=True):
    n = Notice(title=title, content="<p>본문</p>", type=NoticeType.GENERAL if club else NoticeType.SYSTEM, club_id=club.id if club else None,
               author_id=1, is_important=important, is_published=published)
    db.add(n)
    db.commit()
    return n


def test_notice_list_excludes_club_notices_and_filters(env):
    db, club, _, as_role = env
    notice(db, "서버 점검 안내")
    notice(db, "개인정보 처리방침 변경", important=True)
    notice(db, "임시 저장", published=False)
    club_notice = notice(db, "클럽 정기 라운딩", club=club)
    client = as_role("CONTENT_ADMIN")

    r = client.get(f"{A}/notices").json()
    assert r["total"] == 3
    assert [n["title"] for n in r["data"]][0] == "개인정보 처리방침 변경"  # 중요 공지 먼저
    assert r["data"][0]["author_name"] == "콘텐츠팀"
    assert client.get(f"{A}/notices", params={"is_published": False}).json()["total"] == 1
    assert client.get(f"{A}/notices", params={"q": "점검"}).json()["data"][0]["title"] == "서버 점검 안내"
    # 클럽 공지는 이 API 로 열거나 지울 수 없다
    assert client.get(f"{A}/notices/{club_notice.id}").status_code == 404
    assert client.delete(f"{A}/notices/{club_notice.id}").status_code == 404


def test_notice_draft_does_not_notify_until_published(env):
    db, _, sent, as_role = env
    client = as_role("CONTENT_ADMIN")

    r = client.post(f"{A}/notices", json={"title": " ", "content": "<p></p>"})
    assert r.status_code == 422 and {e["field"] for e in r.json()["detail"]} == {"title", "content"}

    nid = client.post(f"{A}/notices", json={"title": "앱 업데이트", "content": "<p>1.0.3</p>", "is_published": False}).json()["id"]
    assert sent == []  # 예전에는 임시 저장에도 전체 회원에게 알림이 갔다

    client.put(f"{A}/notices/{nid}", json={"is_published": True, "title": "앱 업데이트 안내"})
    assert len(sent) == 2 and sent[0]["notice_id"] == nid  # 활성 회원 2명
    client.put(f"{A}/notices/{nid}", json={"is_important": True})
    assert len(sent) == 2  # 이미 게시된 공지 수정은 다시 알리지 않는다

    upd = audits(db, action="UPDATE", target_type="NOTICE")[0]
    assert upd.before_json == {"is_published": False, "title": "앱 업데이트"} and upd.after_json["title"] == "앱 업데이트 안내"

    assert client.delete(f"{A}/notices/{nid}", params={"reason": "중복"}).status_code == 200
    d = audits(db, action="DELETE", target_type="NOTICE")[0]
    assert d.before_json["title"] == "앱 업데이트 안내" and d.reason == "중복"


def test_notice_write_needs_content_role(env):
    _, _, _, as_role = env
    assert as_role("CLUB_ADMIN").get(f"{A}/notices").status_code == 403


def test_faq_filters_reorder_counts_and_uncategorize(env):
    db, _, _, as_role = env
    cat = FAQCategory(title="계정", order=1)
    other = FAQCategory(title="결제", order=2)
    db.add_all([cat, other])
    db.flush()
    f1, f2, f3 = (FAQ(question=f"질문{i}", answer="답", category_id=cat.id, order=i, is_active=i != 3) for i in (1, 2, 3))
    f4 = FAQ(question="결제 질문", answer="답", category_id=other.id, order=1)
    db.add_all([f1, f2, f3, f4])
    db.commit()
    client = as_role("CONTENT_ADMIN")

    cats = {c["title"]: c["faq_count"] for c in client.get(f"{A}/faq-categories").json()}
    assert cats == {"계정": 3, "결제": 1}
    assert client.get(f"{A}/faq", params={"is_active": False}).json()["total"] == 1

    assert client.put(f"{A}/faq-order", json={"ids": [f3.id, f1.id, f2.id]}).status_code == 200
    r = client.get(f"{A}/faq", params={"category_id": cat.id}).json()
    assert [i["question"] for i in r["items"]] == ["질문3", "질문1", "질문2"]
    assert client.put(f"{A}/faq-order", json={"ids": [f1.id, f4.id]}).status_code == 422
    assert audits(db, target_type="FAQ_ORDER")[0].after_json["order"] == {str(f3.id): 1, str(f1.id): 2, str(f2.id): 3}

    # null 이면 미분류로 옮긴다
    assert client.put(f"{A}/faq/{f4.id}", json={"category_id": None}).status_code == 200
    assert [i["question"] for i in client.get(f"{A}/faq", params={"category_id": "none"}).json()["items"]] == ["결제 질문"]
