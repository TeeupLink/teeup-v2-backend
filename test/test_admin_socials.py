"""백오피스 소셜 모임 보강 (backoffice #25) — 목록 필터·정렬, 상세·수정의 소셜 필드, 등록 검증.

SQLite 메모리 DB. 픽스처는 라운딩 테스트와 같다.
"""

import os
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("BACKEND_PORT", "7777")

from models import Meeting  # noqa: E402
from models.enums import SocialType  # noqa: E402
from schemas import MeetingType  # noqa: E402
from test_admin_rounds import A, env, make_people  # noqa: E402,F401
from utils.datetime_utils import get_kst_now  # noqa: E402


def make_social(db, club, name, at, social_type=SocialType.CASUAL, status="SCHEDULED"):
    m = Meeting(name=name, club_id=club.id, meeting_type=MeetingType.SOCIAL, social_type=social_type, meeting_time=at, status=status,
                max_participants=10, venue_name="강남역 고깃집")
    db.add(m)
    db.commit()
    return m


def test_social_list_filters_sort_and_counts(env):  # noqa: F811
    db, club, as_role = env
    now = get_kst_now().replace(tzinfo=None, hour=19, minute=0, second=0, microsecond=0)
    today = make_social(db, club, "오늘 번개", now)
    make_social(db, club, "송년회", now + timedelta(days=30), SocialType.DINNER)
    make_social(db, club, "지난 모임", now - timedelta(days=7))
    ps = make_people(db, today)
    ps[0].status = "PENDING"
    db.commit()
    client = as_role("MEETING_ADMIN")

    r = client.get(f"{A}/meetings/event").json()
    assert [m["name"] for m in r["data"]] == ["송년회", "오늘 번개", "지난 모임"]
    row = r["data"][1]
    assert row["participant_count"] == 3 and row["confirmed_count"] == 2 and row["social_type"] == "CASUAL"

    assert [m["name"] for m in client.get(f"{A}/meetings/event", params={"date": "today"}).json()["data"]] == ["오늘 번개"]
    assert [m["name"] for m in client.get(f"{A}/meetings/event", params={"social_type": "DINNER"}).json()["data"]] == ["송년회"]
    assert [m["name"] for m in client.get(f"{A}/meetings/event", params={"sort": "meeting_time"}).json()["data"]][0] == "지난 모임"
    assert client.get(f"{A}/meetings/event", params={"social_type": "GOLF"}).status_code == 422


def test_social_detail_and_update_social_fields(env):  # noqa: F811
    db, club, as_role = env
    m = make_social(db, club, "송년회", datetime(2026, 12, 20, 19, 0), SocialType.DINNER)
    client = as_role("MEETING_ADMIN")

    r = client.put(f"{A}/meetings/{m.id}", json={"social_type": "EVENT", "social_cost": 30000, "settlement_method": "EQUAL_SPLIT",
                                                 "application_deadline": "2026-12-18T18:00:00"})
    assert r.status_code == 200
    d = client.get(f"{A}/meetings/{m.id}").json()
    assert d["social_type"] == "EVENT" and d["social_cost"] == 30000 and d["settlement_method"] == "EQUAL_SPLIT"
    assert d["application_deadline"].startswith("2026-12-18T18:00") and d["settlement_confirmed"] is False

    # 빈 값은 비운다 (예전에는 '' 가 그대로 들어가 Enum 컬럼이 깨졌다)
    client.put(f"{A}/meetings/{m.id}", json={"settlement_method": "", "social_cost": None})
    db.expire_all()
    got = db.get(Meeting, m.id)
    assert got.settlement_method is None and got.social_cost is None

    # 라운딩 전용 필드는 소셜에 반영하지 않는다
    client.put(f"{A}/meetings/{m.id}", json={"green_fee": 150000})
    db.expire_all()
    assert db.get(Meeting, m.id).green_fee is None


def test_social_create_validates_and_404_is_not_500(env):  # noqa: F811
    db, club, as_role = env
    client = as_role("MEETING_ADMIN")

    r = client.post(f"{A}/meetings/event", json={"club_id": club.id, "name": " "})
    assert r.status_code == 422
    assert {e["field"] for e in r.json()["detail"]} == {"name", "meeting_time"}

    r = client.post(f"{A}/meetings/event", json={"club_id": 999, "name": "번개", "meeting_time": "2026-11-01T19:00:00"})
    assert r.status_code == 404

    r = client.post(f"{A}/meetings/event", json={"club_id": club.id, "name": "번개", "meeting_time": "2026-11-01T19:00:00", "type": "DINNER",
                                                 "venue_name": "역삼 고깃집", "max_participants": 12})
    assert r.status_code == 200
    m = db.get(Meeting, r.json()["id"])
    assert m.social_type == SocialType.DINNER and m.created_by is None
