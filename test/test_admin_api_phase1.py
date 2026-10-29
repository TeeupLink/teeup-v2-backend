"""백오피스 API 1단계 (backend #46) — 권한 표, 선택용 조회, 가입신청, 문의 페이징, 대시보드 할 일, 일괄 처리.

서버·MySQL 없이 SQLite 메모리 DB 로 실제 쿼리까지 돈다.
인증은 get_admin_user 를 역할별 가짜 관리자로 바꿔 끼운다 (JWT 검증은 이 테스트 대상이 아님).
"""

import os
from datetime import timedelta

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("BACKEND_PORT", "7777")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import models  # noqa: E402,F401  (모든 테이블 등록)
from database import Base, get_db  # noqa: E402
from models import (  # noqa: E402
    Club,
    ClubMembership,
    ClubRole,
    ClubStatus,
    Inquiry,
    InquiryStatus,
    InquiryType,
    Meeting,
    MeetingType,
    MembershipStatus,
    User,
    UserStatus,
)
from models.region import Sido  # noqa: E402
from routers import admin as admin_pkg  # noqa: E402
from routers.admin.deps import get_admin_user  # noqa: E402
from utils.datetime_utils import get_kst_now  # noqa: E402


@pytest.fixture
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Sido(code="11", name="서울"))
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
    app.dependency_overrides[get_admin_user] = lambda: {"id": 1, "email": "a@teeup.run", "name": "운영", "role": who["role"], "status": "ACTIVE"}

    def as_role(role):
        who["role"] = role
        return client

    client = TestClient(app)
    yield db, as_role
    db.close()


def user(db, i, **kw):
    u = User(email=f"user{i}@gmail.com", nickname=f"golfer{i}", realname=kw.pop("realname", "김민수"), status=UserStatus.ACTIVE, average_score=90, **kw)
    db.add(u)
    db.flush()
    return u


def club(db, name="강남 골프회", status=ClubStatus.ACTIVE):
    c = Club(name=name, sido_code="11", status=status, member_count=1)
    db.add(c)
    db.flush()
    return c


def pending(db, c, u):
    m = ClubMembership(club_id=c.id, user_id=u.id, role=ClubRole.MEMBER, status=MembershipStatus.PENDING)
    db.add(m)
    db.flush()
    return m


A = "/api/v1/admin"


# ---------------------------------------------------------------- 권한 표


def test_me_permissions_follow_matrix(env):
    _, as_role = env
    perms = as_role("SUPPORT_ADMIN").get(f"{A}/me").json()["permissions"]
    assert perms["users"] == "read"
    assert perms["payments"] == "read"
    assert perms["inquiries"] == "write"
    assert perms["admins"] is None


def test_role_null_has_no_permission(env):
    _, as_role = env
    c = as_role(None)
    body = c.get(f"{A}/me").json()
    assert body["role"] is None
    assert all(v is None for v in body["permissions"].values())
    assert c.get(f"{A}/join-requests").status_code == 403


def test_support_can_read_users_but_not_write(env):
    db, as_role = env
    user(db, 1)
    db.commit()
    c = as_role("SUPPORT_ADMIN")
    assert c.get(f"{A}/users").status_code == 200
    assert c.post(f"{A}/users/bulk", json={"ids": [1], "action": "deactivate"}).status_code == 403


def test_meeting_admin_reads_clubs_but_cannot_write(env):
    _, as_role = env
    c = as_role("MEETING_ADMIN")
    assert c.get(f"{A}/lookup/clubs").status_code == 200
    assert c.post(f"{A}/join-requests/bulk", json={"ids": [1], "action": "approve"}).status_code == 403


# ---------------------------------------------------------------- lookup


def test_lookup_users_masks_and_requires_two_chars(env):
    db, as_role = env
    user(db, 1, realname="김민수")
    db.commit()
    c = as_role("CLUB_ADMIN")
    assert c.get(f"{A}/lookup/users", params={"q": "g"}).json()["items"] == []
    items = c.get(f"{A}/lookup/users", params={"q": "golf"}).json()["items"]
    assert items == [{"id": 1, "nickname": "golfer1", "name_masked": "김*수", "email_masked": "us***@gmail.com", "average_score": 90, "status": "ACTIVE"}]


def test_lookup_clubs_excludes_inactive_by_default(env):
    db, as_role = env
    club(db, "활성 클럽")
    club(db, "닫힌 클럽", ClubStatus.INACTIVE)
    db.commit()
    names = [c["name"] for c in as_role("MEETING_ADMIN").get(f"{A}/lookup/clubs").json()["items"]]
    assert names == ["활성 클럽"]


# ---------------------------------------------------------------- 가입신청


def test_join_requests_list_and_bulk(env):
    db, as_role = env
    c1 = club(db, "A클럽")
    c2 = club(db, "B클럽")
    m1 = pending(db, c1, user(db, 1))
    m2 = pending(db, c2, user(db, 2))
    m3 = pending(db, c1, user(db, 3))
    db.commit()
    id1, id2, id3, club1 = m1.id, m2.id, m3.id, c1.id
    c = as_role("CLUB_ADMIN")

    page = c.get(f"{A}/join-requests").json()
    assert page["total"] == 3 and page["page"] == 1 and page["size"] == 20
    assert {i["club"]["name"] for i in page["items"]} == {"A클럽", "B클럽"}
    assert c.get(f"{A}/join-requests", params={"club_id": c2.id}).json()["total"] == 1

    r = c.post(f"{A}/join-requests/bulk", json={"ids": [id1, id2], "action": "approve"}).json()
    assert r == {"succeeded": [id1, id2], "failed": []}
    db.expire_all()
    assert db.get(ClubMembership, id1).status == MembershipStatus.ACTIVE
    assert db.get(Club, club1).member_count == 2

    # 이미 처리된 건은 실패 목록으로, 나머지는 처리
    r = c.post(f"{A}/join-requests/bulk", json={"ids": [id1, id3], "action": "reject"}).json()
    assert r["succeeded"] == [id3]
    assert r["failed"] == [{"id": id1, "message": "이미 처리된 신청입니다"}]
    db.expire_all()
    assert db.get(ClubMembership, id3) is None
    assert c.get(f"{A}/join-requests").json()["total"] == 0


def test_join_request_approve_fails_for_inactive_club(env):
    db, as_role = env
    m = pending(db, club(db, status=ClubStatus.INACTIVE), user(db, 1))
    db.commit()
    r = as_role("CLUB_ADMIN").post(f"{A}/join-requests/bulk", json={"ids": [m.id], "action": "approve"}).json()
    assert r["failed"][0]["message"] == "비활성 클럽에는 가입을 승인할 수 없습니다"


def test_list_rejects_unknown_sort_and_size(env):
    _, as_role = env
    c = as_role("SUPER_ADMIN")
    assert c.get(f"{A}/join-requests", params={"sort": "password"}).status_code == 422
    assert c.get(f"{A}/join-requests", params={"size": 33}).status_code == 422


# ---------------------------------------------------------------- 문의


def test_inquiries_paging_beyond_20_and_status_filter(env):
    db, as_role = env
    u = user(db, 1)
    now = get_kst_now()
    for i in range(25):
        st = InquiryStatus.PENDING if i % 2 == 0 else InquiryStatus.COMPLETED
        db.add(Inquiry(user_id=u.id, title=f"문의{i}", content="내용", type=InquiryType.GENERAL, status=st, created_at=now - timedelta(minutes=i)))
    db.add(Inquiry(user_id=u.id, title="제출됨", content="내용", type=InquiryType.ACCOUNT, status=InquiryStatus.SUBMITTED, created_at=now - timedelta(hours=5)))
    db.commit()
    c = as_role("SUPPORT_ADMIN")

    p2 = c.get(f"{A}/inquiries", params={"page": 2, "size": 20}).json()
    assert p2["total"] == 26 and len(p2["items"]) == 6  # 예전에는 20건에서 끝났다
    unanswered = c.get(f"{A}/inquiries", params=[("status", "PENDING"), ("status", "SUBMITTED")]).json()
    assert unanswered["total"] == 14
    assert c.get(f"{A}/inquiries", params={"q": "제출"}).json()["items"][0]["type"] == "ACCOUNT"
    assert c.get(f"{A}/inquiries", params={"status": "NOPE"}).status_code == 422


def test_inquiries_bulk_status(env):
    db, as_role = env
    u = user(db, 1)
    q1 = Inquiry(user_id=u.id, title="a", content="b", type=InquiryType.GENERAL, status=InquiryStatus.PENDING)
    db.add(q1)
    db.commit()
    r = as_role("SUPPORT_ADMIN").post(f"{A}/inquiries/bulk", json={"ids": [q1.id, 999], "action": "status", "status": "IN_PROGRESS"}).json()
    assert r["succeeded"] == [q1.id]
    assert r["failed"] == [{"id": 999, "message": "문의를 찾을 수 없습니다"}]


# ---------------------------------------------------------------- 대시보드 할 일


def test_dashboard_todo_counts_and_role_scope(env):
    db, as_role = env
    u = user(db, 1)
    pending(db, club(db), user(db, 2))
    db.add(Inquiry(user_id=u.id, title="a", content="b", type=InquiryType.GENERAL, status=InquiryStatus.SUBMITTED))
    c1 = club(db, "라운딩 클럽")
    today = get_kst_now().replace(hour=9, minute=0, second=0, microsecond=0)
    db.add(Meeting(name="오늘 라운딩", meeting_type=MeetingType.ROUND, meeting_time=today, club_id=c1.id, status="SCHEDULED"))
    db.add(Meeting(name="취소됨", meeting_type=MeetingType.ROUND, meeting_time=today, club_id=c1.id, status="CANCELED"))
    db.add(Meeting(name="내일", meeting_type=MeetingType.ROUND, meeting_time=today + timedelta(days=1), club_id=c1.id, status="SCHEDULED"))
    db.commit()

    assert as_role("SUPER_ADMIN").get(f"{A}/dashboard/todo").json() == {
        "join_requests": 1,
        "unanswered_inquiries": 1,
        "today_rounds": 1,
        "pending_reports": 0,
    }
    assert as_role("SUPPORT_ADMIN").get(f"{A}/dashboard/todo").json() == {
        "join_requests": None,
        "unanswered_inquiries": 1,
        "today_rounds": None,
        "pending_reports": 0,
    }


# ---------------------------------------------------------------- 회원 일괄


def test_users_bulk_deactivate(env):
    db, as_role = env
    u1, u2 = user(db, 1), user(db, 2)
    u2.status = UserStatus.DELETED
    db.commit()
    r = as_role("USER_ADMIN").post(f"{A}/users/bulk", json={"ids": [u1.id, u2.id], "action": "deactivate"}).json()
    assert r["succeeded"] == [u1.id]
    assert r["failed"][0]["message"] == "탈퇴한 회원은 상태를 바꿀 수 없습니다"
    db.expire_all()
    assert db.get(User, u1.id).status == UserStatus.DEACTIVATED
    assert db.get(User, u1.id).deactivated_at is not None


# ---------------------------------------------------------------- 문의 답변·내부 메모


def test_internal_memo_is_not_notified_and_hidden_from_app(env, monkeypatch):
    db, as_role = env
    u = user(db, 1)
    q = Inquiry(user_id=u.id, title="로그인 안 돼요", content="b", type=InquiryType.ACCOUNT, status=InquiryStatus.PENDING)
    db.add(q)
    from models import Admin, AdminRole

    db.add(Admin(id=1, email="a@teeup.run", password="x", name="김운영", role=AdminRole.SUPER_ADMIN))
    db.commit()
    qid, u_id = q.id, u.id

    sent = []
    import utils.notification_service as ns

    monkeypatch.setattr(ns, "create_inquiry_response_notification", lambda **kw: sent.append(kw))
    c = as_role("SUPPORT_ADMIN")

    r = c.post(f"{A}/inquiries/{qid}/responses", json={"content": "비번 재설정 이력 있음", "is_internal": True})
    assert r.status_code == 200
    assert sent == []  # 내부 메모는 회원에게 알리지 않는다

    r = c.post(f"{A}/inquiries/{qid}/responses", json={"content": "재설정 링크 보내드렸습니다", "set_status": "COMPLETED"})
    assert r.json()["status"] == "COMPLETED"
    assert [s["response_content"] for s in sent] == ["재설정 링크 보내드렸습니다"]

    detail = c.get(f"{A}/inquiries/{qid}").json()
    assert [(x["is_internal"], x["admin_name"]) for x in detail["responses"]] == [(True, "김운영"), (False, "김운영")]
    assert detail["inquiry"]["status"] == "COMPLETED"

    assert c.post(f"{A}/inquiries/{qid}/responses", json={"content": "  "}).status_code == 422

    # 앱 API(회원 본인 조회)에는 내부 메모가 내려가지 않는다
    from types import SimpleNamespace

    from routers import inquiries as app_inquiries
    from routers.auth import get_current_user

    c.app.include_router(app_inquiries.router, prefix="/api/v1")
    c.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=u_id)
    app_view = c.get(f"/api/v1/inquiries/{qid}").json()
    assert [x["content"] for x in app_view["responses"]] == ["재설정 링크 보내드렸습니다"]
    assert app_view["total_responses"] == 1
