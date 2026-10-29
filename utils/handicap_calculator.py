"""
핸디캡 계산 유틸리티

핸디캡 자동 계산 및 관리 로직
"""
import logging
from decimal import Decimal
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import desc
from models import (User, UserScoreHistory, Meeting, MeetingStatus, HandicapUpdateMethod, Score, MeetingParticipant,
                    MeetingResult)

logger = logging.getLogger(__name__)


def calculate_handicap_from_average_score(average_score: float) -> Decimal:
    """
    평균 타수 기반 핸디캡 계산 공통 함수

    Args:
        average_score: 평균 타수

    Returns:
        핸디캡 (0~72 범위로 클램프, 소수점 첫째 자리까지)
    """
    handicap = float(average_score) - 72.0
    handicap = max(0.0, min(72.0, handicap))
    return Decimal(str(round(handicap, 1)))


def get_recent_scores(db: Session, user_id: int, count: int = 10) -> list[UserScoreHistory]:
    """
    최근 N경기 스코어 조회
    
    Args:
        db: 데이터베이스 세션
        user_id: 사용자 ID
        count: 조회할 경기 수 (기본값: 10)
    
    Returns:
        최근 N경기 스코어 히스토리 리스트 (최신순)
    """
    scores = db.query(UserScoreHistory).filter(UserScoreHistory.user_id == user_id).order_by(
        desc(UserScoreHistory.played_at)).limit(count).all()

    return scores


def calculate_moving_average(scores: list[UserScoreHistory], count: int = 5) -> float | None:
    """
    이동평균 계산 (최근 N경기 평균)
    
    Args:
        scores: 스코어 히스토리 리스트
        count: 평균 계산에 사용할 경기 수 (기본값: 5)
    
    Returns:
        평균 타수 (float) 또는 None (데이터 부족 시)
    """
    if not scores:
        return None

    # 최근 N경기만 사용
    recent_scores = scores[:count]

    if not recent_scores:
        return None

    # 평균 계산
    total_score = sum(score.gross_score for score in recent_scores)
    average = total_score / len(recent_scores)

    return round(average, 1)


def get_user_handicap_for_formation(db: Session, user_id: int) -> Decimal | None:
    """
    팀 편성에 사용할 핸디캡 조회
    - 경기 기록이 1경기 이상이고 calculated_handicap이 있으면 calculated_handicap 사용
    - 그렇지 않으면 initial_handicap 사용
    - 둘 다 없으면 기존 handicap 필드 사용 (하위 호환성)
    
    Args:
        db: 데이터베이스 세션
        user_id: 사용자 ID
    
    Returns:
        핸디캡 (Decimal) 또는 None
    """
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        return None

    # 실제 경기 기록 확인 (handicap_calculation_count와 실제 데이터 일치 확인)
    actual_score_count = db.query(UserScoreHistory).filter(UserScoreHistory.user_id == user_id).count()

    # 경기 기록이 1경기 이상이고 calculated_handicap이 있으면 사용
    # (handicap_calculation_count가 0이어도 실제 데이터가 있으면 사용)
    if actual_score_count >= 1 and user.handicap is not None:
        return user.handicap

    # 경기 기록이 없거나 calculated_handicap이 없으면 initial_handicap 사용
    if user.handicap_init is not None:
        return user.handicap_init

    # 둘 다 없으면 기존 handicap 필드 사용 (하위 호환성)
    if user.handicap is not None:
        return user.handicap

    return None


def validate_handicap(handicap: float) -> bool:
    """
    핸디캡 유효성 검사 (0~72 범위)
    
    Args:
        handicap: 핸디캡 값
    
    Returns:
        유효 여부 (bool)
    """
    return 0 <= handicap <= 72


def update_user_handicap(db: Session,
                         user_id: int,
                         score_count: int = 5,
                         use_realtime: bool = True) -> Decimal | None:
    """
    사용자 핸디캡 자동 업데이트
    
    Args:
        db: 데이터베이스 세션
        user_id: 사용자 ID
        score_count: 평균 계산에 사용할 경기 수 (기본값: 5)
        use_realtime: 실시간 갱신 여부 (기본값: True)
    
    Returns:
        업데이트된 핸디캡 (Decimal) 또는 None
    """
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        return None

    # 최근 스코어 조회
    recent_scores = get_recent_scores(db, user_id, count=score_count * 2)  # 여유있게 조회

    if not recent_scores:
        # 스코어가 없으면 업데이트하지 않음
        return None

    # 이동평균 계산
    average_score = calculate_moving_average(recent_scores, count=score_count)

    if average_score is None:
        return None

    # 핸디캡 계산
    calculated_handicap = calculate_handicap_from_average_score(average_score)

    # 유효성 검사
    if not validate_handicap(float(calculated_handicap)):
        return None

    # 사용자 정보 업데이트
    # 최근 score_count 경기 기준 평균 타수를 사용자 프로필에 반영
    user.average_score = int(round(average_score))
    user.handicap = calculated_handicap
    # 실제로 평균 계산에 사용된 경기 수 저장
    actual_count = min(len(recent_scores), score_count)
    user.handicap_calculation_count = actual_count
    user.handicap_update_method = HandicapUpdateMethod.AUTO

    db.commit()
    db.refresh(user)

    return calculated_handicap


def check_all_holes_completed(db: Session, participant_id: int, expected_holes: int = 18) -> bool:
    """
    모든 홀의 스코어가 입력되었는지 확인
    
    Args:
        db: 데이터베이스 세션
        participant_id: 참가자 ID
        expected_holes: 예상 홀 수 (기본값: 18)
    
    Returns:
        모든 홀이 입력되었으면 True, 아니면 False
    """
    scores = db.query(Score).filter(Score.participant_id == participant_id).all()

    if not scores:
        return False

    # 입력된 홀 번호 집합
    completed_holes = set(score.hole_number for score in scores)

    # 예상 홀 수만큼 입력되었는지 확인
    return len(completed_holes) >= expected_holes


def calculate_gross_score(db: Session, participant_id: int) -> int | None:
    """
    참가자의 최종 스코어(Gross Score) 계산
    모든 홀의 strokes 합산
    
    Args:
        db: 데이터베이스 세션
        participant_id: 참가자 ID
    
    Returns:
        최종 스코어 (int) 또는 None (스코어가 없을 경우)
    """
    scores = db.query(Score).filter(Score.participant_id == participant_id).all()

    if not scores:
        return None

    # 모든 홀의 strokes 합산
    total_strokes = sum(score.strokes for score in scores)

    return total_strokes


def save_score_to_history(db: Session, user_id: int, meeting_id: int, gross_score: int, handicap_used: Decimal,
                          played_at: datetime) -> UserScoreHistory | None:
    """
    UserScoreHistory에 스코어 저장
    중복 저장 방지 로직 포함
    
    Args:
        db: 데이터베이스 세션
        user_id: 사용자 ID
        meeting_id: 모임 ID
        gross_score: 실제 타수 (Gross Score)
        handicap_used: 사용된 핸디캡
        played_at: 경기 날짜
    
    Returns:
        생성된 UserScoreHistory 또는 None (중복인 경우)
    """
    # 중복 저장 방지: 같은 meeting_id로 이미 저장된 기록이 있는지 확인
    existing = db.query(UserScoreHistory).filter(UserScoreHistory.user_id == user_id,
                                                 UserScoreHistory.meeting_id == meeting_id).first()

    if existing:
        # 이미 저장된 기록이 있으면 None 반환 (중복 저장 방지)
        return None

    # Net Score 계산
    net_score = Decimal(str(gross_score)) - handicap_used

    # UserScoreHistory 생성 (id는 자동 생성)
    score_history = UserScoreHistory(user_id=user_id,
                                     meeting_id=meeting_id,
                                     gross_score=gross_score,
                                     net_score=net_score,
                                     handicap_used=handicap_used,
                                     played_at=played_at)

    db.add(score_history)
    db.commit()
    db.refresh(score_history)

    return score_history


def set_handicap_after_round_on_history(
    db: Session,
    score_history: UserScoreHistory | None,
    updated_handicap: Decimal | None,
) -> None:
    """이 경기 반영 후 자동 재계산된 핸디캡을 기록에 저장 (기록 내역 표시용)."""
    if score_history is None or updated_handicap is None:
        return
    score_history.handicap_after_round = updated_handicap
    db.commit()
    db.refresh(score_history)


def process_participant_score(db: Session,
                              participant_id: int,
                              meeting_id: int,
                              score_count: int = 5) -> Decimal | None:
    """
    참가자 스코어 처리 (스코어 저장 + 핸디캡 업데이트)
    
    Args:
        db: 데이터베이스 세션
        participant_id: 참가자 ID
        meeting_id: 모임 ID
        score_count: 평균 계산에 사용할 경기 수 (기본값: 5)
    
    Returns:
        업데이트된 핸디캡 (Decimal) 또는 None
    """
    # 참가자 정보 조회
    participant = db.query(MeetingParticipant).filter(MeetingParticipant.id == participant_id).first()

    if not participant:
        return None

    user_id = participant.user_id

    # 모임 정보 조회
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        return None

    # 최종 스코어 계산
    gross_score = calculate_gross_score(db, participant_id)
    if gross_score is None:
        # 스코어가 없으면 처리하지 않음
        return None

    # 사용할 핸디캡 조회 (팀 편성용 함수 사용)
    handicap_used = get_user_handicap_for_formation(db, user_id)
    if handicap_used is None:
        # 핸디캡이 없으면 0으로 처리
        handicap_used = Decimal('0')

    # UserScoreHistory에 저장
    played_at = meeting.meeting_time or datetime.now()
    score_history = save_score_to_history(db=db,
                                          user_id=user_id,
                                          meeting_id=meeting_id,
                                          gross_score=gross_score,
                                          handicap_used=handicap_used,
                                          played_at=played_at)

    # 중복 저장이면 None 반환
    if score_history is None:
        return None

    # 핸디캡 자동 업데이트
    updated_handicap = update_user_handicap(db=db, user_id=user_id, score_count=score_count, use_realtime=True)
    set_handicap_after_round_on_history(db, score_history, updated_handicap)

    return updated_handicap


def process_meeting_completion(db: Session, meeting_id: int, score_count: int = 5) -> dict:
    """
    모임 완료 시 모든 참가자 스코어 처리
    
    Args:
        db: 데이터베이스 세션
        meeting_id: 모임 ID
        score_count: 평균 계산에 사용할 경기 수 (기본값: 5)
    
    Returns:
        처리 결과 딕셔너리 (처리된 참가자 수, 업데이트된 핸디캡 수 등)
    """
    # 모임 정보 조회
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        return {"error": "모임을 찾을 수 없습니다."}

    # 참가자 조회
    participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).all()

    if not participants:
        return {"processed": 0, "updated": 0}

    processed_count = 0
    updated_count = 0

    # 각 참가자별로 스코어 처리
    for participant in participants:
        try:
            updated_handicap = process_participant_score(db=db,
                                                         participant_id=participant.id,
                                                         meeting_id=meeting_id,
                                                         score_count=score_count)

            if updated_handicap is not None:
                processed_count += 1
                updated_count += 1
            else:
                # 스코어가 없거나 중복 저장인 경우
                processed_count += 1

        except Exception as e:
            # 개별 참가자 처리 실패 시 로그만 남기고 계속 진행
            logger.error(f"참가자 스코어 처리 실패: participant_id={participant.id}, error={str(e)}")
            continue

    # MeetingResult 생성 (모임 완료 시 대회 성적 저장)
    try:
        results_created = create_meeting_results(db, meeting_id)
        logger.info(f"MeetingResult 생성 완료: {results_created}개")
    except Exception as e:
        logger.error(f"MeetingResult 생성 실패: {str(e)}")

    return {"processed": processed_count, "updated": updated_count, "total_participants": len(participants)}


def create_meeting_results(db: Session, meeting_id: int) -> int:
    """
    모임 완료 시 MeetingResult 생성 (Net Score 계산 및 순위 저장)
    
    Args:
        db: 데이터베이스 세션
        meeting_id: 모임 ID
    
    Returns:
        생성된 MeetingResult 개수
    """
    # 모임 정보 조회
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        logger.error(f"모임을 찾을 수 없습니다: {meeting_id}")
        return 0

    # 이미 MeetingResult가 생성되어 있는지 확인 (중복 방지)
    existing_results = db.query(MeetingResult).filter(MeetingResult.meeting_id == meeting_id).count()

    if existing_results > 0:
        logger.info(f"이미 MeetingResult가 존재합니다: {meeting_id} ({existing_results}개)")
        return existing_results

    # 참가자 조회
    participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).all()

    if not participants:
        logger.warning(f"참가자가 없습니다: {meeting_id}")
        return 0

    # 각 참가자의 스코어 및 핸디캡 정보 수집
    participant_results = []

    for participant in participants:
        try:
            # Gross Score 계산
            gross_score = calculate_gross_score(db, participant.id)
            if gross_score is None:
                logger.warning(f"참가자 스코어를 계산할 수 없습니다: participant_id={participant.id}")
                continue

            # 사용된 핸디캡 조회 (팀 편성 시 사용한 핸디캡)
            user_handicap = get_user_handicap_for_formation(db, participant.user_id)
            if user_handicap is None:
                # 핸디캡이 없으면 0으로 처리
                handicap_used = Decimal("0.0")
            else:
                handicap_used = user_handicap

            # Net Score 계산: Gross Score - Handicap
            net_score = Decimal(str(gross_score)) - handicap_used

            participant_results.append({
                "user_id": participant.user_id,
                "gross_score": gross_score,
                "net_score": net_score,
                "handicap_used": handicap_used,
                "completed_at": meeting.meeting_time or datetime.now()
            })

        except Exception as e:
            logger.error(f"참가자 스코어 처리 실패: participant_id={participant.id}, error={str(e)}")
            continue

    if not participant_results:
        logger.warning(f"처리 가능한 참가자 결과가 없습니다: {meeting_id}")
        return 0

    # Net Score 기준으로 정렬 (낮을수록 좋음)
    participant_results.sort(key=lambda x: float(x["net_score"]))

    # 순위 계산 (동점 처리: 같은 Net Score는 같은 순위)
    current_rank = 1
    previous_net_score = None

    for i, result in enumerate(participant_results):
        current_net_score = float(result["net_score"])

        # 이전 순위와 Net Score가 다르면 순위 증가
        if previous_net_score is not None and current_net_score != previous_net_score:
            current_rank = i + 1

        result["rank"] = current_rank
        previous_net_score = current_net_score

    # MeetingResult 생성
    created_count = 0

    for result in participant_results:
        try:
            # 중복 확인 (같은 모임, 같은 사용자)
            existing = db.query(MeetingResult).filter(MeetingResult.meeting_id == meeting_id,
                                                      MeetingResult.user_id == result["user_id"]).first()

            if existing:
                logger.warning(f"이미 MeetingResult가 존재합니다: meeting_id={meeting_id}, user_id={result['user_id']}")
                continue

            # UserScoreHistory 존재 확인 (스코어 정보는 UserScoreHistory에 저장됨)
            score_history = db.query(UserScoreHistory).filter(UserScoreHistory.user_id == result["user_id"],
                                                              UserScoreHistory.meeting_id == meeting_id).first()

            if not score_history:
                logger.warning(
                    f"UserScoreHistory가 없어 MeetingResult를 생성할 수 없습니다: user_id={result['user_id']}, meeting_id={meeting_id}"
                )
                continue

            meeting_result = MeetingResult(meeting_id=meeting_id,
                                           user_id=result["user_id"],
                                           rank=result["rank"],
                                           completed_at=result["completed_at"])

            db.add(meeting_result)
            created_count += 1

        except Exception as e:
            logger.error(f"MeetingResult 생성 실패: user_id={result['user_id']}, error={str(e)}")
            continue

    # 커밋
    try:
        db.commit()
        logger.info(f"MeetingResult 생성 완료: {created_count}개")
    except Exception as e:
        db.rollback()
        logger.error(f"MeetingResult 커밋 실패: {str(e)}")
        return 0

    return created_count
