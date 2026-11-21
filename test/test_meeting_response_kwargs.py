"""MeetingResponse 를 만들 때 같은 키를 두 번 넘기지 않는지 정적으로 검사한다.

routers/meetings 에는 아래 패턴이 반복된다.

    meeting_dict = {col.key: getattr(meeting, col.key) for col in Meeting.__table__.columns}
    MeetingResponse(**meeting_dict, club_name=..., participant_count=...)

여기서 meeting_dict 에 이미 들어 있는 키(= Meeting 의 컬럼)를 키워드로 한 번 더
넘기면 파이썬이 TypeError("got multiple values for keyword argument") 를 던지고,
그 라우트는 호출될 때마다 500 이 된다. 런타임에만 드러나고 조회가 드문 경로라면
오래 묻혀 있게 된다 — base.py 의 get_rounding_meetings 가 실제로 그랬다.

가공한 값으로 컬럼을 덮어써야 할 때는 키워드를 늘리는 대신 딕셔너리를 병합한다.

    MeetingResponse(**{**meeting_dict, "settlement_method": ..., "tee_time": ...})
"""
from __future__ import annotations

import ast
from pathlib import Path

from models.meeting import Meeting

ROUTERS = Path(__file__).resolve().parent.parent / "routers" / "meetings"
COLUMNS = {col.key for col in Meeting.__table__.columns}


def _collisions(path: Path) -> list[str]:
    """`MeetingResponse(**dict, key=...)` 에서 key 가 컬럼과 겹치는 것들"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "MeetingResponse":
            continue
        # ** 로 펼쳐지는 인자가 있는 호출만 대상 (keyword.arg is None == **)
        if not any(kw.arg is None for kw in node.keywords):
            continue
        for kw in node.keywords:
            if kw.arg and kw.arg in COLUMNS:
                found.append(f"{path.name}:{node.lineno} 에서 '{kw.arg}'")
    return found


def test_meeting_columns_are_not_passed_twice():
    files = sorted(ROUTERS.rglob("*.py"))
    assert files, "routers/meetings 에서 파이썬 파일을 못 찾았다"

    collisions = [c for f in files for c in _collisions(f)]
    assert not collisions, (
        "MeetingResponse 에 Meeting 컬럼과 같은 키를 두 번 넘긴다 "
        "(TypeError 로 해당 라우트가 항상 500 이 된다):\n  "
        + "\n  ".join(collisions)
        + "\n\n가공값으로 덮어쓸 때는 MeetingResponse(**{**meeting_dict, \"키\": 값}) 형태로 병합하라."
    )


def test_detector_catches_the_old_pattern(tmp_path: Path):
    """검사기가 실제로 잡는지 확인한다 (항상 통과하는 테스트가 되지 않도록)"""
    some_column = next(iter(COLUMNS))
    bad = tmp_path / "bad.py"
    bad.write_text(
        "MeetingResponse(**meeting_dict, %s=1, club_name='x')\n" % some_column,
        encoding="utf-8",
    )
    assert _collisions(bad) == [f"bad.py:1 에서 '{some_column}'"]

    good = tmp_path / "good.py"
    good.write_text(
        "MeetingResponse(**{**meeting_dict, '%s': 1}, club_name='x')\n" % some_column,
        encoding="utf-8",
    )
    assert _collisions(good) == []
