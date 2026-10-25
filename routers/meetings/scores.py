"""
스코어 관리 API 라우터

골프 라운딩 스코어 입력 및 관리 기능
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
import logging
from math import ceil

# from sqlalchemy.dialects import mysql # 디버깅 쿼리출력용


# 로깅 설정
logger = logging.getLogger(__name__)

from database import get_db
from models import (
    Score, MeetingParticipant, Meeting, User, ClubMembership, UserScoreHistory, MeetingResult
)
from schemas import ClubRole, MeetingType
from schemas import (
    MessageResponse,
    MeetingParticipantScoreCreate,
    ScoreCreate,
    ScoreListResponse,
    ScoreResponse,
    ScoreStats,
    ScoreUpdate,
    SimpleScoreCreate,
    SimpleScoreResponse,
)
from routers.auth import get_current_active_user, get_user_role_from_token
from fastapi.security import HTTPAuthorizationCredentials
from utils.jwt_auth import security
from utils.handicap_calculator import (
    get_user_handicap_for_formation,
    save_score_to_history,
    set_handicap_after_round_on_history,
    update_user_handicap,
    create_meeting_results
)
from utils.datetime_utils import get_kst_now
from decimal import Decimal

router = APIRouter(prefix="/scores", tags=["scores"])

# 모임 관련 스코어 엔드포인트를 위한 별도 router
meeting_score_router = APIRouter(prefix="/meetings", tags=["모임 스코어"])
# 라운딩 prefix 기반 호환용 router (RN 앱에서 사용)
round_score_router = APIRouter(prefix="/rounds", tags=["라운딩 스코어"])


def _build_score_response(score: Score, meeting: Meeting | None = None) -> ScoreResponse:
    participant = score.participant
    meeting_obj = meeting or (participant.meeting if participant else None)
    user_obj = participant.user if participant else None
    return ScoreResponse(
        id=score.id,
        participant_id=score.participant_id,
        user_id=participant.user_id if participant and participant.user_id else 0,
        user_name=user_obj.realname if user_obj and user_obj.realname else "",
        user_nickname=user_obj.nickname if user_obj and user_obj.nickname else "",
        meeting_id=meeting_obj.id if meeting_obj else 0,
        meeting_name=meeting_obj.name if meeting_obj and meeting_obj.name else "",
        hole_number=score.hole_number,
        strokes=score.strokes,
        par=score.par,
        score_to_par=score.score_to_par,
        created_at=score.created_at,
        updated_at=score.updated_at,
    )


def _empty_score_stats() -> ScoreStats:
    return ScoreStats(
        total_strokes=0,
        total_par=0,
        total_score_to_par=0,
        average_strokes=0.0,
        average_par=0.0,
        average_score_to_par=0.0,
        best_hole=0,
        worst_hole=0,
        birdies=0,
        pars=0,
        bogeys=0,
        double_bogeys=0,
        triple_bogeys=0,
        worse=0,
    )


def _build_score_stats(scores: list[Score]) -> ScoreStats:
    if not scores:
        return _empty_score_stats()

    total_holes = len(scores)
    total_strokes = sum(score.strokes for score in scores)
    total_par = sum(score.par for score in scores)
    total_score_to_par = sum(score.score_to_par for score in scores)
    score_to_par_values = [score.score_to_par for score in scores]
    stroke_values = [score.strokes for score in scores]

    return ScoreStats(
        total_strokes=total_strokes,
        total_par=total_par,
        total_score_to_par=total_score_to_par,
        average_strokes=round(total_strokes / total_holes, 2),
        average_par=round(total_par / total_holes, 2),
        average_score_to_par=round(total_score_to_par / total_holes, 2),
        best_hole=min(stroke_values),
        worst_hole=max(stroke_values),
        birdies=sum(1 for value in score_to_par_values if value == -1),
        pars=sum(1 for value in score_to_par_values if value == 0),
        bogeys=sum(1 for value in score_to_par_values if value == 1),
        double_bogeys=sum(1 for value in score_to_par_values if value == 2),
        triple_bogeys=sum(1 for value in score_to_par_values if value == 3),
        worse=sum(1 for value in score_to_par_values if value >= 4),
    )


def _calc_total_pages(total: int, limit: int) -> int:
    if total <= 0:
        return 0
    return ceil(total / limit)


def _resolve_participant(
    db: Session,
    meeting_id: int,
    participant_id: str,
    current_user: User,
) -> MeetingParticipant:
    if participant_id == "current":
        participant = db.query(MeetingParticipant).filter(
            MeetingParticipant.meeting_id == meeting_id,
            MeetingParticipant.user_id == current_user.id,
        ).first()
        if not participant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="현재 사용자 참가자를 찾을 수 없습니다.",
            )
        return participant

    try:
        parsed_id = int(participant_id)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="participant_id가 유효하지 않습니다. 숫자 또는 'current'를 사용하세요.",
        ) from error

    participant = db.query(MeetingParticipant).filter(
        MeetingParticipant.id == parsed_id,
        MeetingParticipant.meeting_id == meeting_id,
    ).first()
    if not participant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="참가자를 찾을 수 없습니다.",
        )
    return participant


def _ensure_round_meeting(db: Session, meeting_id: int) -> Meeting:
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="모임을 찾을 수 없습니다.",
        )
    _meeting_type = getattr(meeting.meeting_type, "value", meeting.meeting_type)
    if _meeting_type != MeetingType.ROUND.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="라운딩 모임이 아닙니다.",
        )
    return meeting


def _set_participant_hole_score_flag(db: Session, participant_id: int, has_hole_scores: bool) -> None:
    db.query(MeetingParticipant).filter(
        MeetingParticipant.id == participant_id
    ).update(
        {MeetingParticipant.has_hole_scores: has_hole_scores},
        synchronize_session=False,
    )


def _sync_participant_hole_score_flag(db: Session, participant_id: int) -> bool:
    has_hole_scores = db.query(Score.id).filter(
        Score.participant_id == participant_id
    ).first() is not None
    _set_participant_hole_score_flag(db, participant_id, has_hole_scores)
    return has_hole_scores

def check_score_permission(user_id: int, participant_id: int, db: Session) -> bool:
    """스코어 관리 권한 확인 (본인 스코어만 수정 가능)"""
    try:
        # 사용자 정보 조회하여 관리자인지 확인
        from models import Admin
        admin = db.query(Admin).filter(
            Admin.id == user_id,
            Admin.deleted_at.is_(None)
        ).first()
        if admin:
            return True  # 관리자는 모든 스코어에 접근 가능
            
        # 참가자 정보 조회
        participant = db.query(MeetingParticipant).filter(
            MeetingParticipant.id == participant_id
        ).first()
        
        if not participant:
            return False
            
        # 본인 스코어인지 확인
        if participant.user_id == user_id:
            return True
            
        # 모임 정보 조회
        meeting = db.query(Meeting).filter(Meeting.id == participant.meeting_id).first()
        if not meeting:
            return False
            
        # 클럽 리더/매니저인지 확인
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == meeting.club_id,
            ClubMembership.user_id == user_id,
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER])
        ).first()
        
        return membership is not None
    except Exception as e:
        logger.error(f"권한 확인 중 오류: {str(e)}")
        return False

@router.post("/", response_model=ScoreResponse)
async def create_score(
    score_data: ScoreCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """스코어 등록"""
    try:
        # 권한 확인
        if not check_score_permission(current_user.id, score_data.participant_id, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="스코어 등록 권한이 없습니다."
            )
        
        # 참가자 존재 확인
        participant = db.query(MeetingParticipant).filter(
            MeetingParticipant.id == score_data.participant_id
        ).first()
        
        if not participant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="참가자를 찾을 수 없습니다."
            )
        
        # 중복 홀 번호 확인
        existing_score = db.query(Score).filter(
            Score.participant_id == score_data.participant_id,
            Score.hole_number == score_data.hole_number
        ).first()
        
        if existing_score:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"홀 {score_data.hole_number}번의 스코어가 이미 등록되어 있습니다."
            )
        
        # 스코어 생성
        score = Score(
            hole_number=score_data.hole_number,
            strokes=score_data.strokes,
            par=score_data.par,
            score_to_par=score_data.score_to_par,
            participant_id=score_data.participant_id
        )
        
        db.add(score)
        _set_participant_hole_score_flag(db, score_data.participant_id, True)
        db.commit()
        db.refresh(score)
        
        score = db.query(Score).options(
            joinedload(Score.participant).joinedload(MeetingParticipant.user),
            joinedload(Score.participant).joinedload(MeetingParticipant.meeting),
        ).filter(Score.id == score.id).first()
        return _build_score_response(score)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"스코어 등록 중 오류: {str(e)}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.get("/", response_model=ScoreListResponse)
async def get_scores(
    participant_id: int | None = Query(None, description="참가자 ID"),
    meeting_id: int | None = Query(None, description="모임 ID"),
    hole_number: int | None = Query(None, description="홀 번호"),
    page: int = Query(1, ge=1, description="페이지 번호"),
    limit: int = Query(20, ge=1, le=100, description="페이지 크기"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """스코어 목록 조회"""
    try:
        # 1) current_user.id 기준으로 participant_id 조회
        participant_query = db.query(MeetingParticipant.id).filter(
            MeetingParticipant.user_id == current_user.id
        )

        if meeting_id:
            participant_query = participant_query.filter(MeetingParticipant.meeting_id == meeting_id)
        if participant_id:
            participant_query = participant_query.filter(MeetingParticipant.id == participant_id)

        resolved_participant = participant_query.first()
        if not resolved_participant:
            return ScoreListResponse(scores=[], total=0, page=page, limit=limit, total_pages=0)

        resolved_participant_id = resolved_participant[0]

        # 2) ScoreResponse에 필요한 컬럼만 조회
        query = db.query(
            Score.id.label("id"),
            Score.participant_id.label("participant_id"),
            MeetingParticipant.user_id.label("user_id"),
            User.realname.label("user_name"),
            User.nickname.label("user_nickname"),
            Meeting.id.label("meeting_id"),
            Meeting.name.label("meeting_name"),
            Score.hole_number.label("hole_number"),
            Score.strokes.label("strokes"),
            Score.par.label("par"),
            Score.score_to_par.label("score_to_par"),
            Score.created_at.label("created_at"),
            Score.updated_at.label("updated_at"),
        ).join(
            MeetingParticipant, MeetingParticipant.id == Score.participant_id
        ).join(
            Meeting, Meeting.id == MeetingParticipant.meeting_id
        ).outerjoin(
            User, User.id == MeetingParticipant.user_id
        ).filter(
            Score.participant_id == resolved_participant_id
        )

        if hole_number:
            query = query.filter(Score.hole_number == hole_number)

        # # 디버깅용 쿼리출력 =============================================================
        # debug_query = query.order_by(Score.hole_number.asc())

        # raw_sql = debug_query.statement.compile(
        #     dialect=mysql.dialect(),
        #     compile_kwargs={"literal_binds": True},
        # )
        # print(f"[RAW SQL] {raw_sql}")
        # # 디버깅용 쿼리출력 =============================================================

        # 3) hole_number 오름차순 정렬 후 페이지네이션
        all_scores = query.order_by(Score.hole_number.asc()).all()
        total = len(all_scores)
        offset = (page - 1) * limit
        scores = all_scores[offset:offset + limit]
        score_responses = [
            ScoreResponse(
                id=score.id,
                participant_id=score.participant_id,
                user_id=score.user_id or 0,
                user_name=score.user_name or "",
                user_nickname=score.user_nickname or "",
                meeting_id=score.meeting_id,
                meeting_name=score.meeting_name or "",
                hole_number=score.hole_number,
                strokes=score.strokes,
                par=score.par,
                score_to_par=score.score_to_par,
                created_at=score.created_at,
                updated_at=score.updated_at,
            ) for score in scores
        ]
        
        return ScoreListResponse(
            scores=score_responses,
            total=total,
            page=page,
            limit=limit,
            total_pages=_calc_total_pages(total, limit),
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"스코어 목록 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.get("/{score_id}", response_model=ScoreResponse)
async def get_score(
    score_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """스코어 상세 조회"""
    try:
        score = db.query(Score).options(
            joinedload(Score.participant).joinedload(MeetingParticipant.user),
            joinedload(Score.participant).joinedload(MeetingParticipant.meeting),
        ).filter(
            Score.id == score_id
        ).first()
        
        if not score:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="스코어를 찾을 수 없습니다."
            )
        
        # 권한 확인 (관리자는 모든 스코어 조회 가능)
        from models import Admin
        admin = db.query(Admin).filter(
            Admin.id == current_user.id,
            Admin.deleted_at.is_(None)
        ).first()
        
        if not admin:
            if not check_score_permission(current_user.id, score.participant_id, db):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="스코어 조회 권한이 없습니다."
                )
        
        return _build_score_response(score)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"스코어 상세 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.put("/{score_id}", response_model=ScoreResponse)
async def update_score(
    score_id: int,
    score_data: ScoreUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    credentials: HTTPAuthorizationCredentials = Depends(security),
    request: Request = None
):
    """스코어 수정"""
    try:
        score = db.query(Score).filter(Score.id == score_id).first()
        
        if not score:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="스코어를 찾을 수 없습니다."
            )
        
        # 권한 확인 (관리자는 모든 스코어 수정 가능)
        # JWT 토큰에서 role 확인
        user_role = get_user_role_from_token(credentials, request)
        if user_role != "ADMIN":
            if not check_score_permission(current_user.id, score.participant_id, db):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="스코어 수정 권한이 없습니다."
                )
        
        # 중복 홀 번호 확인 (홀 번호가 변경되는 경우)
        if score_data.hole_number and score_data.hole_number != score.hole_number:
            existing_score = db.query(Score).filter(
                Score.participant_id == score.participant_id,
                Score.hole_number == score_data.hole_number,
                Score.id != score_id
            ).first()
            
            if existing_score:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"홀 {score_data.hole_number}번의 스코어가 이미 등록되어 있습니다."
                )
        
        # 스코어 업데이트
        update_data = score_data.model_dump(exclude_unset=True)

        if "strokes" in update_data or "par" in update_data:
            next_strokes = update_data.get("strokes", score.strokes)
            next_par = update_data.get("par", score.par)
            computed_score_to_par = next_strokes - next_par
            if "score_to_par" in update_data and update_data["score_to_par"] != computed_score_to_par:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="score_to_par는 strokes - par 값과 같아야 합니다.",
                )
            update_data["score_to_par"] = computed_score_to_par

        for field, value in update_data.items():
            setattr(score, field, value)
        
        db.commit()
        db.refresh(score)
        score = db.query(Score).options(
            joinedload(Score.participant).joinedload(MeetingParticipant.user),
            joinedload(Score.participant).joinedload(MeetingParticipant.meeting),
        ).filter(Score.id == score_id).first()

        return _build_score_response(score)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"스코어 수정 중 오류: {str(e)}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.delete("/{score_id}", response_model=MessageResponse)
async def delete_score(
    score_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    credentials: HTTPAuthorizationCredentials = Depends(security),
    request: Request = None
):
    """스코어 삭제"""
    try:
        score = db.query(Score).filter(Score.id == score_id).first()
        
        if not score:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="스코어를 찾을 수 없습니다."
            )
        
        # 권한 확인 (관리자는 모든 스코어 삭제 가능)
        user_role = get_user_role_from_token(credentials, request)
        if user_role != "ADMIN":
            if not check_score_permission(current_user.id, score.participant_id, db):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="스코어 삭제 권한이 없습니다."
                )
        participant_id = score.participant_id
        db.delete(score)
        db.flush()
        _sync_participant_hole_score_flag(db, participant_id)
        db.commit()
        
        return MessageResponse(
            success=True,
            message="스코어가 성공적으로 삭제되었습니다."
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"스코어 삭제 중 오류: {str(e)}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.get("/stats/{participant_id}", response_model=ScoreStats)
async def get_score_stats(
    participant_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """스코어 통계 조회"""
    try:
        # 권한 확인
        if not check_score_permission(current_user.id, participant_id, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="스코어 통계 조회 권한이 없습니다."
            )
        
        # 참가자 스코어 조회
        scores = db.query(Score).filter(Score.participant_id == participant_id).all()
        
        return _build_score_stats(scores)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"스코어 통계 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

# =============================================================================
# 모임 관련 스코어 엔드포인트 (base.py에서 이동)
# =============================================================================

@round_score_router.get("/{meeting_id}/participants/{participant_id}/scores", response_model=ScoreListResponse)
@meeting_score_router.get("/{meeting_id}/participants/{participant_id}/scores", response_model=ScoreListResponse)
async def get_participant_scores(
    meeting_id: int,
    participant_id: str,
    page: int = 1,
    limit: int = 18,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """참가자 스코어 조회"""
    try:
        meeting = _ensure_round_meeting(db, meeting_id)
        participant = _resolve_participant(db, meeting_id, participant_id, current_user)
        
        # 권한 확인 (참가자 본인 또는 모임 매니저)
        user_id = current_user.id
        is_participant = (participant.user_id == user_id) if participant.user_id else False
        is_manager = meeting.created_by == user_id
        
        if not (is_participant or is_manager):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="스코어 조회 권한이 없습니다."
            )
        
        # 페이지네이션 계산
        offset = (page - 1) * limit
        
        # 스코어 조회
        scores_query = db.query(Score).options(
            joinedload(Score.participant).joinedload(MeetingParticipant.user),
            joinedload(Score.participant).joinedload(MeetingParticipant.meeting),
        ).filter(Score.participant_id == participant.id)
        total = scores_query.enable_eagerloads(False).order_by(None).with_entities(func.count(Score.id)).scalar() or 0
        scores = scores_query.order_by(Score.hole_number).offset(offset).limit(limit).all()
        score_responses = [_build_score_response(score, meeting=meeting) for score in scores]
        
        return ScoreListResponse(
            scores=score_responses,
            total=total,
            page=page,
            limit=limit,
            total_pages=_calc_total_pages(total, limit),
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"참가자 스코어 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )

@round_score_router.get("/{meeting_id}/participants/{participant_id}/scores/stats", response_model=ScoreStats)
@meeting_score_router.get("/{meeting_id}/participants/{participant_id}/scores/stats", response_model=ScoreStats)
async def get_participant_score_stats(
    meeting_id: int,
    participant_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """참가자 스코어 통계 조회"""
    try:
        meeting = _ensure_round_meeting(db, meeting_id)
        participant = _resolve_participant(db, meeting_id, participant_id, current_user)
        
        # 권한 확인 (참가자 본인 또는 모임 매니저)
        user_id = current_user.id
        is_participant = (participant.user_id == user_id) if participant.user_id else False
        is_manager = meeting.created_by == user_id
        
        if not (is_participant or is_manager):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="스코어 통계 조회 권한이 없습니다."
            )
        
        scores = db.query(Score).filter(Score.participant_id == participant.id).all()
        return _build_score_stats(scores)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"참가자 스코어 통계 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )


@round_score_router.post("/{meeting_id}/participants/{participant_id}/scores", response_model=ScoreResponse)
@meeting_score_router.post("/{meeting_id}/participants/{participant_id}/scores", response_model=ScoreResponse)
async def create_participant_score(
    meeting_id: int,
    participant_id: str,
    score_data: MeetingParticipantScoreCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """홀별 점수 등록"""
    try:
        meeting = _ensure_round_meeting(db, meeting_id)
        participant = _resolve_participant(db, meeting_id, participant_id, current_user)

        if not check_score_permission(current_user.id, participant.id, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="스코어 등록 권한이 없습니다.",
            )

        existing_score = db.query(Score).filter(
            Score.participant_id == participant.id,
            Score.hole_number == score_data.hole_number,
        ).first()
        if existing_score:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"홀 {score_data.hole_number}번의 스코어가 이미 등록되어 있습니다.",
            )

        score = Score(
            participant_id=participant.id,
            hole_number=score_data.hole_number,
            strokes=score_data.strokes,
            par=score_data.par,
            score_to_par=score_data.score_to_par,
        )
        db.add(score)
        _set_participant_hole_score_flag(db, participant.id, True)
        db.commit()
        db.refresh(score)

        score = db.query(Score).options(
            joinedload(Score.participant).joinedload(MeetingParticipant.user),
            joinedload(Score.participant).joinedload(MeetingParticipant.meeting),
        ).filter(Score.id == score.id).first()
        return _build_score_response(score, meeting=meeting)

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"홀별 점수 등록 실패: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}",
        )


@round_score_router.put("/{meeting_id}/participants/{participant_id}/scores/{score_id}", response_model=ScoreResponse)
@meeting_score_router.put("/{meeting_id}/participants/{participant_id}/scores/{score_id}", response_model=ScoreResponse)
async def update_participant_score(
    meeting_id: int,
    participant_id: str,
    score_id: int,
    score_data: ScoreUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """홀별 점수 수정"""
    try:
        meeting = _ensure_round_meeting(db, meeting_id)
        participant = _resolve_participant(db, meeting_id, participant_id, current_user)

        if not check_score_permission(current_user.id, participant.id, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="스코어 수정 권한이 없습니다.",
            )

        score = db.query(Score).filter(
            Score.id == score_id,
            Score.participant_id == participant.id,
        ).first()
        if not score:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="스코어를 찾을 수 없습니다.",
            )

        update_data = score_data.model_dump(exclude_unset=True)

        if "strokes" in update_data or "par" in update_data:
            next_strokes = update_data.get("strokes", score.strokes)
            next_par = update_data.get("par", score.par)
            computed_score_to_par = next_strokes - next_par
            if "score_to_par" in update_data and update_data["score_to_par"] != computed_score_to_par:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="score_to_par는 strokes - par 값과 같아야 합니다.",
                )
            update_data["score_to_par"] = computed_score_to_par

        next_hole_number = update_data.get("hole_number")
        if next_hole_number and next_hole_number != score.hole_number:
            existing_score = db.query(Score).filter(
                Score.participant_id == participant.id,
                Score.hole_number == next_hole_number,
                Score.id != score_id,
            ).first()
            if existing_score:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"홀 {next_hole_number}번의 스코어가 이미 등록되어 있습니다.",
                )

        for field, value in update_data.items():
            setattr(score, field, value)

        db.commit()
        db.refresh(score)

        score = db.query(Score).options(
            joinedload(Score.participant).joinedload(MeetingParticipant.user),
            joinedload(Score.participant).joinedload(MeetingParticipant.meeting),
        ).filter(Score.id == score.id).first()
        return _build_score_response(score, meeting=meeting)

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"홀별 점수 수정 실패: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}",
        )


@round_score_router.delete("/{meeting_id}/participants/{participant_id}/scores/{score_id}", response_model=MessageResponse)
@meeting_score_router.delete("/{meeting_id}/participants/{participant_id}/scores/{score_id}", response_model=MessageResponse)
async def delete_participant_score(
    meeting_id: int,
    participant_id: str,
    score_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """홀별 점수 삭제"""
    try:
        _ensure_round_meeting(db, meeting_id)
        participant = _resolve_participant(db, meeting_id, participant_id, current_user)

        if not check_score_permission(current_user.id, participant.id, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="스코어 삭제 권한이 없습니다.",
            )

        score = db.query(Score).filter(
            Score.id == score_id,
            Score.participant_id == participant.id,
        ).first()
        if not score:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="스코어를 찾을 수 없습니다.",
            )

        db.delete(score)
        db.flush()
        _sync_participant_hole_score_flag(db, participant.id)
        db.commit()
        return MessageResponse(success=True, message="스코어가 성공적으로 삭제되었습니다.")

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"홀별 점수 삭제 실패: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}",
        )


@meeting_score_router.post("/{meeting_id}/participants/{participant_id}/simple-score", response_model=SimpleScoreResponse)
async def create_simple_score(
    meeting_id: int,
    participant_id: int,
    score_data: SimpleScoreCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """간단 점수 입력 (총 스코어만 입력, 라운딩 종료 후 가능)"""
    try:
        user_id = current_user.id
        
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="모임을 찾을 수 없습니다."
            )
        
        # 라운딩 모임 확인 (모델 Enum / 스키마 Enum 값 비교)
        _mt = getattr(meeting.meeting_type, "value", None) or meeting.meeting_type
        if _mt != MeetingType.ROUND.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="라운딩 모임이 아닙니다."
            )
        
        # 라운딩 종료 확인
        if not meeting.rounding_completed_at:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="라운딩이 종료된 후 점수를 입력할 수 있습니다."
            )
        
        # 참가자 확인
        participant = db.query(MeetingParticipant).filter(
            MeetingParticipant.id == participant_id,
            MeetingParticipant.meeting_id == meeting_id
        ).first()
        
        if not participant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="참가자를 찾을 수 없습니다."
            )
        
        # 본인 확인 (게스트는 user_id가 없으므로 점수 입력 불가)
        if not participant.user_id or participant.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="본인의 점수만 입력할 수 있습니다."
            )
        
        # 중복 저장 방지 (기존 UserScoreHistory 확인)
        existing_score = db.query(UserScoreHistory).filter(
            UserScoreHistory.user_id == user_id,
            UserScoreHistory.meeting_id == meeting_id
        ).first()
        
        if existing_score:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="이미 점수가 입력되었습니다. 수정하려면 수정 API를 사용해주세요."
            )
        
        # 현재 핸디캡 조회
        handicap_used = get_user_handicap_for_formation(db, user_id)
        if handicap_used is None:
            # 핸디캡이 없으면 0으로 처리
            handicap_used = Decimal('0')
        
        # UserScoreHistory에 저장
        played_at = meeting.meeting_time or get_kst_now()
        score_history = save_score_to_history(
            db=db,
            user_id=user_id,
            meeting_id=meeting_id,
            gross_score=score_data.gross_score,
            handicap_used=handicap_used,
            played_at=played_at
        )
        
        # 중복 저장이면 에러 (이미 위에서 확인했지만 안전장치)
        if score_history is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="이미 점수가 입력되었습니다."
            )
        
        # 핸디캡 자동 업데이트
        updated_handicap = update_user_handicap(
            db=db,
            user_id=user_id,
            score_count=5
        )
        set_handicap_after_round_on_history(db, score_history, updated_handicap)

        # MeetingResult 생성/업데이트
        try:
            create_meeting_results(db, meeting_id)
        except Exception as e:
            logger.error(f"MeetingResult 생성 실패: {str(e)}")
            # MeetingResult 생성 실패해도 점수 입력은 성공으로 처리
        
        return SimpleScoreResponse(
            message="점수가 입력되었습니다.",
            score_history_id=score_history.id,
            updated_handicap=float(updated_handicap) if updated_handicap else None
        )
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"간단 점수 입력 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )

@meeting_score_router.put("/{meeting_id}/participants/{participant_id}/simple-score", response_model=SimpleScoreResponse)
async def update_simple_score(
    meeting_id: int,
    participant_id: int,
    score_data: SimpleScoreCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """간단 점수 수정 (총 스코어만 수정, 라운딩 종료 후 가능)"""
    try:
        user_id = current_user.id
        
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="모임을 찾을 수 없습니다."
            )
        
        # 라운딩 모임 확인 (모델 Enum / 스키마 Enum 값 비교)
        _mt = getattr(meeting.meeting_type, "value", None) or meeting.meeting_type
        if _mt != MeetingType.ROUND.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="라운딩 모임이 아닙니다."
            )
        
        # 라운딩 종료 확인
        if not meeting.rounding_completed_at:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="라운딩이 종료된 후 점수를 수정할 수 있습니다."
            )
        
        # 참가자 확인
        participant = db.query(MeetingParticipant).filter(
            MeetingParticipant.id == participant_id,
            MeetingParticipant.meeting_id == meeting_id
        ).first()
        
        if not participant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="참가자를 찾을 수 없습니다."
            )
        
        # 본인 확인 (게스트는 user_id가 없으므로 점수 수정 불가)
        if not participant.user_id or participant.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="본인의 점수만 수정할 수 있습니다."
            )
        
        # 기존 점수 기록 조회
        existing_score = db.query(UserScoreHistory).filter(
            UserScoreHistory.user_id == user_id,
            UserScoreHistory.meeting_id == meeting_id
        ).first()
        
        if not existing_score:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="수정할 점수 기록을 찾을 수 없습니다. 먼저 점수를 입력해주세요."
            )
        
        # 현재 핸디캡 조회 (기존 기록에 저장된 핸디캡 사용)
        handicap_used = existing_score.handicap_used
        
        # Net Score 재계산
        net_score = Decimal(str(score_data.gross_score)) - handicap_used
        
        # 기존 기록 업데이트
        existing_score.gross_score = score_data.gross_score
        existing_score.net_score = net_score
        db.commit()
        db.refresh(existing_score)
        
        # 핸디캡 자동 업데이트
        updated_handicap = update_user_handicap(
            db=db,
            user_id=user_id,
            score_count=5
        )
        set_handicap_after_round_on_history(db, existing_score, updated_handicap)

        # MeetingResult 재생성/업데이트
        try:
            # 기존 MeetingResult 삭제 후 재생성
            db.query(MeetingResult).filter(
                MeetingResult.meeting_id == meeting_id
            ).delete()
            db.commit()
            create_meeting_results(db, meeting_id)
        except Exception as e:
            logger.error(f"MeetingResult 재생성 실패: {str(e)}")
            # MeetingResult 재생성 실패해도 점수 수정은 성공으로 처리
        
        return SimpleScoreResponse(
            message="점수가 수정되었습니다.",
            score_history_id=existing_score.id,
            updated_handicap=float(updated_handicap) if updated_handicap else None
        )
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"간단 점수 수정 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )
