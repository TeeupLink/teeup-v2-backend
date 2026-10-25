from __future__ import annotations

# 실행:
#   .venv/bin/python scripts/loadtest/profile_team_formation.py
# 테스트 요약:
#   - 특정 ROUND 모임의 팀 편성 로직을 line-profiler로 라인 단위 측정
#   - 팀 편성 관련 핵심 함수 호출 비용을 출력
#   - 병목 함수 식별용 단일 프로파일링 도구

import argparse
import sys
from pathlib import Path
from collections.abc import Sequence

from sqlalchemy import func

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import SessionLocal
from models import Meeting, MeetingParticipant, MeetingType
from schemas import TeamFormationMode
from utils.team_formation import (
    _form_teams_by_criteria,
    form_random_teams,
    form_teams_by_mode,
    separate_by_gender,
    sort_by_handicap,
    sort_by_previous_record,
    zigzag_distribute,
)

try:
    from line_profiler import LineProfiler
except ImportError as exc:  # pragma: no cover - runtime guard
    raise SystemExit(
        "line-profiler is not installed. Run `pip install -r scripts/loadtest/requirements.txt` first."
    ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Line profile for team formation")
    parser.add_argument(
        "--meeting-id",
        type=int,
        default=None,
        help="target ROUND meeting_id (기본: 참가자 2명 이상 ROUND 중 최신)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default=TeamFormationMode.GENDER_MIXED_RANDOM.value,
        choices=[mode.value for mode in TeamFormationMode],
        help="team formation mode",
    )
    parser.add_argument("--team-size", type=int, default=4, help="team size")
    return parser.parse_args()


def _validate_inputs(meeting: Meeting | None, participants: Sequence[MeetingParticipant]) -> None:
    if meeting is None:
        raise SystemExit("Meeting not found for given meeting_id.")
    if meeting.meeting_type != MeetingType.ROUND:
        raise SystemExit("Only ROUND meetings can be profiled.")
    if len(participants) < 2:
        raise SystemExit("At least 2 participants are required for team formation.")


def _resolve_target_meeting_id(db, meeting_id: int | None) -> int:
    """명시 meeting_id가 없으면 참가자 2명 이상 ROUND 중 최신 meeting_id를 선택한다."""
    if meeting_id is not None:
        return meeting_id

    row = (
        db.query(Meeting.id)
        .join(MeetingParticipant, MeetingParticipant.meeting_id == Meeting.id)
        .filter(Meeting.meeting_type == MeetingType.ROUND)
        .group_by(Meeting.id)
        .having(func.count(MeetingParticipant.id) >= 2)
        .order_by(Meeting.id.desc())
        .first()
    )
    if row is None:
        raise SystemExit(
            "No ROUND meeting with at least 2 participants found. "
            "Use --meeting-id or prepare test data first."
        )
    return row[0]


def main() -> int:
    args = parse_args()
    db = SessionLocal()
    try:
        target_meeting_id = _resolve_target_meeting_id(db, args.meeting_id)
        meeting = db.query(Meeting).filter(Meeting.id == target_meeting_id).first()
        participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == target_meeting_id).all()
        _validate_inputs(meeting, participants)

        mode = TeamFormationMode(args.mode)
        profiler = LineProfiler()
        profiler.add_function(form_teams_by_mode)
        profiler.add_function(_form_teams_by_criteria)
        profiler.add_function(separate_by_gender)
        profiler.add_function(sort_by_handicap)
        profiler.add_function(sort_by_previous_record)
        profiler.add_function(zigzag_distribute)
        profiler.add_function(form_random_teams)

        teams = profiler.runcall(form_teams_by_mode, participants, mode, db, args.team_size)
        print(f"meeting_id={target_meeting_id}, participants={len(participants)}, teams={len(teams)}")
        profiler.print_stats(stripzeros=True)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
