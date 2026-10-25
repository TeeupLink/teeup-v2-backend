"""
모임 워크플로우 관리 API
- 참가 신청 관리
- 팀 편성 관리
- 정산 관리
"""

from fastapi import APIRouter, Depends, HTTPException, status, Body
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, and_, or_
from datetime import datetime, time, timedelta
from decimal import Decimal
import logging
from schemas.team import TeamMemberResponse
from utils.datetime_utils import get_kst_now

from database import get_db
from models import (
    User, Club, ClubMembership, Meeting, MeetingParticipant,
    Team, TeamMember, Expense,
    Notification, MeetingResult, UserScoreHistory, Guest, ParticipantType, Gender
)
from schemas import (
    MeetingType, MeetingSubtype, SettlementMethod,
    MeetingStatus, MeetingParticipantStatus, MeetingParticipantRole,
    ClubRole, NotificationType, NotificationStatus, TeamStatus
)
from schemas import (
    RoundingMeetingCreate, SocialMeetingCreate, MeetingUpdate, 
    MeetingResponse, MeetingParticipantResponse, PaginatedResponse,
    TeamFormationRequest, TeamFormationResponse, TeamFormationMode,
    TeamMemberResponse, TeamMemberAddRequest, GuestCreate
)
from routers.auth import get_current_active_user, get_current_user, get_current_user_allow_both
from services.push_delivery_service import send_push_to_user
from utils.permissions import MEMBERSHIP_ACTIVE_STATUSES
from utils.handicap_calculator import process_meeting_completion
from utils.team_formation import TeamFormationEngine
from utils.notification_service import notify_organizer_participant_added_after_recruitment_closed

router = APIRouter(prefix="/meetings", tags=["meeting-workflow"])
logger = logging.getLogger(__name__)


# 한국 시간 기준으로 application_deadline이 지났는지 확인하는 헬퍼 함수
def is_application_deadline_passed(application_deadline):
    """한국 시간 기준으로 신청 마감일이 지났는지 확인"""
    if not application_deadline:
        return False
    # 한국 시간으로 저장되어 있으므로 직접 비교 (서버가 한국 시간대이므로)
    return datetime.now() > application_deadline


def close_meetings_with_passed_deadline(db: Session) -> int:
    """
    모집 마감일이 지난 모임 중 status가 SCHEDULED인 것을 IN_PROGRESS로 변경.
    application_closed_early=True 로 설정.
    라운딩/소셜 목록 조회(GET /rounds/, GET /socials/) 시 호출되어, 
    목록을 열 때마다 마감일 경과 건이 자동 반영된다.
    """
    from utils.datetime_utils import get_kst_now
    now = get_kst_now()
    meetings = db.query(Meeting).filter(
        Meeting.application_deadline.isnot(None),
        Meeting.application_deadline < now,
        Meeting.status == "SCHEDULED",
    ).all()
    for meeting in meetings:
        meeting.application_closed_early = True
        meeting.status = MeetingStatus.IN_PROGRESS
    if meetings:
        db.commit()
        logger.info(f"모집 마감일 경과 자동 처리: {len(meetings)}건 (meeting_ids={[m.id for m in meetings]})")
    return len(meetings)


def is_meeting_time_started(meeting_time) -> bool:
    """티업시간(모임 시작시간) 경과 여부"""
    if not meeting_time:
        return False
    return datetime.now() >= meeting_time


def get_rounding_auto_complete_deadline(meeting_time):
    """라운딩 자동 종료 기준: 모임 다음날 00:00"""
    if not meeting_time:
        return None
    next_day = meeting_time.date() + timedelta(days=1)
    return datetime.combine(next_day, time.min)


def sync_rounding_completion_if_due(meeting: Meeting) -> bool:
    """자동 종료 기준을 넘긴 라운딩의 종료 시각을 동기화"""
    if not meeting or meeting.meeting_type != MeetingType.ROUND:
        return False
    if meeting.rounding_completed_at:
        return False

    deadline = get_rounding_auto_complete_deadline(meeting.meeting_time)
    if not deadline or datetime.now() < deadline:
        return False

    if not meeting.rounding_started_at and meeting.meeting_time:
        meeting.rounding_started_at = meeting.meeting_time
    meeting.rounding_completed_at = deadline
    return True


def close_roundings_with_passed_completion_deadline(db: Session) -> int:
    """
    다음날 00:00이 지난 라운딩을 자동 종료 처리.
    라운딩 목록/상세 조회 시 호출되어, 화면 재진입만으로도 상태가 맞춰진다.
    """
    today_start = datetime.combine(datetime.now().date(), time.min)
    meetings = db.query(Meeting).filter(
        Meeting.meeting_type == MeetingType.ROUND,
        Meeting.meeting_time.isnot(None),
        Meeting.meeting_time < today_start,
        Meeting.rounding_completed_at.is_(None),
    ).all()

    for meeting in meetings:
        if not meeting.rounding_started_at and meeting.meeting_time:
            meeting.rounding_started_at = meeting.meeting_time
        meeting.rounding_completed_at = get_rounding_auto_complete_deadline(meeting.meeting_time)

    if meetings:
        db.commit()
        logger.info(f"라운딩 자동 종료 처리: {len(meetings)}건 (meeting_ids={[m.id for m in meetings]})")
    return len(meetings)


def reset_team_formation_confirmation(meeting: Meeting, db: Session) -> bool:
    """팀 편집이 발생하면 기존 확정 상태를 다시 미확정으로 되돌림"""
    if not meeting:
        return False

    changed = False
    if meeting.team_formation_confirmed_at is not None:
        meeting.team_formation_confirmed_at = None
        changed = True

    teams = db.query(Team).filter(Team.meeting_id == meeting.id).all()
    for team in teams:
        if str(team.status or "").upper() != TeamStatus.DRAFT.value:
            team.status = TeamStatus.DRAFT.value
            changed = True

    return changed


# =============================================================================
# 참가 신청 관리 API
# =============================================================================


@router.post("/{meeting_id}/apply")
async def apply_to_meeting(meeting_id: int,
                           db: Session = Depends(get_db),
                           current_user: dict = Depends(get_current_active_user)):
    """모임 참가 (일반회원/리더/매니저 구분없이)"""
    try:
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # 클럽 멤버십 확인
        membership = db.query(ClubMembership).filter(ClubMembership.club_id == meeting.club_id,
                                                     ClubMembership.user_id == current_user.id,
                                                     ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES)).first()

        if not membership:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="클럽 멤버만 참가 신청할 수 있습니다.")

        # 이미 참가 신청했는지 확인
        existing_participant = db.query(MeetingParticipant).filter(
            MeetingParticipant.meeting_id == meeting_id, MeetingParticipant.user_id == current_user.id).first()

        if existing_participant:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 참가 신청한 모임입니다.")

        # 참가 신청 마감 확인 (한국 시간 기준으로 비교)
        if is_application_deadline_passed(meeting.application_deadline):
            # 자동 마감 시 알림 전송 (한 번만)
            if not meeting.application_closed_early:
                await send_application_closed_notification(meeting_id, db, is_early=False)
                meeting.application_closed_early = True  # 알림 전송 표시
                if str(getattr(meeting.status, "value", meeting.status)) == "SCHEDULED":
                    meeting.status = MeetingStatus.IN_PROGRESS
                db.commit()

            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="참가 신청 마감되었습니다.")

        # 조기 마감 확인
        if meeting.application_closed_early:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="참가 신청이 조기 마감되었습니다.")

        # 참가자 수 확인 (max_participants가 null이면 제한 없음)
        if meeting.max_participants is not None:
            current_participants = db.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == meeting_id).count()

            if current_participants >= meeting.max_participants:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="모임 정원이 마감되었습니다.")

        participant = MeetingParticipant(meeting_id=meeting_id,
                                         user_id=current_user.id,
                                         participant_type=ParticipantType.USER)

        db.add(participant)
        db.commit()
        db.refresh(participant)

        return {"message": "참가가 완료되었습니다."}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"참가 신청 오류: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.post("/{meeting_id}/guests")
async def add_round_guest(meeting_id: int,
                          guest_data: GuestCreate,
                          current_user: User = Depends(get_current_active_user),
                          db: Session = Depends(get_db)):
    """라운딩 게스트 추가 (생성 후 별도 추가)"""
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting or meeting.meeting_type != MeetingType.ROUND:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="라운딩을 찾을 수 없습니다.")

        if sync_rounding_completion_if_due(meeting):
            db.commit()
            db.refresh(meeting)

        from utils.permissions import is_meeting_organizer_or_manager
        if not is_meeting_organizer_or_manager(meeting_id, current_user.id, db):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="라운딩 매니저만 게스트를 추가할 수 있습니다.")

        if is_meeting_time_started(meeting.meeting_time):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="티업시간 이후에는 게스트를 추가할 수 없습니다.")

        if meeting.max_participants is not None:
            current_participants = db.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == meeting_id).count()
            if current_participants >= meeting.max_participants:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라운딩 정원이 가득 찼습니다.")

        guest_gender_enum = None
        if guest_data.gender:
            guest_gender_enum = Gender(guest_data.gender)

        guest_handicap_decimal = None
        if guest_data.handicap is not None:
            guest_handicap_decimal = Decimal(str(guest_data.handicap))

        from utils.team_formation import add_guest_to_meeting
        guest_participant = add_guest_to_meeting(meeting_id=meeting_id,
                                                 guest_name=guest_data.name,
                                                 guest_handicap=guest_handicap_decimal,
                                                 average_score=guest_data.average_score,
                                                 guest_birthdate=guest_data.birthdate,
                                                 guest_gender=guest_gender_enum,
                                                 db=db)

        try:
            notify_organizer_participant_added_after_recruitment_closed(
                db=db,
                meeting_id=meeting_id,
                participant_guest_name=guest_data.name,
            )
        except Exception as notify_error:
            logger.error(
                f"모집 완료 단계 organizer 알림 전송 실패 - meeting_id: {meeting_id}, guest_name: {guest_data.name}, error: {str(notify_error)}"
            )

        return {
            "message": "게스트가 추가되었습니다.",
            "participant_id": guest_participant.id,
            "guest_id": guest_participant.guest_id
        }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"게스트 정보 오류: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"게스트 추가 오류: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.post("/{meeting_id}/close-application")
async def close_application_early(meeting_id: int,
                                  db: Session = Depends(get_db),
                                  current_user: dict = Depends(get_current_active_user)):
    """참가 신청 조기 마감 (매니저/리더만 가능)"""
    try:
        logger.info(f"참가 신청 조기 마감 요청 - meeting_id: {meeting_id}, user_id: {current_user.id}")

        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            logger.error(f"모임을 찾을 수 없습니다: {meeting_id}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        logger.info(
            f"모임 조회 성공 - meeting_id: {meeting_id}, status: {meeting.status}, application_closed_early: {getattr(meeting, 'application_closed_early', False)}"
        )

        # 매니저/리더 권한 확인 (개설자 또는 클럽 리더/매니저)
        from utils.permissions import is_meeting_organizer_or_manager
        if not is_meeting_organizer_or_manager(meeting_id, current_user.id, db):
            logger.error(f"모집 조기 마감 권한 없음 - user_id: {current_user.id}, meeting_id: {meeting_id}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="매니저/리더만 모집을 조기 마감할 수 있습니다.")

        # 이미 조기 마감된 경우
        if meeting.application_closed_early:
            logger.info(f"이미 조기 마감된 모임 - meeting_id: {meeting_id}")
            return {"message": "이미 조기 마감된 모임입니다.", "outcome": "ALREADY_CLOSED"}

        # 조기 마감 처리
        meeting.application_closed_early = True
        logger.info(f"조기 마감 플래그 설정 - meeting_id: {meeting_id}")

        try:
            participant_count = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).count()

            logger.info(f"참가자 수: {participant_count}명 - meeting_id: {meeting_id}")

            # 참가자가 4명 이상이면 팀 편성 단계로 이동 안내
            if participant_count >= 4:
                logger.info(f"팀 편성 준비 - meeting_id: {meeting_id}, participant_count: {participant_count}")
                db.commit()
                try:
                    await send_application_closed_notification(meeting_id,
                                                               db,
                                                               is_early=True,
                                                               outcome="TEAM_FORMATION_READY",
                                                               participant_count=participant_count)
                except Exception as notif_error:
                    logger.error(f"알림 전송 실패 (팀 편성 준비): {notif_error}", exc_info=True)
                return {
                    "message": "참가 신청이 조기 마감되었습니다.\n팀 편성을 준비해주세요.",
                    "outcome": "TEAM_FORMATION_READY",
                    "participant_count": participant_count
                }

            # 참가 인원 미달이어도 자동 취소하지 않음 (모집만 마감 처리)
            db.commit()
            try:
                await send_application_closed_notification(
                    meeting_id,
                    db,
                    is_early=True,
                    outcome="CLOSED_INSUFFICIENT",
                    participant_count=participant_count,
                )
            except Exception as notif_error:
                logger.error(f"알림 전송 실패 (모집 마감): {notif_error}", exc_info=True)

            return {
                "message": "참가 신청이 조기 마감되었습니다.",
                "outcome": "CLOSED_INSUFFICIENT",
                "participant_count": participant_count
            }
        except Exception as inner_error:
            logger.error(f"조기 마감 처리 중 내부 오류: {inner_error}", exc_info=True)
            db.rollback()
            raise

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"참가 신청 조기 마감 오류: {e}", exc_info=True)
        import traceback
        logger.error(f"트레이스백: {traceback.format_exc()}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.get("/{meeting_id}/application-status")
async def get_application_status(meeting_id: int,
                                 db: Session = Depends(get_db),
                                 current_user: User = Depends(get_current_active_user)):
    """참가 현황 조회 (매니저/리더만 가능)"""
    try:
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # 클럽 멤버십 확인
        membership = db.query(ClubMembership).filter(ClubMembership.user_id == current_user.id,
                                                     ClubMembership.club_id == meeting.club_id,
                                                     ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES)).first()

        if not membership:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="해당 클럽의 멤버가 아닙니다.")

        # 참가자 현황 조회
        participant_count = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).count()

        return {
            "meeting_id":
            meeting_id,
            "max_participants":
            meeting.max_participants,
            "participant_count":
            participant_count,
            "application_closed_early":
            meeting.application_closed_early,
            "application_deadline":
            meeting.application_deadline.isoformat() if meeting.application_deadline else None,
            "can_start_team_formation":
            meeting.application_closed_early or is_application_deadline_passed(meeting.application_deadline)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"참가 신청 현황 조회 오류: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.post("/{meeting_id}/start-team-formation")
async def start_team_formation(meeting_id: int,
                               db: Session = Depends(get_db),
                               current_user: dict = Depends(get_current_active_user)):
    """팀 편성 시작 (모집마감 후에만 가능)"""
    try:
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # 소셜 모임은 팀 편성 기능을 지원하지 않음
        if meeting.meeting_type == MeetingType.SOCIAL:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="소셜 모임은 팀 편성 기능을 지원하지 않습니다.")

        # 매니저/리더 권한 확인
        # 프라이빗 라운딩 생성자가 참가하지 않은 경우에도 권한 확인
        from utils.permissions import is_meeting_organizer_or_manager
        user_id = current_user.get('id') if isinstance(current_user, dict) else current_user.id
        is_creator = meeting.created_by == user_id

        if not is_meeting_organizer_or_manager(meeting_id, user_id, db) and not is_creator:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="매니저/리더만 팀 편성을 시작할 수 있습니다.")

        if sync_rounding_completion_if_due(meeting):
            db.commit()
            db.refresh(meeting)

        if is_meeting_time_started(meeting.meeting_time):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="티업시간 이후에는 팀 편성을 시작할 수 없습니다.")

        # 모집마감 확인
        is_closed = meeting.application_closed_early or is_application_deadline_passed(meeting.application_deadline)
        if not is_closed:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="모집이 마감되지 않았습니다. 먼저 모집을 마감해주세요.")

        # 참가자 수 확인
        participant_count = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).count()

        # 라운딩 모임은 최소 4명, 소셜 모임은 최소 2명 필요
        min_participants = 4 if meeting.meeting_type == MeetingType.ROUND else 2
        if participant_count < min_participants:
            meeting_type_name = "라운딩" if meeting.meeting_type == MeetingType.ROUND else "소셜"
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"{meeting_type_name} 모임의 팀 편성을 위해서는 최소 {min_participants}명의 참가자가 필요합니다.")

        return {"message": "팀 편성을 시작할 수 있습니다.", "participant_count": participant_count, "can_auto_form": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"팀 편성 시작 확인 오류: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


# =============================================================================
# 팀 편성 관리 API
# =============================================================================


@router.post("/{meeting_id}/teams/auto-formation", response_model=TeamFormationResponse)
async def auto_form_teams(meeting_id: int,
                          formation_request: TeamFormationRequest,
                          db: Session = Depends(get_db),
                          current_user: dict = Depends(get_current_active_user)):
    """자동 팀 편성 (매니저/리더만 가능)"""
    try:
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # 소셜 모임은 팀 편성 기능을 지원하지 않음
        if meeting.meeting_type == MeetingType.SOCIAL:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="소셜 모임은 팀 편성 기능을 지원하지 않습니다.")

        # 매니저/리더 권한 확인
        # 프라이빗 라운딩 생성자가 참가하지 않은 경우에도 권한 확인
        from utils.permissions import is_meeting_organizer_or_manager
        user_id = current_user.get('id') if isinstance(current_user, dict) else current_user.id
        is_creator = meeting.created_by == user_id

        if not is_meeting_organizer_or_manager(meeting_id, user_id, db) and not is_creator:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="매니저/리더만 팀을 편성할 수 있습니다.")

        if sync_rounding_completion_if_due(meeting):
            db.commit()
            db.refresh(meeting)

        if is_meeting_time_started(meeting.meeting_time):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="티업시간 이후에는 팀 편성을 변경할 수 없습니다.")

        # 모집마감 확인
        is_closed = meeting.application_closed_early or is_application_deadline_passed(meeting.application_deadline)
        if not is_closed:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="모집이 마감되지 않았습니다. 먼저 모집을 마감해주세요.")

        # 참가자 조회 (게스트 포함)
        participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).options(
            joinedload(MeetingParticipant.user)).all()

        # 라운딩 모임은 최소 4명, 소셜 모임은 최소 2명 필요
        min_participants = 4 if meeting.meeting_type == MeetingType.ROUND else 2
        if len(participants) < min_participants:
            meeting_type_name = "라운딩" if meeting.meeting_type == MeetingType.ROUND else "소셜"
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"{meeting_type_name} 모임의 팀 편성을 위해서는 최소 {min_participants}명의 참가자가 필요합니다.")

        reset_team_formation_confirmation(meeting, db)

        # 팀 편성 실행 (TeamFormationEngine 사용)
        formation_engine = TeamFormationEngine(db)
        teams = formation_engine.create_teams_from_formation(meeting_id, formation_request, participants)

        # 편성 결과 조회 및 응답 생성
        from schemas import TeamResponse, TeamMemberResponse, TeamStatus

        team_responses = []
        for team in teams:
            members = []
            for team_member in team.members:
                # user_id 또는 guest_id를 통해 MeetingParticipant 조회
                if team_member.user_id:
                    participant = db.query(MeetingParticipant).filter(
                        MeetingParticipant.meeting_id == meeting_id,
                        or_((MeetingParticipant.user_id == team_member.user_id) if team_member.user_id else False,
                            (MeetingParticipant.guest_id == team_member.guest_id)
                            if team_member.guest_id else False)).first()
                elif team_member.guest_id:
                    participant = db.query(MeetingParticipant).filter(
                        MeetingParticipant.meeting_id == meeting_id,
                        MeetingParticipant.guest_id == team_member.guest_id).first()
                else:
                    continue

                if not participant:
                    continue

                # 게스트인 경우와 멤버인 경우 구분
                gender = None
                handicap = None
                average_score = None

                if participant.guest_id:
                    # guest_id를 통해 Guest 모델에서 정보 조회
                    guest = db.query(Guest).filter(Guest.id == participant.guest_id).first()
                    if guest:
                        user_name = guest.name or "게스트"
                        user_nickname = guest.name or "게스트"
                        gender = guest.gender.value if guest.gender else None
                        handicap = int(guest.handicap) if guest.handicap else None
                    else:
                        user_name = "게스트"
                        user_nickname = "게스트"
                        gender = None
                        handicap = None
                else:
                    user = participant.user
                    if not user:
                        continue
                    user_name = user.realname or user.nickname or "이름 없음"
                    user_nickname = user.nickname or "닉네임 없음"
                    gender = user.gender.value if user.gender else None
                    handicap = participant.handicap

                    # MeetingResult의 직전 경기 id를 찾은 뒤 UserScoreHistory.gross_score 조회
                    average_score = None
                    if participant.user_id:
                        last_result = db.query(MeetingResult).filter(
                            MeetingResult.user_id == participant.user_id,
                            MeetingResult.meeting_id != meeting_id  # 현재 모임 제외
                        ).order_by(MeetingResult.completed_at.desc()).first()

                        if last_result:
                            last_score = db.query(UserScoreHistory).filter(
                                UserScoreHistory.user_id == participant.user_id,
                                UserScoreHistory.meeting_id == last_result.meeting_id
                            ).order_by(UserScoreHistory.played_at.desc()).first()
                            if last_score:
                                average_score = last_score.gross_score

                members.append(
                    TeamMemberResponse(id=team_member.id,
                                       team_id=team.id,
                                       user_id=team_member.user_id,
                                       user_name=user_name,
                                       user_nickname=user_nickname,
                                       order=team_member.order,
                                       gender=gender,
                                       handicap=handicap,
                                       average_score=average_score,
                                       created_at=team_member.created_at))

            team_responses.append(
                TeamResponse(id=team.id,
                             name=team.name,
                             meeting_id=team.meeting_id,
                             formation_mode=formation_request.formation_mode,
                             status=TeamStatus.DRAFT,
                             formation_notes=team.formation_notes,
                             member_count=len(members),
                             members=members,
                             total_handicap=float(team.total_handicap) if team.total_handicap else None,
                             created_at=team.created_at,
                             updated_at=team.updated_at))

        # 편성 요약 생성
        formation_summary = formation_engine.get_formation_summary(teams)

        return TeamFormationResponse(teams=team_responses,
                                     total_teams=len(teams),
                                     unassigned_participants=[],
                                     total_participants=len(participants),
                                     formation_summary=formation_summary)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"자동 팀 편성 오류: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.post("/{meeting_id}/teams/confirm")
async def confirm_team_formation(meeting_id: int,
                                 db: Session = Depends(get_db),
                                 current_user: dict = Depends(get_current_active_user)):
    """팀 편성 확정 (매니저/리더만 가능)"""
    try:
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # 매니저/리더 권한 확인
        # 프라이빗 라운딩 생성자가 참가하지 않은 경우에도 권한 확인
        from utils.permissions import is_meeting_organizer_or_manager
        user_id = current_user.get('id') if isinstance(current_user, dict) else current_user.id
        is_creator = meeting.created_by == user_id

        if not is_meeting_organizer_or_manager(meeting_id, user_id, db) and not is_creator:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="매니저/리더만 팀 편성을 확정할 수 있습니다.")

        if sync_rounding_completion_if_due(meeting):
            db.commit()
            db.refresh(meeting)

        if is_meeting_time_started(meeting.meeting_time):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="티업시간 이후에는 팀 편성을 확정할 수 없습니다.")

        if meeting.team_formation_confirmed_at:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 팀 편성이 확정되었습니다.")

        # 팀 편성 확정
        teams = db.query(Team).filter(Team.meeting_id == meeting_id).all()
        for team in teams:
            team.status = TeamStatus.CONFIRMED.value

        # 확정일자 저장
        meeting.team_formation_confirmed_at = datetime.now()

        db.commit()
        db.refresh(meeting)

        logger.info(
            f"팀 편성 확정 완료 - meeting_id: {meeting_id}, team_formation_confirmed_at: {meeting.team_formation_confirmed_at}"
        )

        # 팀 편성 완료 알림 전송 (참가자들에게)
        try:
            logger.info(f"팀 편성 완료 알림 전송 시작 - meeting_id: {meeting_id}")
            await send_team_formation_completed_notification(meeting_id, db)
            logger.info(f"팀 편성 완료 알림 전송 완료 - meeting_id: {meeting_id}")
        except Exception as e:
            logger.error(f"팀 편성 완료 알림 전송 실패: {str(e)}", exc_info=True)

        return {"message": "팀 편성이 확정되었습니다."}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"팀 편성 확정 오류: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.post("/{meeting_id}/teams/{team_id}/members", response_model=TeamMemberResponse)
async def add_team_member_workflow(meeting_id: int,
                                   team_id: int,
                                   member_data: TeamMemberAddRequest = Body(...),
                                   db: Session = Depends(get_db),
                                   current_user: dict = Depends(get_current_active_user)):
    """팀 멤버 추가 (POST /meetings/... 경로)"""
    from routers.meetings.types.rounds import add_team_member
    return await add_team_member(meeting_id, team_id, member_data, current_user, db)


@router.post("/{meeting_id}/teams/{team_id}/members/{member_id}/confirm")
async def confirm_team_member(meeting_id: int,
                              team_id: int,
                              member_id: int,
                              db: Session = Depends(get_db),
                              current_user: dict = Depends(get_current_active_user)):
    """팀 편성 확인 완료 (참가자 본인만 가능)"""
    try:
        # 팀 멤버 조회
        team_member = db.query(TeamMember).join(Team).filter(TeamMember.id == member_id, Team.id == team_id,
                                                             Team.meeting_id == meeting_id).first()

        if not team_member:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="팀 멤버를 찾을 수 없습니다.")

        # 참가자 확인 (user_id를 통해 조회)
        participant = db.query(MeetingParticipant).filter(
            MeetingParticipant.meeting_id == meeting_id,
            or_((MeetingParticipant.user_id == team_member.user_id) if team_member.user_id else False,
                (MeetingParticipant.guest_id == team_member.guest_id) if team_member.guest_id else False)).first()

        if not participant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="참가자를 찾을 수 없습니다.")

        if participant.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="본인의 팀 편성만 확인할 수 있습니다.")

        # 확인 완료 처리
        team_member.confirmation_checked = True
        db.commit()

        return {"message": "팀 편성 확인이 완료되었습니다."}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"팀 편성 확인 오류: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


# =============================================================================
# 모임 진행 시작 API
# =============================================================================


@router.post("/{meeting_id}/start-rounding")
async def start_rounding(meeting_id: int,
                         db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_active_user)):
    """모임 진행 시작 (매니저/리더만 가능)"""
    try:
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # 매니저/리더 권한 확인
        from utils.permissions import is_meeting_organizer_or_manager
        if not is_meeting_organizer_or_manager(meeting_id, current_user.id, db):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="매니저/리더만 모임을 진행할 수 있습니다.")

        # 팀 편성 확정 확인
        if not meeting.team_formation_confirmed_at:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="팀 편성이 확정되지 않았습니다.")

        # 이미 진행 중인지 확인
        if meeting.rounding_started_at:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 모임이 진행 중입니다.")

        # 모임 진행 시작
        meeting.rounding_started_at = get_kst_now()
        db.commit()
        db.refresh(meeting)

        logger.info(f"모임 {meeting_id} 진행 시작: rounding_started_at = {meeting.rounding_started_at}")

        return {"message": "모임 진행이 시작되었습니다.", "rounding_started_at": meeting.rounding_started_at}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"모임 진행 시작 오류: {e}")
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.post("/{meeting_id}/complete-rounding")
async def complete_rounding(meeting_id: int,
                            db: Session = Depends(get_db),
                            current_user: User = Depends(get_current_active_user)):
    """라운딩 종료 (매니저/리더만 가능)"""
    try:
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # 라운딩 모임 확인
        if meeting.meeting_type != MeetingType.ROUND:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라운딩 모임이 아닙니다.")

        # 매니저/리더 권한 확인
        from utils.permissions import is_meeting_organizer_or_manager
        if not is_meeting_organizer_or_manager(meeting_id, current_user.id, db):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="매니저/리더만 라운딩을 종료할 수 있습니다.")

        if sync_rounding_completion_if_due(meeting):
            db.commit()
            db.refresh(meeting)

        # 이미 종료되었는지 확인
        if meeting.rounding_completed_at:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 라운딩이 종료되었습니다.")

        if meeting.meeting_time and not is_meeting_time_started(meeting.meeting_time):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="티업시간 전에는 라운딩을 종료할 수 없습니다.")

        if not meeting.meeting_time and not meeting.rounding_started_at:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="모임이 진행 중이 아닙니다.")

        if not meeting.rounding_started_at:
            meeting.rounding_started_at = meeting.meeting_time or datetime.now()

        # 라운딩 종료
        meeting.rounding_completed_at = get_kst_now()
        meeting.is_completed = True
        meeting.status = MeetingStatus.COMPLETED
        db.commit()
        db.refresh(meeting)

        logger.info(f"라운딩 {meeting_id} 종료: rounding_completed_at = {meeting.rounding_completed_at}")

        # 라운딩 종료 알림 전송
        try:
            await send_rounding_completed_notification(meeting_id, db)
        except Exception as e:
            logger.error(f"라운딩 종료 알림 전송 실패: {str(e)}", exc_info=True)

        return {"message": "라운딩이 종료되었습니다.", "rounding_completed_at": meeting.rounding_completed_at}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"라운딩 종료 오류: {e}")
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


# =============================================================================
# 모임 완료 및 정산 관리 API
# =============================================================================


@router.post("/{meeting_id}/complete")
async def complete_meeting(meeting_id: int,
                           db: Session = Depends(get_db),
                           current_user: dict = Depends(get_current_active_user)):
    """모임 완료 체크 (매니저/리더만 가능)"""
    try:
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # 매니저/리더 권한 확인 (개설자 또는 리더/매니저)
        from utils.permissions import is_meeting_organizer_or_manager
        if not is_meeting_organizer_or_manager(meeting_id, current_user.id, db):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="매니저/리더만 모임을 완료 처리할 수 있습니다.")

        # 모임 완료 처리
        meeting.is_completed = True
        meeting.status = MeetingStatus.COMPLETED
        db.commit()

        # 모든 참가자 스코어 히스토리 저장 및 핸디캡 자동 업데이트
        try:
            result = process_meeting_completion(db, meeting_id, score_count=5)
            logger.info(f"모임 완료 시 스코어 처리 결과: {result}")
        except Exception as e:
            # 스코어 처리 실패해도 모임 완료는 성공으로 처리
            logger.error(f"모임 완료 시 스코어 처리 실패: {str(e)}")

        # 모임 완료 알림 전송
        try:
            await send_meeting_completed_notification(meeting_id, db)
        except Exception as e:
            logger.error(f"모임 완료 알림 전송 실패: {str(e)}")

        return {"message": "모임이 완료 처리되었습니다."}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"모임 완료 처리 오류: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.get("/{meeting_id}/results")
async def get_meeting_results(meeting_id: int,
                              db: Session = Depends(get_db),
                              current_user: dict = Depends(get_current_user)):
    """모임별 전체 성적 조회"""
    try:
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # MeetingResult와 UserScoreHistory 조인 조회 (순위 순으로 정렬)
        from models import UserScoreHistory
        from sqlalchemy import asc
        results = db.query(MeetingResult, UserScoreHistory).join(
            UserScoreHistory, (UserScoreHistory.meeting_id == MeetingResult.meeting_id) &
            (UserScoreHistory.user_id == MeetingResult.user_id)).filter(
                MeetingResult.meeting_id == meeting_id).order_by(asc(MeetingResult.rank),
                                                                 asc(UserScoreHistory.net_score)).all()

        # 사용자 정보 포함하여 응답 생성
        result_list = []
        for result, score_history in results:
            user = db.query(User).filter(User.id == result.user_id).first()
            result_list.append({
                "id": result.id,
                "meeting_id": result.meeting_id,
                "user_id": result.user_id,
                "user_nickname": user.nickname if user else None,
                "gross_score": score_history.gross_score,
                "net_score": float(score_history.net_score) if score_history.net_score else None,
                "rank": result.rank,
                "handicap_used": float(score_history.handicap_used) if score_history.handicap_used else None,
                "completed_at": result.completed_at.isoformat() if result.completed_at else None,
                "created_at": result.created_at.isoformat() if result.created_at else None
            })

        return {
            "meeting_id": meeting_id,
            "meeting_name": meeting.name,
            "total_participants": len(result_list),
            "results": result_list
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"모임 성적 조회 오류: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="모임 성적을 조회하는 중 오류가 발생했습니다.")


@router.post("/{meeting_id}/settlement/confirm")
async def confirm_settlement(meeting_id: int,
                             db: Session = Depends(get_db),
                             current_user: dict = Depends(get_current_active_user)):
    """정산 확정 (매니저/리더만 가능)"""
    try:
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # 매니저/리더 권한 확인 (주최자 또는 클럽 리더/매니저)
        from routers.meetings.settlement import can_manage_settlement, assert_club_settlement_api_allowed

        assert_club_settlement_api_allowed(meeting, db)

        if not can_manage_settlement(meeting_id, current_user.id, db):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="매니저/리더만 정산을 확정할 수 있습니다.")

        if meeting.meeting_type == MeetingType.ROUND:
            if sync_rounding_completion_if_due(meeting):
                db.commit()
                db.refresh(meeting)
            if not meeting.rounding_completed_at:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라운딩 종료 후 정산을 확정할 수 있습니다.")

        # 정산 확정 처리
        meeting.settlement_confirmed = True
        db.commit()
        db.refresh(meeting)  # meeting 객체 갱신

        # 정산 완료 알림 전송 (소셜모임과 라운딩 모임 구분)
        try:
            from utils.notification_service import create_social_settlement_completed_notification
            from models import Club

            # 클럽 정보 조회
            club = db.query(Club).filter(Club.id == meeting.club_id).first()
            club_name = club.name if club else "알 수 없는 클럽"

            # 소셜모임인 경우 별도 알림 전송
            if meeting.meeting_type == MeetingType.SOCIAL:
                # 참가자들 조회
                participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id,
                                                                   MeetingParticipant.user_id.isnot(None)).all()

                for participant in participants:
                    create_social_settlement_completed_notification(db=db,
                                                                    user_id=participant.user_id,
                                                                    meeting_name=meeting.name,
                                                                    club_name=club_name,
                                                                    meeting_id=meeting_id)
            else:
                # 라운딩 모임인 경우 정산 완료 알림 전송 (모임 종료 알림)
                await send_settlement_completed_notification(meeting_id, db)
        except Exception as e:
            logger.error(f"정산 완료 알림 전송 실패: {str(e)}", exc_info=True)

        return {"message": "정산이 확정되었습니다."}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"정산 확정 오류: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


# =============================================================================
# 알림 관련 헬퍼 함수
# =============================================================================


async def send_application_closed_notification(meeting_id: int,
                                               db: Session,
                                               is_early: bool = False,
                                               outcome: str | None = None,
                                               participant_count: int | None = None):
    """모집 마감 시 참가자들에게 알림 전송"""
    try:
        # 모임 정보 조회
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            logger.error(f"모임을 찾을 수 없습니다: {meeting_id}")
            return

        # 클럽 정보 조회
        club = db.query(Club).filter(Club.id == meeting.club_id).first()
        club_name = club.name if club else "알 수 없는 클럽"

        # 참가자들 조회
        participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id,
                                                           MeetingParticipant.user_id.isnot(None)).all()

        if not participants:
            logger.info(f"알림을 받을 참가자가 없습니다: {meeting_id}")
            return

        # 알림 메시지 생성
        # meeting_time 안전하게 포맷팅
        meeting_time_str = "미정"
        if meeting.meeting_time:
            try:
                if isinstance(meeting.meeting_time, datetime):
                    meeting_time_str = meeting.meeting_time.strftime('%Y년 %m월 %d일 %H:%M')
                else:
                    meeting_time_str = str(meeting.meeting_time)
            except Exception as e:
                logger.error(f"모임 시간 포맷팅 실패: {e}")
                meeting_time_str = str(meeting.meeting_time) if meeting.meeting_time else "미정"

        if is_early:
            if outcome == "AUTO_CANCELED":
                title = f"모임 자동 취소 - {meeting.name}"
                content = f"""
{club_name}의 모임 '{meeting.name}'이 참가 인원 미달로 자동 취소되었습니다.

📅 모임 일시: {meeting_time_str}
📍 장소: {meeting.location or meeting.venue_name or '미정'}

모임 준비에 도움 주신 모든 분들께 감사드립니다.
다음 모임에서 다시 만나요!
                """.strip()
            elif outcome == "TEAM_FORMATION_READY":
                title = f"모집 조기 마감 - {meeting.name}"
                content = f"""
{club_name}의 모임 '{meeting.name}' 모집이 조기 마감되었습니다.

📅 모임 일시: {meeting_time_str}
📍 장소: {meeting.location or meeting.venue_name or '미정'}
참가 인원: {participant_count or 0}명

참가자 기준으로 팀 편성을 준비해주세요.
모임 매니저는 팀 편성을 진행하고 참가자분들은 안내를 기다려주세요.
                """.strip()
            else:
                title = f"모집 조기 마감 - {meeting.name}"
                content = f"""
{club_name}의 모임 '{meeting.name}' 모집이 조기 마감되었습니다.

📅 모임 일시: {meeting_time_str}
📍 장소: {meeting.location or meeting.venue_name or '미정'}

모집이 조기 마감되어 더 이상 신청을 받지 않습니다.
참가자들은 모임 준비를 해주세요!
                """.strip()
        else:
            title = f"모집 마감 - {meeting.name}"
            content = f"""
{club_name}의 모임 '{meeting.name}' 모집이 마감되었습니다.

📅 모임 일시: {meeting_time_str}
📍 장소: {meeting.location or meeting.venue_name or '미정'}

모집 마감으로 인해 더 이상 신청을 받지 않습니다.
참가자들은 모임 준비를 해주세요!
            """.strip()

        # 각 참가자에게 알림 전송
        sent_count = 0
        for participant in participants:
            try:
                notification = Notification(
                    user_id=participant.user_id,
                    type=NotificationType.MEETING_CANCELLATION.value,  # 모집 마감 알림으로 사용
                    title=title,
                    content=content,
                    status=NotificationStatus.UNREAD.value)

                db.add(notification)
                send_push_to_user(db=db,
                                  user_id=participant.user_id,
                                  title=notification.title,
                                  content=notification.content,
                                  category="meeting",
                                  target_id=meeting_id,
                                  extra_data={"meeting_type": meeting.meeting_type.value.lower()})
                sent_count += 1

            except Exception as e:
                logger.error(f"모집 마감 알림 생성 실패 - user_id: {participant.user_id}, error: {str(e)}", exc_info=True)
                continue

        if sent_count > 0:
            try:
                db.commit()
                logger.info(f"모집 마감 알림 전송 완료 - meeting_id: {meeting_id}, sent_count: {sent_count}")
            except Exception as commit_error:
                logger.error(f"모집 마감 알림 커밋 실패: {commit_error}", exc_info=True)
                db.rollback()
                raise
        else:
            logger.info(f"모집 마감 알림 전송 건수 없음 - meeting_id: {meeting_id}")

    except Exception as e:
        logger.error(f"모집 마감 알림 전송 오류: {str(e)}", exc_info=True)
        try:
            db.rollback()
        except Exception as rollback_error:
            logger.error(f"롤백 실패: {rollback_error}")


async def send_settlement_completed_notification(meeting_id: int, db: Session):
    """정산 완료 시 참가자들에게 알림 전송"""
    try:
        # 모임 정보 조회
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            logger.error(f"모임을 찾을 수 없습니다: {meeting_id}")
            return

        # 클럽 정보 조회
        club = db.query(Club).filter(Club.id == meeting.club_id).first()
        club_name = club.name if club else "알 수 없는 클럽"

        # 참가자들 조회
        participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id,
                                                           MeetingParticipant.user_id.isnot(None)).all()

        if not participants:
            logger.info(f"알림을 받을 참가자가 없습니다: {meeting_id}")
            return

        # 알림 메시지 생성 (모임 종료 알림)
        title = f"모임 종료 - {meeting.name}"
        meeting_time_str = meeting.meeting_time.strftime('%Y년 %m월 %d일 %H:%M') if meeting.meeting_time else '미정'
        content = f"""
{club_name}의 라운딩 모임 '{meeting.name}'이 종료되었습니다!

📅 모임 일시: {meeting_time_str}
📍 장소: {meeting.location or meeting.venue_name or '미정'}

정산이 완료되어 각자의 부담금이 확정되었습니다.
정산 내역을 확인해보세요!
        """.strip()

        # 각 참가자에게 알림 전송
        sent_count = 0
        for participant in participants:
            try:
                notification = Notification(user_id=participant.user_id,
                                            type=NotificationType.MEETING_SETTLEMENT_COMPLETED.value,
                                            title=title,
                                            content=content,
                                            status=NotificationStatus.UNREAD.value)

                db.add(notification)
                send_push_to_user(db=db,
                                  user_id=participant.user_id,
                                  title=notification.title,
                                  content=notification.content,
                                  category="meeting",
                                  target_id=meeting_id,
                                  extra_data={"meeting_type": meeting.meeting_type.value.lower()})
                sent_count += 1

            except Exception as e:
                logger.error(f"정산 완료 알림 생성 실패 - user_id: {participant.user_id}, error: {str(e)}")
                continue

        db.commit()
        logger.info(f"정산 완료 알림 전송 완료 - meeting_id: {meeting_id}, sent_count: {sent_count}")

    except Exception as e:
        logger.error(f"정산 완료 알림 전송 오류: {str(e)}")
        db.rollback()


async def send_team_formation_completed_notification(meeting_id: int, db: Session):
    """팀 편성 완료 시 참가자들에게 알림 전송"""
    try:
        # 모임 정보 조회
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            logger.error(f"모임을 찾을 수 없습니다: {meeting_id}")
            return

        # 클럽 정보 조회
        club = db.query(Club).filter(Club.id == meeting.club_id).first()
        club_name = club.name if club else "알 수 없는 클럽"

        # 참가자들 조회 (게스트 제외)
        participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id,
                                                           MeetingParticipant.user_id.isnot(None)).all()

        logger.info(f"팀 편성 완료 알림 - meeting_id: {meeting_id}, 참가자 수: {len(participants)}")

        if not participants:
            logger.warning(f"알림을 받을 참가자가 없습니다: {meeting_id}")
            return

        # 알림 메시지 생성
        title = f"팀 편성이 완료되었습니다"
        content = f"""
{club_name}의 라운딩 모임 '{meeting.name}' 팀 편성이 완료되었습니다.

📅 모임 일시: {meeting.meeting_time.strftime('%Y년 %m월 %d일 %H:%M') if meeting.meeting_time else '미정'}
📍 장소: {meeting.location or meeting.venue_name or '미정'}

팀 편성 결과를 확인해보세요!
        """.strip()

        # 각 참가자에게 알림 전송
        sent_count = 0
        for participant in participants:
            # user_id가 없는 경우 건너뛰기
            if not participant.user_id:
                logger.warning(f"user_id가 없는 참가자 건너뛰기 - participant_id: {participant.id}, meeting_id: {meeting_id}")
                continue

            try:
                notification = Notification(user_id=participant.user_id,
                                            type=NotificationType.TEAM_FORMATION_COMPLETED.value,
                                            title=title,
                                            content=content,
                                            status=NotificationStatus.UNREAD.value)

                db.add(notification)
                send_push_to_user(db=db,
                                  user_id=participant.user_id,
                                  title=notification.title,
                                  content=notification.content,
                                  category="meeting",
                                  target_id=meeting_id,
                                  extra_data={"meeting_type": meeting.meeting_type.value.lower()})
                sent_count += 1
                logger.debug(f"팀 편성 완료 알림 추가 - user_id: {participant.user_id}, meeting_id: {meeting_id}")

            except Exception as e:
                logger.error(f"팀 편성 완료 알림 생성 실패 - user_id: {participant.user_id}, error: {str(e)}", exc_info=True)
                continue

        if sent_count > 0:
            try:
                db.commit()
                logger.info(f"팀 편성 완료 알림 전송 완료 - meeting_id: {meeting_id}, sent_count: {sent_count}")
            except Exception as commit_error:
                logger.error(f"팀 편성 완료 알림 커밋 실패: {commit_error}", exc_info=True)
                db.rollback()
                raise
        else:
            logger.warning(f"팀 편성 완료 알림 전송 건수 없음 - meeting_id: {meeting_id}, 참가자 수: {len(participants)}")

    except Exception as e:
        logger.error(f"팀 편성 완료 알림 전송 오류: {str(e)}", exc_info=True)
        try:
            db.rollback()
        except Exception as rollback_error:
            logger.error(f"롤백 실패: {rollback_error}")


async def send_rounding_completed_notification(meeting_id: int, db: Session):
    """라운딩 종료 시 참가자들에게 알림 전송"""
    try:
        # 모임 정보 조회
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            logger.error(f"모임을 찾을 수 없습니다: {meeting_id}")
            return

        # 클럽 정보 조회
        club = db.query(Club).filter(Club.id == meeting.club_id).first()
        club_name = club.name if club else "알 수 없는 클럽"

        # 참가자들 조회
        participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id,
                                                           MeetingParticipant.user_id.isnot(None)).all()

        if not participants:
            logger.info(f"알림을 받을 참가자가 없습니다: {meeting_id}")
            return

        # 알림 메시지 생성
        title = f"라운딩 종료 - {meeting.name}"
        content = f"""
{club_name}의 라운딩 모임 '{meeting.name}'이 종료되었습니다!

📅 모임 일시: {meeting.meeting_time.strftime('%Y년 %m월 %d일 %H:%M') if meeting.meeting_time else '미정'}
📍 장소: {meeting.location or meeting.venue_name or '미정'}

라운딩이 성공적으로 종료되었습니다.
정산 및 후속 처리가 진행될 예정입니다.

수고하셨습니다!
        """.strip()

        # 각 참가자에게 알림 전송
        sent_count = 0
        for participant in participants:
            try:
                notification = Notification(user_id=participant.user_id,
                                            type=NotificationType.MEETING_COMPLETED.value,
                                            title=title,
                                            content=content,
                                            status=NotificationStatus.UNREAD.value)

                db.add(notification)
                send_push_to_user(db=db,
                                  user_id=participant.user_id,
                                  title=notification.title,
                                  content=notification.content,
                                  category="meeting",
                                  target_id=meeting_id,
                                  extra_data={"meeting_type": meeting.meeting_type.value.lower()})
                sent_count += 1

            except Exception as e:
                logger.error(f"라운딩 종료 알림 생성 실패 - user_id: {participant.user_id}, error: {str(e)}")
                continue

        db.commit()
        logger.info(f"라운딩 종료 알림 전송 완료 - meeting_id: {meeting_id}, sent_count: {sent_count}")

    except Exception as e:
        logger.error(f"라운딩 종료 알림 전송 오류: {str(e)}")
        db.rollback()


async def send_meeting_completed_notification(meeting_id: int, db: Session):
    """모임 완료 시 참가자들에게 알림 전송"""
    try:
        # 모임 정보 조회
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            logger.error(f"모임을 찾을 수 없습니다: {meeting_id}")
            return

        # 클럽 정보 조회
        club = db.query(Club).filter(Club.id == meeting.club_id).first()
        club_name = club.name if club else "알 수 없는 클럽"

        # 참가자들 조회
        participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id,
                                                           MeetingParticipant.user_id.isnot(None)).all()

        if not participants:
            logger.info(f"알림을 받을 참가자가 없습니다: {meeting_id}")
            return

        # 알림 메시지 생성
        title = f"모임 완료 - {meeting.name}"
        content = f"""
{club_name}의 모임 '{meeting.name}'이 완료되었습니다!

📅 모임 일시: {meeting.meeting_time.strftime('%Y년 %m월 %d일 %H:%M')}
📍 장소: {meeting.location or meeting.venue_name or '미정'}

모임이 성공적으로 완료되었습니다.
정산 및 후속 처리가 진행될 예정입니다.

수고하셨습니다!
        """.strip()

        # 각 참가자에게 알림 전송
        sent_count = 0
        for participant in participants:
            try:
                notification = Notification(user_id=participant.user_id,
                                            type=NotificationType.MEETING_COMPLETED.value,
                                            title=title,
                                            content=content,
                                            status=NotificationStatus.UNREAD.value)

                db.add(notification)
                send_push_to_user(db=db,
                                  user_id=participant.user_id,
                                  title=notification.title,
                                  content=notification.content,
                                  category="meeting",
                                  target_id=meeting_id,
                                  extra_data={"meeting_type": meeting.meeting_type.value.lower()})
                sent_count += 1

            except Exception as e:
                logger.error(f"모임 완료 알림 생성 실패 - user_id: {participant.user_id}, error: {str(e)}")
                continue

        db.commit()
        logger.info(f"모임 완료 알림 전송 완료 - meeting_id: {meeting_id}, sent_count: {sent_count}")

    except Exception as e:
        logger.error(f"모임 완료 알림 전송 오류: {str(e)}")
        db.rollback()
