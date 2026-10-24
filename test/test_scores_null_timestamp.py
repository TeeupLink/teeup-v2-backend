"""타임스탬프가 NULL 인 행을 읽어도 500 이 나지 않는지 확인한다.

막으려는 버그 (2026-10-24 운영 장애):
    scores 테이블에 created_at / updated_at 이 NULL 인 행이 있었다.
    모델의 타임스탬프 컬럼은 nullable 이고 server_default 가 없어서,
    ORM 을 거치지 않고 들어온 행(덤프 복원 등)은 NULL 이 된다.
    그런데 ScoreResponse 는 created_at: datetime 을 필수로 받아서,
    그 행을 읽는 순간 ValidationError -> 500 이 났다.
    앱의 점수 입력 화면이 통째로 열리지 않았다.

    같은 모양이 응답 스키마 44곳에 있었고 전부 Optional 로 바꿨다.
    여기서는 실제로 터졌던 ScoreResponse 경로를 지킨다.

이 파일은 서버를 띄우지 않고 임시 sqlite DB 와 TestClient 로만 돈다.
"""

import datetime as dt
import os
import tempfile

import pytest

# database 를 임포트하는 순간 엔진이 만들어지므로 그 전에 DB 를 바꿔 둔다
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="teeup-score-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy import (  # noqa: E402
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    Integer,
    String,
    Text,
)
from sqlalchemy.sql.sqltypes import Numeric  # noqa: E402

from database import Base, SessionLocal, engine  # noqa: E402
import models  # noqa: F401,E402  (모든 모델을 Base.metadata 에 등록)
from models import Club, ClubMembership, Meeting, MeetingParticipant, User  # noqa: E402
from models.meeting import Score  # noqa: E402
from routers.auth import get_current_active_user  # noqa: E402
from routers.meetings.scores import meeting_score_router, round_score_router  # noqa: E402

# 같은 폴더의 다른 in-process 테스트와 한 프로세스에서 돌 수 있다.
# 먼저 임포트된 쪽이 정한 DATABASE_URL 하나를 공유하므로, 시드가 겹치지
# 않게 이 파일만의 ID 대역을 쓴다.
USER_ID = 901
CLUB_ID = 901
MEETING_ID = 929
PARTICIPANT_ID = 901
SCORE_NULL_ID = 9001
SCORE_OK_ID = 9002


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
        # 유니크 컬럼이 다른 테스트 파일의 시드와 겹치지 않게 한다
        return f"sc-{column.table.name}-{column.name}"
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
    db.add(_row(User, id=USER_ID, realname="스코어테스터", nickname="스코어테스터",
                email="score-test@example.com", status="ACTIVE"))
    db.add(_row(Club, id=CLUB_ID, name="스코어테스트클럽"))
    db.flush()
    db.add(_row(ClubMembership, id=901, club_id=CLUB_ID, user_id=USER_ID,
                role="LEADER", status="ACTIVE"))
    db.add(_row(Meeting, id=MEETING_ID, name="스코어테스트라운딩", club_id=CLUB_ID,
                meeting_type="ROUND", created_by=USER_ID))
    db.flush()
    db.add(_row(MeetingParticipant, id=PARTICIPANT_ID, meeting_id=MEETING_ID,
                user_id=USER_ID, status="CONFIRMED"))
    db.flush()

    db.add(_row(Score, id=SCORE_NULL_ID, participant_id=PARTICIPANT_ID, hole_number=1,
                strokes=5, par=4, score_to_par=1))
    # 정상 행도 하나 둔다. 둘이 섞여도 되는지 본다.
    db.add(_row(Score, id=SCORE_OK_ID, participant_id=PARTICIPANT_ID, hole_number=2,
                strokes=4, par=4, score_to_par=0,
                created_at=dt.datetime(2026, 9, 24, 7, 0),
                updated_at=dt.datetime(2026, 9, 24, 7, 0)))
    db.commit()

    # 운영에 실제로 있던 행을 만든다: 타임스탬프가 비어 있다.
    # ORM 으로는 이 상태를 만들 수 없다. default=func.now() 가 None 을
    # 덮어쓰기 때문이다. 바로 그래서 운영의 NULL 행도 ORM 이 아니라
    # 덤프 복원 같은 경로로 들어왔다. 같은 방식으로 재현한다.
    db.execute(text("UPDATE scores SET created_at = NULL, updated_at = NULL "
                    "WHERE id = :sid"), {"sid": SCORE_NULL_ID})
    db.commit()
    db.close()

    app = FastAPI()
    app.include_router(round_score_router, prefix="/api/v1")
    app.include_router(meeting_score_router, prefix="/api/v1")

    def _current_user():
        session = SessionLocal()
        try:
            return session.query(User).filter(User.id == USER_ID).first()
        finally:
            session.close()

    app.dependency_overrides[get_current_active_user] = _current_user
    return TestClient(app)


def _scores_of(payload):
    if isinstance(payload, list):
        return payload
    return payload.get("scores") or payload.get("items") or []


@pytest.mark.parametrize("prefix", ["rounds", "meetings"])
def test_타임스탬프가_비어도_점수목록이_열린다(client, prefix):
    """앱의 점수 입력 화면이 부르는 경로. 두 접두어 모두 같은 핸들러다."""
    res = client.get(
        f"/api/v1/{prefix}/{MEETING_ID}/participants/current/scores"
    )
    assert res.status_code == 200, res.text
    scores = _scores_of(res.json())
    assert len(scores) == 2
    by_hole = {s["hole_number"]: s for s in scores}
    # NULL 이던 행은 null 로 내려온다. 500 이 아니라.
    assert by_hole[1]["created_at"] is None
    assert by_hole[1]["updated_at"] is None
    assert by_hole[1]["strokes"] == 5
    # 값이 있는 행은 그대로 내려온다.
    assert by_hole[2]["created_at"] is not None


def test_참가자ID를_직접_줘도_열린다(client):
    res = client.get(
        f"/api/v1/rounds/{MEETING_ID}/participants/{PARTICIPANT_ID}/scores"
    )
    assert res.status_code == 200, res.text
    assert len(_scores_of(res.json())) == 2


def test_통계도_열린다(client):
    res = client.get(
        f"/api/v1/rounds/{MEETING_ID}/participants/current/scores/stats"
    )
    assert res.status_code == 200, res.text
    assert res.json()["total_strokes"] == 9


def test_응답스키마에_필수_타임스탬프가_남아있지_않다():
    """같은 버그가 다시 생기지 않게 스키마 전체를 확인한다."""
    import ast
    import os

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    schemas_dir = os.path.join(here, "schemas")
    timestamps = {
        "created_at", "updated_at", "deleted_at", "joined_at", "paid_at",
        "approved_at", "completed_at", "canceled_at", "cancelled_at",
        "answered_at", "read_at", "verified_at", "last_login_at",
    }

    offenders = []
    for name in sorted(os.listdir(schemas_dir)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(schemas_dir, name)
        src = open(path, encoding="utf-8").read()
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.ClassDef) or not node.name.endswith("Response"):
                continue
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and stmt.target.id in timestamps
                    and stmt.value is None
                    and (ast.get_source_segment(src, stmt.annotation) or "").strip() == "datetime"
                ):
                    offenders.append(f"{name}:{stmt.lineno} {node.name}.{stmt.target.id}")

    assert not offenders, (
        "응답 스키마에 필수 datetime 타임스탬프가 남아 있습니다. "
        "DB 에 NULL 인 행이 하나라도 있으면 그 엔드포인트가 500 이 됩니다:\n  "
        + "\n  ".join(offenders)
    )
