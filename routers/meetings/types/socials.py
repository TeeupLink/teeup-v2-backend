"""
소셜 모임 관리 API - SOCIAL 타입 전용
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from utils.content_filter import reject_banned_content
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, case
from datetime import date, datetime, time
from utils import activity
from utils.datetime_utils import get_kst_now
import logging

from database import get_db
from models import (
    User, Club, ClubMembership, Meeting, MeetingParticipant, ParticipantType, Guest
)
from models.enums import MeetingType as DbMeetingType
from schemas import (
    MeetingType, MeetingStatus, ClubRole
)
from schemas import (
    SocialMeetingCreate, MeetingUpdate, MeetingResponse,
    MeetingParticipantResponse, PaginatedResponse, MessageResponse
)
from routers.auth import get_current_active_user, get_current_user, get_current_user_allow_both
from utils.permissions import MEMBERSHIP_ACTIVE_STATUSES
from utils.club_flags import club_settlement_enabled
from utils.notification_service import notify_organizer_participant_added_after_recruitment_closed
from routers.meetings.workflow import close_meetings_with_passed_deadline

router = APIRouter(prefix="/socials", tags=["소셜 모임 관리"])
logger = logging.getLogger(__name__)


def _meeting_to_response(meeting: Meeting, club: Club, participant_count: int) -> MeetingResponse:
    """ORM 행 → MeetingResponse. SQLAlchemy 2.x는 __dict__에 컬럼이 없을 수 있어 테이블 메타데이터로 수집."""
    d = {col.key: getattr(meeting, col.key) for col in Meeting.__table__.columns}
    d["tee_times"] = meeting.tee_times or []
    st = getattr(meeting, "social_type", None)
    d["type"] = st.value if st is not None and hasattr(st, "value") else (st if isinstance(st, str) else None)
    for key in ("meeting_type", "meeting_subtype", "settlement_method"):
        v = d.get(key)
        if v is not None and hasattr(v, "value"):
            d[key] = v.value
    sv = d.get("status")
    if sv is not None and hasattr(sv, "value"):
        d["status"] = sv.value
    return MeetingResponse(
        **d,
        club_name=club.name,
        participant_count=participant_count,
        settlement_enabled=club_settlement_enabled(club),
    )


# =============================================================================
# 소셜 모임 목록 조회
# =============================================================================

@router.get("/", response_model=PaginatedResponse[MeetingResponse])
async def get_socials(
    page: int = Query(1, ge=1, description="페이지 번호"),
    limit: int = Query(10, ge=1, le=100, description="페이지당 항목 수"),
    status_group: str | None = Query(None, description="상태 그룹(active|completed)"),
    search: str | None = Query(None, description="검색어"),
    start_date: date | None = Query(None, description="시작일(YYYY-MM-DD)"),
    end_date: date | None = Query(None, description="종료일(YYYY-MM-DD)"),
    current_user: User = Depends(get_current_user_allow_both),
    db: Session = Depends(get_db)
):
    """소셜 모임 목록 조회 - 모든 소셜 노출(완료/취소 포함). 프라이빗은 참가한 모임에서만 노출."""

    # 마감일 지난 모임을 IN_PROGRESS로 갱신 (크론 없이 목록 조회 시 반영)
    try:
        close_meetings_with_passed_deadline(db)
    except Exception as e:
        logger.warning(f"마감일 경과 자동 갱신 스킵: {e}")

    # 프라이빗 소셜: 목록에는 참가자/생성자/클럽 리더·매니저만 노출
    user_participant_meeting_ids = db.query(
        MeetingParticipant.meeting_id
    ).filter(MeetingParticipant.user_id == current_user.id).subquery()
    user_created_meeting_ids = db.query(Meeting.id).filter(
        Meeting.created_by == current_user.id
    ).subquery()
    manager_club_ids = db.query(ClubMembership.club_id).filter(
        and_(
            ClubMembership.user_id == current_user.id,
            ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES),
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER]),
        )
    ).subquery()

    # 모든 소셜(클럽 무관) 중: 일반 소셜이거나, 프라이빗이면 참가/생성/매니저인 경우만
    query = db.query(Meeting).join(Club).filter(
        and_(
            Meeting.meeting_type == MeetingType.SOCIAL,
            or_(
                Meeting.is_private == False,
                and_(
                    Meeting.is_private == True,
                    or_(
                        Meeting.id.in_(user_participant_meeting_ids),
                        Meeting.id.in_(user_created_meeting_ids),
                        Meeting.club_id.in_(manager_club_ids),
                    ),
                ),
            ),
        )
    )
    
    # 상태 필터
    if status_group == "active":
        query = query.filter(Meeting.status.in_([MeetingStatus.SCHEDULED, MeetingStatus.IN_PROGRESS]))
    elif status_group == "completed":
        query = query.filter(Meeting.status.in_([MeetingStatus.COMPLETED, MeetingStatus.CANCELED]))

    # 검색 필터
    if search:
        search_filter = or_(
            Meeting.name.contains(search),
            Meeting.venue_name.contains(search),
            Meeting.location.contains(search)
        )
        query = query.filter(search_filter)
    if start_date:
        query = query.filter(Meeting.meeting_time >= datetime.combine(start_date, time.min))
    if end_date:
        query = query.filter(Meeting.meeting_time <= datetime.combine(end_date, time.max))
    
    # 총 개수
    total = query.count()
    
    # 정렬: 상태별 우선순위 (SCHEDULED → IN_PROGRESS → COMPLETED)
    # CANCELED는 999로 설정하여 맨 뒤로
    # 동일 상태 내에서는 최신 모임부터 (created_at 내림차순)
    status_order = case(
        (Meeting.status == MeetingStatus.SCHEDULED, 1),
        (Meeting.status == MeetingStatus.IN_PROGRESS, 2),
        (Meeting.status == MeetingStatus.COMPLETED, 3),
        (Meeting.status == MeetingStatus.CANCELED, 999),
        else_=5
    )
    query = query.order_by(status_order.asc(), Meeting.created_at.desc())
    
    # 페이지네이션
    meetings = query.offset((page - 1) * limit).limit(limit).all()
    
    # 응답 데이터 구성
    meeting_responses = []
    for meeting in meetings:
        participant_count = db.query(MeetingParticipant).filter(
            MeetingParticipant.meeting_id == meeting.id
        ).count()
        
        meeting_responses.append(
            _meeting_to_response(meeting, meeting.club, participant_count)
        )
    
    return PaginatedResponse(
        data=meeting_responses,
        total=total,
        page=page,
        limit=limit,
        total_pages=(total + limit - 1) // limit
    )

# =============================================================================
# 소셜 모임 생성
# =============================================================================

@router.post("/", response_model=MeetingResponse, dependencies=[Depends(reject_banned_content)])
async def create_social(
    meeting_data: SocialMeetingCreate,
    current_user: User = Depends(get_current_user_allow_both),
    db: Session = Depends(get_db)
):
    """소셜 모임 생성 (리더/매니저만 가능)"""
    
    # 클럽 멤버십 확인
    membership = db.query(ClubMembership).filter(
        and_(
            ClubMembership.user_id == current_user.id,
            ClubMembership.club_id == meeting_data.club_id,
            ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES)
        )
    ).first()
    
    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="해당 클럽의 멤버가 아닙니다."
        )

    if membership.role not in [ClubRole.LEADER, ClubRole.MANAGER]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="클럽 리더/매니저만 소셜 모임을 생성할 수 있습니다."
        )
    
    # 모임 생성
    from models.enums import SettlementMethod as ModelSettlementMethod, SocialType as ModelSocialType
    settlement_val = meeting_data.settlement_method
    if settlement_val is not None:
        v = settlement_val.value if hasattr(settlement_val, "value") else settlement_val
        settlement_method = ModelSettlementMethod(v)
    else:
        settlement_method = None
    type_val = getattr(meeting_data, "type", None)
    if type_val is not None:
        t_str = type_val.value if hasattr(type_val, "value") else type_val
        social_type = ModelSocialType(t_str)
    else:
        social_type = None
    meeting = Meeting(
        name=meeting_data.name,
        description=meeting_data.description,
        meeting_time=meeting_data.meeting_time,
        application_deadline=meeting_data.application_deadline,
        max_participants=meeting_data.max_participants,
        meeting_type=DbMeetingType.SOCIAL,
        venue_name=meeting_data.venue_name,
        location=meeting_data.venue_name,
        social_cost=meeting_data.social_cost,
        settlement_method=settlement_method,
        social_notes=meeting_data.social_notes,
        social_type=social_type,
        club_id=meeting_data.club_id,
        status=MeetingStatus.SCHEDULED.value,
        tee_times=[],
        created_by=current_user.id
    )
    
    db.add(meeting)
    db.commit()
    db.refresh(meeting)
    
    # 생성자를 참가자로 자동 추가
    participant = MeetingParticipant(
        meeting_id=meeting.id,
        user_id=current_user.id,
        participant_type=ParticipantType.USER
    )
    db.add(participant)
    db.commit()
    
    # 클럽 정보 조회
    club = db.query(Club).filter(Club.id == meeting.club_id).first()
    
    # 클럽 멤버들에게 새 소셜모임 등록 알림 전송
    try:
        from utils.notification_service import create_social_notification
        
        # 클럽의 모든 멤버 조회 (생성자 제외)
        club_members = db.query(ClubMembership).filter(
            ClubMembership.club_id == meeting_data.club_id,
            ClubMembership.user_id != current_user.id,  # 생성자 제외
            ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES)  # 승인된 멤버만
        ).all()
        
        for member in club_members:
            create_social_notification(
                db=db,
                user_id=member.user_id,
                social_name=meeting_data.name,
                club_name=club.name,
                notification_type="CREATED",
                meeting_id=meeting.id
            )
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"소셜모임 등록 알림 전송 실패: {str(e)}")
    
    return _meeting_to_response(meeting, club, 1)

# =============================================================================
# 소셜 모임 상세 조회
# =============================================================================

@router.get("/{meeting_id}", response_model=MeetingResponse)
async def get_social(
    meeting_id: int,
    current_user: User = Depends(get_current_user_allow_both),
    db: Session = Depends(get_db)
):
    """소셜 모임 상세 조회"""
    
    # 모임 조회
    meeting = db.query(Meeting).join(Club).filter(
        and_(
            Meeting.id == meeting_id,
            Meeting.meeting_type == MeetingType.SOCIAL
        )
    ).first()
    
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="소셜 모임을 찾을 수 없습니다."
        )
    
    # 클럽 멤버십 확인 (관리자는 제외)
    from models import Admin
    admin = db.query(Admin).filter(
        Admin.id == current_user.id,
        Admin.deleted_at.is_(None)
    ).first()
    
    if not admin:
        membership = db.query(ClubMembership).filter(
            and_(
                ClubMembership.user_id == current_user.id,
                ClubMembership.club_id == meeting.club_id,
                ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES)
            )
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="해당 클럽의 멤버가 아닙니다."
            )
    
    # 참가자 수 조회
    participant_count = db.query(MeetingParticipant).filter(
        MeetingParticipant.meeting_id == meeting.id
    ).count()
    
    return _meeting_to_response(meeting, meeting.club, participant_count)

# =============================================================================
# 소셜 모임 수정
# =============================================================================

@router.put("/{meeting_id}", response_model=MeetingResponse, dependencies=[Depends(reject_banned_content)])
async def update_social(
    meeting_id: int,
    meeting_data: MeetingUpdate,
    current_user: User = Depends(get_current_user_allow_both),
    db: Session = Depends(get_db)
):
    """소셜 모임 수정 (매니저만 가능)"""
    
    # 모임 조회
    meeting = db.query(Meeting).filter(
        and_(
            Meeting.id == meeting_id,
            Meeting.meeting_type == MeetingType.SOCIAL
        )
    ).first()
    
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="소셜 모임을 찾을 수 없습니다."
        )
    
    # 매니저 권한 확인 (개설자 또는 리더/매니저)
    from utils.permissions import is_meeting_organizer_or_manager
    if not is_meeting_organizer_or_manager(meeting_id, current_user.id, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="소셜 모임 매니저만 수정할 수 있습니다."
        )
    
    # 수정 가능한 필드들만 업데이트 (Enum 컬럼에는 모델 Enum 인스턴스로 저장)
    from models.enums import SettlementMethod as ModelSettlementMethod
    update_data = meeting_data.dict(exclude_unset=True)
    for field, value in update_data.items():
        if value is None:
            if field == "settlement_method":
                setattr(meeting, "settlement_method", None)
            else:
                setattr(meeting, field, None)
            continue
        if field == "settlement_method":
            val_str = value.value if hasattr(value, "value") else value
            try:
                setattr(meeting, "settlement_method", ModelSettlementMethod(val_str))
            except (ValueError, TypeError):
                setattr(meeting, "settlement_method", value)
            continue
        if field == "type":
            if value is None:
                setattr(meeting, "social_type", None)
            else:
                from models.enums import SocialType as ModelSocialType
                t_str = value.value if hasattr(value, "value") else value
                try:
                    setattr(meeting, "social_type", ModelSocialType(t_str))
                except (ValueError, TypeError):
                    setattr(meeting, "social_type", None)
            continue
        if hasattr(value, "value"):
            value = value.value
        setattr(meeting, field, value)
    
    meeting.updated_at = get_kst_now()
    
    db.commit()
    db.refresh(meeting)
    
    # 클럽 정보 조회
    club = db.query(Club).filter(Club.id == meeting.club_id).first()
    
    # 참가자 수 조회
    participant_count = db.query(MeetingParticipant).filter(
        MeetingParticipant.meeting_id == meeting.id
    ).count()
    
    return _meeting_to_response(meeting, club, participant_count)

# =============================================================================
# 소셜 모임 삭제
# =============================================================================

@router.delete("/{meeting_id}")
async def delete_social(
    meeting_id: int,
    current_user: User = Depends(get_current_user_allow_both),
    db: Session = Depends(get_db)
):
    """소셜 모임 삭제 (매니저만 가능)"""
    
    # 모임 조회
    meeting = db.query(Meeting).filter(
        and_(
            Meeting.id == meeting_id,
            Meeting.meeting_type == MeetingType.SOCIAL
        )
    ).first()
    
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="소셜 모임을 찾을 수 없습니다."
        )
    
    # 매니저 권한 확인 (개설자 또는 리더/매니저)
    from utils.permissions import is_meeting_organizer_or_manager
    if not is_meeting_organizer_or_manager(meeting_id, current_user.id, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="소셜 모임 매니저만 삭제할 수 있습니다."
        )
    
    db.delete(meeting)
    db.commit()
    
    return {"message": "소셜 모임이 삭제되었습니다."}

# =============================================================================
# 소셜 모임 참가자 목록 조회
# =============================================================================

@router.get("/{meeting_id}/participants")
async def get_social_participants(
    meeting_id: int,
    current_user: User = Depends(get_current_user_allow_both),
    db: Session = Depends(get_db)
):
    """소셜 모임 참가자 목록 조회 (하단 참가탭용)"""
    meeting = db.query(Meeting).filter(
        and_(
            Meeting.id == meeting_id,
            Meeting.meeting_type == MeetingType.SOCIAL
        )
    ).first()

    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="소셜 모임을 찾을 수 없습니다."
        )

    # 클럽 멤버십 확인
    membership = db.query(ClubMembership).filter(
        and_(
            ClubMembership.user_id == current_user.id,
            ClubMembership.club_id == meeting.club_id,
            ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES)
        )
    ).first()

    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="해당 클럽의 멤버가 아닙니다."
        )

    # 참가자 목록 조회 (게스트 포함)
    participants = db.query(MeetingParticipant).filter(
        MeetingParticipant.meeting_id == meeting_id
    ).all()

    result = []
    for p in participants:
        # MeetingParticipant 모델에 status/role 컬럼 없음 - 파생값 사용
        role = "ORGANIZER" if p.user_id and p.user_id == meeting.created_by else "PARTICIPANT"
        status_val = "CONFIRMED"
        club_role = None
        if p.user_id:
            m = db.query(ClubMembership).filter(
                ClubMembership.user_id == p.user_id,
                ClubMembership.club_id == meeting.club_id,
                ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES)
            ).first()
            if m and m.role:
                club_role = m.role.value if hasattr(m.role, "value") else str(m.role)

        if p.guest_id:
            guest = db.query(Guest).filter(Guest.id == p.guest_id).first()
            result.append({
                "id": p.id,
                "user_id": None,
                "guest_id": p.guest_id,
                "user_name": guest.name if guest else "게스트",
                "user_nickname": guest.name if guest else "게스트",
                "name": guest.name if guest else "게스트",
                "status": status_val,
                "role": "PARTICIPANT",
                "club_role": None,
                "membership_role": None,
                "is_guest": True,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            })
        else:
            user = db.query(User).filter(User.id == p.user_id).first() if p.user_id else None
            if user:
                result.append({
                    "id": p.id,
                    "user_id": p.user_id,
                    "guest_id": None,
                    "user_name": user.realname or user.nickname or "이름 없음",
                    "user_nickname": user.nickname or "닉네임 없음",
                    "name": user.realname or user.nickname or "이름 없음",
                    "status": status_val,
                    "role": role,
                    "club_role": club_role,
                    "membership_role": club_role,
                    "is_guest": False,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                })
    return result


# =============================================================================
# 소셜 모임 참가/탈퇴
# =============================================================================

@router.post("/{meeting_id}/join")
async def join_social(
    meeting_id: int,
    current_user: User = Depends(get_current_user_allow_both),
    db: Session = Depends(get_db)
):
    """소셜 모임 참가"""
    
    # 모임 조회
    meeting = db.query(Meeting).filter(
        and_(
            Meeting.id == meeting_id,
            Meeting.meeting_type == MeetingType.SOCIAL
        )
    ).first()
    
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="소셜 모임을 찾을 수 없습니다."
        )
    
    # 클럽 멤버십 확인
    membership = db.query(ClubMembership).filter(
        and_(
            ClubMembership.user_id == current_user.id,
            ClubMembership.club_id == meeting.club_id,
            ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES)
        )
    ).first()
    
    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="해당 클럽의 멤버가 아닙니다."
        )
    
    # 이미 참가했는지 확인
    existing_participant = db.query(MeetingParticipant).filter(
        and_(
            MeetingParticipant.meeting_id == meeting_id,
            MeetingParticipant.user_id == current_user.id
        )
    ).first()
    
    if existing_participant:
        # 이미 참가한 경우에도 200 (idempotent - 프론트 반복 클릭 대응)
        return {"message": "이미 참가한 소셜 모임입니다.", "already_joined": True}

    if meeting.application_deadline and datetime.now() > meeting.application_deadline:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="참가 신청 마감되었습니다."
        )

    if meeting.application_closed_early:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="참가 신청이 조기 마감되었습니다."
        )
    
    # 최대 참가자 수 확인 (max_participants가 null이면 제한 없음)
    if meeting.max_participants is not None:
        current_participants = db.query(MeetingParticipant).filter(
            MeetingParticipant.meeting_id == meeting_id
        ).count()
        
        if current_participants >= meeting.max_participants:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="소셜 모임 정원이 가득 찼습니다."
            )
    
    # 참가자 추가
    participant = MeetingParticipant(
        meeting_id=meeting_id,
        user_id=current_user.id,
        participant_type=ParticipantType.USER
    )
    
    db.add(participant)
    db.commit()

    try:
        notify_organizer_participant_added_after_recruitment_closed(
            db=db,
            meeting_id=meeting_id,
            participant_user_id=current_user.id,
        )
    except Exception as e:
        logger.error(f"모집 완료 단계 organizer 알림 전송 실패 - meeting_id: {meeting_id}, user_id: {current_user.id}, error: {str(e)}")
    
    activity.log(current_user, "MEETING_JOIN", f"{getattr(meeting, 'name', '')} 참가", target_type="MEETING", target_id=meeting_id)
    return {"message": "소셜 모임에 참가했습니다."}

@router.delete("/{meeting_id}/leave")
async def leave_social(
    meeting_id: int,
    current_user: User = Depends(get_current_user_allow_both),
    db: Session = Depends(get_db)
):
    """소셜 모임 탈퇴"""
    
    # 참가자 조회
    participant = db.query(MeetingParticipant).filter(
        and_(
            MeetingParticipant.meeting_id == meeting_id,
            MeetingParticipant.user_id == current_user.id
        )
    ).first()
    
    if not participant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="참가하지 않은 소셜 모임입니다."
        )
    
    # 생성자는 탈퇴 불가
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if meeting and meeting.created_by == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="소셜 모임 생성자는 탈퇴할 수 없습니다."
        )
    
    db.delete(participant)
    db.commit()
    
    activity.log(current_user, "MEETING_CANCEL", f"{getattr(meeting, 'name', '')} 참가 취소", target_type="MEETING", target_id=meeting_id)
    return {"message": "소셜 모임에서 탈퇴했습니다."}

# =============================================================================
# 소셜 모임 취소
# =============================================================================

@router.post("/{meeting_id}/cancel")
async def cancel_social(
    meeting_id: int,
    reason: str,
    current_user: User = Depends(get_current_user_allow_both),
    db: Session = Depends(get_db)
):
    """소셜 모임 취소 (매니저만 가능)"""
    
    # 모임 조회
    meeting = db.query(Meeting).filter(
        and_(
            Meeting.id == meeting_id,
            Meeting.meeting_type == MeetingType.SOCIAL
        )
    ).first()
    
    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="소셜 모임을 찾을 수 없습니다."
        )
    
    # 매니저 권한 확인 (모임 생성자 기준)
    if meeting.created_by != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="소셜 모임 매니저만 취소할 수 있습니다."
        )

    if meeting.status == MeetingStatus.IN_PROGRESS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="진행중인 소셜 모임은 취소할 수 없습니다."
        )
    
    # 모임 취소
    meeting.status = MeetingStatus.CANCELED
    meeting.cancel_reason = reason
    meeting.updated_at = get_kst_now()
    
    db.commit()
    
    return {"message": "소셜 모임이 취소되었습니다."}
