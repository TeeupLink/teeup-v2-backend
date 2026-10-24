"""정산·회원탈퇴 API 회귀 테스트 (서버를 띄우지 않고 in-process 로 돈다).

같은 폴더의 다른 테스트는 localhost:8002 에 서버가 떠 있어야 하지만,
이 파일은 임시 sqlite DB 와 TestClient 만으로 돌아간다.

막으려는 버그 (2026-10-24 운영 장애):
    1) ExpenseListResponse.total_pages 를 안 넘겨서 정산 목록 조회가 전부 500
    2) expenses.py 에 Decimal 임포트가 없어 비용 등록·수정이 500
    3) MessageResponse.success 를 안 넘겨서 회원 탈퇴가 500
    세 건 모두 라우터의 `except Exception` 에 삼켜져 "서버 내부 오류" 로만 보였다.
"""

import datetime as dt
import os
import tempfile

import pytest

# database 를 임포트하는 순간 엔진이 만들어지므로 그 전에 DB 를 바꿔 둔다
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="teeup-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Boolean, Date, DateTime, Enum as SAEnum, Integer, String, Text  # noqa: E402
from sqlalchemy.sql.sqltypes import Numeric  # noqa: E402

from database import Base, SessionLocal, engine  # noqa: E402
import models  # noqa: F401,E402  (모든 모델을 Base.metadata 에 등록)
from models import Club, ClubMembership, Meeting, MeetingParticipant, User  # noqa: E402
from routers.auth import get_current_active_user  # noqa: E402
from routers.meetings.expenses import router as expenses_router, rounds_expenses_router  # noqa: E402
from routers.users import router as users_router  # noqa: E402

USER_ID = 1
MEETING_ID = 29
CLUB_ID = 1


def _dummy(column):
    """NOT NULL 인데 테스트가 값을 주지 않은 컬럼을 타입에 맞는 더미로 채운다."""
    t = column.type
    if isinstance(t, SAEnum):
        return list(t.enums)[0]
    if isinstance(t, Boolean):
        return False
    if isinstance(t, (Integer, Numeric)):
        return 0
    if isinstance(t, DateTime):
        return dt.datetime(2026, 1, 1, 9, 0)
    if isinstance(t, Date):
        return dt.date(2026, 1, 1)
    if isinstance(t, (String, Text)):
        return "x"
    return None


def _row(model, **values):
    """모델 인스턴스를 만든다. 지정하지 않은 필수 컬럼은 더미로 채운다."""
    kwargs = {}
    for column in model.__table__.columns:
        if column.name in values:
            kwargs[column.name] = values[column.name]
        elif (
            not column.nullable
            and column.default is None
            and column.server_default is None
            and not column.primary_key
        ):
            kwargs[column.name] = _dummy(column)
    return model(**kwargs)


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    db.add(_row(User, id=USER_ID, realname="테스터", nickname="테스터", email="t@example.com", status="ACTIVE"))
    db.add(_row(Club, id=CLUB_ID, name="테스트클럽"))
    db.flush()
    db.add(_row(ClubMembership, id=1, club_id=CLUB_ID, user_id=USER_ID, role="LEADER", status="ACTIVE"))
    db.add(_row(Meeting, id=MEETING_ID, name="테스트라운딩", club_id=CLUB_ID,
                meeting_type="ROUND", created_by=USER_ID))
    db.flush()
    db.add(_row(MeetingParticipant, id=1, meeting_id=MEETING_ID, user_id=USER_ID, status="CONFIRMED"))
    db.commit()
    db.close()

    app = FastAPI()
    app.include_router(expenses_router, prefix="/api/v1")
    app.include_router(rounds_expenses_router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")

    # 운영과 같은 모양으로 넘긴다: get_current_active_user 는 dict 가 아니라
    # User ORM 객체를 돌려준다. dict 로 넘기면 .get() 을 쓰는 버그를 가리게 된다.
    def _current_user():
        session = SessionLocal()
        try:
            return session.query(User).filter(User.id == USER_ID).first()
        finally:
            session.close()

    app.dependency_overrides[get_current_active_user] = _current_user
    return TestClient(app)


def test_정산목록_비용이_없어도_200(client):
    """total_pages 누락 버그: 비용이 0건이어도 500 이 났다."""
    res = client.get(f"/api/v1/meetings/{MEETING_ID}/expenses")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["expenses"] == []
    assert body["total"] == 0
    assert body["total_pages"] == 0


def test_비용등록과_수정(client):
    """Decimal 임포트 누락 버그: 등록(POST)·수정(PUT) 이 NameError 로 500 이 났다."""
    res = client.post(
        f"/api/v1/meetings/{MEETING_ID}/expenses",
        json={"title": "그린피", "amount": 100000, "total_participants": 1, "participant_ids": [1]},
    )
    assert res.status_code == 200, res.text
    expense_id = res.json()["id"]
    assert res.json()["amount"] == 100000.0

    res = client.put(
        f"/api/v1/meetings/{MEETING_ID}/expenses/{expense_id}",
        json={"amount": 50000},
    )
    assert res.status_code == 200, res.text
    assert res.json()["amount"] == 50000.0


def test_정산목록_페이지수_계산(client):
    """total_pages 가 limit 에 맞게 계산되는지."""
    res = client.get(f"/api/v1/meetings/{MEETING_ID}/expenses", params={"limit": 10})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 1
    assert body["total_pages"] == 1


def test_앱경로_rounds_expenses(client):
    """앱(1.0, 1.0.1)은 /rounds/{id}/expenses 를 호출한다.

    이 경로가 서버에 없어서 정산 화면이 404 를 받고 있었다.
    이미 배포된 앱을 바꿀 수 없으므로 같은 핸들러를 /rounds 에도 붙였다.
    """
    res = client.get(f"/api/v1/rounds/{MEETING_ID}/expenses")
    assert res.status_code == 200, res.text
    body = res.json()
    assert "total_pages" in body

    # /meetings 경로와 같은 결과여야 한다
    other = client.get(f"/api/v1/meetings/{MEETING_ID}/expenses")
    assert other.status_code == 200, other.text
    assert body == other.json()


def test_앱경로_rounds_expenses_등록과_수정(client):
    """/rounds 경로로도 등록·수정이 되는지."""
    res = client.post(
        f"/api/v1/rounds/{MEETING_ID}/expenses",
        json={"title": "카트비", "amount": 80000, "total_participants": 1, "participant_ids": [1]},
    )
    assert res.status_code == 200, res.text
    expense_id = res.json()["id"]

    res = client.put(
        f"/api/v1/rounds/{MEETING_ID}/expenses/{expense_id}",
        json={"amount": 60000},
    )
    assert res.status_code == 200, res.text
    assert res.json()["amount"] == 60000.0

    res = client.delete(f"/api/v1/rounds/{MEETING_ID}/expenses/{expense_id}")
    assert res.status_code == 200, res.text


def test_회원탈퇴_200(client):
    """MessageResponse.success 누락 버그: 회원 탈퇴가 500 이 났다.

    App Store 심사 요구사항(계정 삭제)이라 반드시 동작해야 한다.
    다른 테스트가 이 사용자를 쓰므로 맨 마지막에 둔다.
    """
    res = client.delete("/api/v1/users/account")
    assert res.status_code == 200, res.text
    assert res.json()["success"] is True

    db = SessionLocal()
    user = db.query(User).filter(User.id == USER_ID).first()
    assert user.deleted_at is not None
    assert user.status.value == "DELETED"
    db.close()
