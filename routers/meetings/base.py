# 모임 관리 API들
from fastapi import APIRouter, Depends, HTTPException, status, Request, Query
from utils.content_filter import reject_banned_content
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import text, or_, and_, select
from decimal import Decimal
from datetime import date, datetime, time, timezone
from utils.datetime_utils import get_kst_now
import logging

logger = logging.getLogger(__name__)

from database import get_db
from models import (
    User, Club, ClubMembership, ClubRole, Meeting, MeetingParticipant, 
    Expense, Score,
    UserScoreHistory, MeetingResult, Guest, ParticipantType, ParticipantStatus, ParticipantRole,
    SettlementMethod as ModelSettlementMethod, MeetingType as ModelMeetingType, MeetingSubtype as ModelMeetingSubtype
)
from schemas import (
    MembershipStatus, MeetingType, MeetingStatus, MeetingParticipantStatus, MeetingParticipantRole
)
from schemas import (
    RoundingMeetingCreate, SocialMeetingCreate, MeetingUpdate, MeetingResponse,
    MeetingParticipantResponse, PaginatedResponse, MessageResponse,
    ExpenseCreate, ExpenseUpdate, ExpenseResponse, ExpenseListResponse,
    ExpenseParticipantResponse, ExpenseParticipantUpdate,
    GuestCreate, GuestUpdate, GuestResponse,
    ScoreCreate, ScoreUpdate, ScoreResponse, ScoreListResponse, ScoreStats,
    SimpleScoreCreate, SimpleScoreResponse
)
from routers.auth import get_current_user, get_current_active_user
# admin_auth는 JWT 기반으로 변경됨
from utils.permissions import MEMBERSHIP_ACTIVE_STATUSES
from utils.club_flags import club_settlement_enabled
from utils.cuid import generate_cuid
from utils.handicap_calculator import (check_all_holes_completed, process_participant_score,
                                       get_user_handicap_for_formation, save_score_to_history, update_user_handicap,
                                       create_meeting_results)
from utils.notification_service import notify_round_participants_status_changed

router = APIRouter(prefix="/meetings", tags=["모임 관리"])


@router.post("/", response_model=MeetingResponse, dependencies=[Depends(reject_banned_content)])
async def create_meeting(meeting_data: RoundingMeetingCreate,
                         club_id: str = Query(..., description="클럽 ID"),
                         db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_active_user)):
    """모임 생성 (클럽 리더/매니저만 가능)"""
    try:
        # 디버깅: 스키마 필드 확인
        print(f"DEBUG: RoundingMeetingCreate fields: {RoundingMeetingCreate.__fields__.keys()}")
        print(f"DEBUG: meeting_data: {meeting_data}")
        print(f"DEBUG: club_id: {club_id}")
        # 클럽 조회 (display_id로 먼저 조회)
        print(f"DEBUG: 클럽 조회 시작 - club_id: {club_id}")
        club = db.query(Club).filter(Club.display_id == club_id, Club.deleted_at.is_(None)).first()

        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(Club.id == club_id_int, Club.deleted_at.is_(None)).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None

        print(f"DEBUG: 클럽 조회 결과 - club: {club}")

        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다.")

        # 클럽 멤버 권한 확인
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == current_user.id,
            ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES),
        ).first()

        if not membership:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="해당 클럽의 활성 멤버가 아닙니다.")

        # 모임 생성은 리더/매니저만 가능
        if membership.role not in [ClubRole.LEADER, ClubRole.MANAGER]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="클럽 리더/매니저만 모임을 생성할 수 있습니다.")

        # 모임 생성
        meeting = Meeting(            name=meeting_data.name,
            description=meeting_data.description,
            location=meeting_data.location,
            meeting_time=meeting_data.meeting_time,
            application_deadline=meeting_data.application_deadline,
            tee_times=meeting_data.tee_times,
            max_participants=meeting_data.max_participants,
            meeting_type=ModelMeetingType(meeting_data.meeting_type.value),
            meeting_subtype=ModelMeetingSubtype(meeting_data.meeting_subtype.value) if meeting_data.meeting_subtype else None,
            total_cost=meeting_data.total_cost,
            green_fee=meeting_data.green_fee,
            caddy_fee=meeting_data.caddy_fee,
            cart_fee=meeting_data.cart_fee,
            settlement_method=ModelSettlementMethod(meeting_data.settlement_method.value),
            course_name=meeting_data.course_name,
            hole_count=meeting_data.hole_count,
            reservation_name=meeting_data.reservation_name,
            team_formation_mode=meeting_data.team_formation_mode,
            team_size=meeting_data.team_size,
            club_id=club.id,
            created_by=current_user.id
        )
        
        db.add(meeting)
        db.commit()
        db.refresh(meeting)

        # 디버깅: 저장된 미팅 데이터 확인
        print(f"DEBUG: 저장된 미팅 데이터:")
        print(f"  - tee_times: {meeting.tee_times}")
        print(f"  - meeting_subtype: {meeting.meeting_subtype}")
        print(f"  - total_cost: {meeting.total_cost}")
        print(f"  - green_fee: {meeting.green_fee}")
        print(f"  - caddy_fee: {meeting.caddy_fee}")
        print(f"  - cart_fee: {meeting.cart_fee}")
        print(f"  - settlement_method: {meeting.settlement_method}")
        print(f"  - reservation_name: {meeting.reservation_name}")

        # 모임 생성자를 참가자로 추가 (승인/역할 없이 즉시 참가)
        participant = MeetingParticipant(meeting_id=meeting.id,
                                         user_id=current_user.id,
                                         participant_type=ParticipantType.USER)

        db.add(participant)
        db.commit()

        # 클럽 멤버들에게 새 모임 등록 알림 전송
        try:
            from utils.notification_service import create_meeting_notification

            # 클럽의 모든 멤버 조회 (생성자 제외)
            club_members = db.query(ClubMembership).filter(
                ClubMembership.club_id == club.id,
                ClubMembership.user_id != current_user.id  # 생성자 제외
            ).all()

            for member in club_members:
                create_meeting_notification(db=db,
                                            user_id=member.user_id,
                                            meeting_name=meeting_data.name,
                                            club_name=club.name,
                                            notification_type="CREATED",
                                            meeting_id=meeting.id)
        except Exception as e:
            logger.error(f"모임 등록 알림 전송 실패: {str(e)}")

        # 응답 데이터 구성
        # Meeting 객체를 다시 refresh하여 최신 상태로 업데이트
        db.refresh(meeting)
        
        participant_count = db.query(MeetingParticipant).filter(
            MeetingParticipant.meeting_id == meeting.id,
            MeetingParticipant.status == MeetingParticipantStatus.CONFIRMED
        ).count()
        
        return MeetingResponse(
            id=meeting.id,            name=meeting.name,
            description=meeting.description,
            location=meeting.location,
            meeting_time=meeting.meeting_time,
            application_deadline=meeting.application_deadline,
            tee_times=meeting.tee_times,
            max_participants=meeting.max_participants,
            meeting_type=meeting.meeting_type,
            meeting_subtype=meeting.meeting_subtype,
            team_formation_mode=meeting.team_formation_mode,
            team_size=meeting.team_size,
            total_cost=meeting.total_cost,
            green_fee=meeting.green_fee,
            caddy_fee=meeting.caddy_fee,
            cart_fee=meeting.cart_fee,
            settlement_method=meeting.settlement_method.value if meeting.settlement_method else None,
            course_name=meeting.course_name,
            hole_count=meeting.hole_count,
            reservation_name=meeting.reservation_name,
            venue_name=meeting.venue_name,
            status=meeting.status,
            cancel_reason=meeting.cancel_reason,
            club_id=meeting.club_id,
            club_name=club.name,
            participant_count=participant_count,
            social_cost=meeting.social_cost,
            social_notes=meeting.social_notes,
            team_formation_confirmed_at=meeting.team_formation_confirmed_at,
            rounding_started_at=meeting.rounding_started_at,
            rounding_completed_at=meeting.rounding_completed_at,
            settlement_confirmed=meeting.settlement_confirmed,
            settlement_enabled=club_settlement_enabled(club),
            created_at=meeting.created_at,
            updated_at=meeting.updated_at
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"미팅 생성 중 오류 발생: {str(e)}")
        print(f"ERROR: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 내부 오류가 발생했습니다: {str(e)}")


@router.get("/rounding", response_model=PaginatedResponse)
async def get_rounding_meetings(page: int = Query(1, ge=1),
                                limit: int = Query(10, ge=1, le=100),
                                db: Session = Depends(get_db),
                                current_user: dict = Depends(get_current_user)):
    """라운딩 모임 목록 조회"""
    try:
        # 사용자가 속한 클럽들의 ID 조회
        user_clubs = db.query(ClubMembership.club_id).filter(
            ClubMembership.user_id == current_user.id,
            ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES),
        ).all()

        club_ids = [club.club_id for club in user_clubs]

        if not club_ids:
            return PaginatedResponse(data=[], total=0, page=page, limit=limit, total_pages=0)

        # 라운딩 모임 조회 (ROUNDING 타입) - DELETED 상태 제외
        # 프라이빗 라운딩 필터링: 참가자이거나 생성자인 경우만 표시
        # 사용자가 참가한 프라이빗 라운딩 ID 목록
        user_participant_meeting_ids = select(MeetingParticipant.meeting_id).where(
            MeetingParticipant.user_id == current_user.id
        )

        # 사용자가 생성한 프라이빗 라운딩 ID 목록
        user_created_meeting_ids = select(Meeting.id).where(Meeting.created_by == current_user.id)
        manager_club_ids = select(ClubMembership.club_id).where(
            ClubMembership.user_id == current_user.id,
            ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES),
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER]),
        )

        query = db.query(Meeting).filter(
            Meeting.club_id.in_(club_ids),
            Meeting.meeting_type == MeetingType.ROUND,
            Meeting.status != MeetingStatus.DELETED,
            # 프라이빗 라운딩 필터링
            or_(
                Meeting.is_private == False,  # 일반 라운딩
                and_(Meeting.is_private == True,
                     or_(
                         Meeting.id.in_(user_participant_meeting_ids),
                         Meeting.id.in_(user_created_meeting_ids),
                         Meeting.club_id.in_(manager_club_ids),
                     ))))

        total = query.count()

        meetings = query.order_by(Meeting.meeting_time.desc()).offset((page - 1) * limit).limit(limit).all()

        # Meeting 객체를 MeetingResponse로 변환
        meeting_responses = []
        for meeting in meetings:
            # 클럽 이름 조회
            club = db.query(Club).filter(Club.id == meeting.club_id).first()
            club_name = club.name if club else "Unknown Club"

            # 참가자 수 조회
            participant_count = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting.id).count()

            # tee_times에서 첫 번째 시간을 tee_time으로 설정
            tee_time = None
            if meeting.tee_times and len(meeting.tee_times) > 0:
                from datetime import time
                tee_time_str = meeting.tee_times[0]
                if isinstance(tee_time_str, str):
                    # "18:53" 형태의 문자열을 time 객체로 변환
                    hour, minute = map(int, tee_time_str.split(':'))
                    tee_time = time(hour, minute)

            # 생성자 정보 조회
            created_by_name = None
            if meeting.created_by:
                creator = db.query(User).filter(User.id == meeting.created_by).first()
                if creator:
                    created_by_name = creator.nickname or creator.name

            meeting_dict = {col.key: getattr(meeting, col.key) for col in Meeting.__table__.columns}
            meeting_dict["tee_times"] = meeting.tee_times or []
            
            meeting_responses.append(MeetingResponse(
                **meeting_dict,
                id=meeting.id,                name=meeting.name,
                description=meeting.description,
                location=meeting.location,
                venue_name=meeting.venue_name,
                meeting_time=meeting.meeting_time,
                application_deadline=meeting.application_deadline,
                application_closed_early=False,
                team_formation_mode=meeting.team_formation_mode,
                team_size=meeting.team_size,
                is_completed=meeting.is_completed,
                settlement_confirmed=meeting.settlement_confirmed,
                tee_time=tee_time,
                max_participants=meeting.max_participants,
                social_cost=meeting.social_cost,
                total_cost=meeting.total_cost,
                green_fee=meeting.green_fee,
                caddy_fee=meeting.caddy_fee,
                cart_fee=meeting.cart_fee,
                settlement_method=meeting.settlement_method.value if meeting.settlement_method else None,
                reservation_name=meeting.reservation_name,
            meeting_type=meeting.meeting_type.value if meeting.meeting_type else None,
            meeting_subtype=meeting.meeting_subtype.value if meeting.meeting_subtype else None,
            course_name=meeting.course_name,
                hole_count=meeting.hole_count,
                status=meeting.status,
                cancel_reason=meeting.cancel_reason,
                club_id=meeting.club_id,
                club_name=club_name,
                participant_count=participant_count,
                created_by=meeting.created_by,
                created_by_name=created_by_name,
                team_formation_confirmed_at=meeting.team_formation_confirmed_at,
                rounding_started_at=meeting.rounding_started_at,
                rounding_completed_at=meeting.rounding_completed_at,
                settlement_enabled=club_settlement_enabled(club),
                created_at=meeting.created_at,
                updated_at=meeting.updated_at
            ))
        
        return PaginatedResponse(
            data=meeting_responses,
            total=total,
            page=page,
            limit=limit,
            total_pages=(total + limit - 1) // limit
        )
        
    except Exception as e:
        logger.error(f"라운딩 모임 조회 실패: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="라운딩 모임 조회에 실패했습니다.")


# @router.get("/event", response_model=PaginatedResponse)
# async def get_event_meetings(
#     page: int = Query(1, ge=1),
#     limit: int = Query(10, ge=1, le=100),
#     db: Session = Depends(get_db),
#     current_user: dict = Depends(get_current_user)
# ):
#     """이벤트 모임 목록 조회 (레거시 - Phase 4에서 제거)"""
#     try:
#         # 사용자가 속한 클럽들의 ID 조회
#         user_clubs = db.query(ClubMembership.club_id).filter(
#             ClubMembership.user_id == current_user.id,
#             ClubMembership.status == MembershipStatus.APPROVED
#         ).all()
#
#         club_ids = [club.club_id for club in user_clubs]
#
#         if not club_ids:
#             return PaginatedResponse(
#                 data=[],
#                 total=0,
#                 page=page,
#                 limit=limit,
#                 total_pages=0
#             )
#
#         # 이벤트 모임 조회 (EVENT 타입) - DELETED 상태 제외
#         query = db.query(Meeting).filter(
#             Meeting.club_id.in_(club_ids),
#             Meeting.meeting_type == MeetingType.SOCIAL,
#             Meeting.status != MeetingStatus.DELETED
#         )
#
#         total = query.count()
#
#         meetings = query.order_by(Meeting.meeting_time.desc()).offset(
#             (page - 1) * limit
#         ).limit(limit).all()
#
#         # Meeting 객체를 MeetingResponse로 변환
#         meeting_responses = []
#         for meeting in meetings:
#             # 클럽 이름 조회
#             club = db.query(Club).filter(Club.id == meeting.club_id).first()
#             club_name = club.name if club else "Unknown Club"
#
#             # 참가자 수 조회
#
#             meeting_responses.append(MeetingResponse(
#                 id=meeting.id,
#                 name=meeting.name,
#                 description=meeting.description,
#                 location=meeting.location,
#                 venue_name=meeting.venue_name,
#                 meeting_time=meeting.meeting_time,
#                 application_deadline=meeting.application_deadline,
#                 application_closed_early=False,
#                 team_formation_mode=meeting.team_formation_mode,
#                 team_size=meeting.team_size,
#                 is_completed=meeting.is_completed,
#                 settlement_confirmed=meeting.settlement_confirmed,
#                 tee_time=None,  # 이벤트 모임은 tee_time이 없음
#                 max_participants=meeting.max_participants,
#                 social_cost=meeting.social_cost,
#                 total_cost=meeting.total_cost,
#                 green_fee=meeting.green_fee,
#                 caddy_fee=meeting.caddy_fee,
#                 cart_fee=meeting.cart_fee,
#                 settlement_method=meeting.settlement_method,
#                 reservation_name=meeting.reservation_name,
#                 meeting_type=meeting.meeting_type,
#                 meeting_subtype=meeting.meeting_subtype,
#                 course_name=meeting.course_name,
#                 hole_count=meeting.hole_count,
#                 status=meeting.status,
#                 cancel_reason=meeting.cancel_reason,
#                 club_id=meeting.club_id,
#                 club_name=club_name,
#                 participant_count=participant_count,
#                 created_at=meeting.created_at,
#                 updated_at=meeting.updated_at
#             ))
#
#         return PaginatedResponse(
#             data=meeting_responses,
#             total=total,
#             page=page,
#             limit=limit,
#             total_pages=(total + limit - 1) // limit
#         )
#
#     except Exception as e:
#         logger.error(f"이벤트 모임 조회 실패: {str(e)}")
#         raise HTTPException(
#             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
#             detail="이벤트 모임 조회에 실패했습니다."
#         )


@router.get("/", response_model=PaginatedResponse)
async def get_meetings(club_id: int | None = None,
                       page: int = 1,
                       limit: int = 10,
                       status_filter: MeetingStatus | None = None,
                       meeting_type_filter: MeetingType | None = None,
                       status_group: str | None = None,
                       search: str | None = None,
                       start_date: date | None = None,
                       end_date: date | None = None,
                       list_type: str | None = Query(None, description="목록 타입(rounding|social|participating)"),
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_active_user)):
    """모임 목록 조회 (내 클럽 기본 + 타입 분기)"""
    try:
        offset = (page - 1) * limit

        user_clubs = db.query(ClubMembership.club_id).filter(
            ClubMembership.user_id == current_user.id,
            ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES),
        ).all()
        club_ids = [club.club_id for club in user_clubs]

        if not club_ids:
            return {"data": [], "total": 0, "page": page, "limit": limit, "total_pages": 0}

        meetings_query = db.query(Meeting).filter(Meeting.club_id.in_(club_ids))

        if club_id:
            meetings_query = meetings_query.filter(Meeting.club_id == club_id)

        list_type_value = (list_type or "").lower()
        if list_type_value == "rounding":
            meetings_query = meetings_query.filter(Meeting.meeting_type == MeetingType.ROUND)
        elif list_type_value == "social":
            meetings_query = meetings_query.filter(Meeting.meeting_type == MeetingType.SOCIAL)
        elif list_type_value == "participating":
            participant_meeting_ids = select(MeetingParticipant.meeting_id).where(
                MeetingParticipant.user_id == current_user.id,
                MeetingParticipant.status == MeetingParticipantStatus.CONFIRMED,
            )
            meetings_query = meetings_query.filter(
                Meeting.id.in_(participant_meeting_ids),
                Meeting.meeting_type.in_([MeetingType.ROUND, MeetingType.SOCIAL]),
            )
        elif list_type_value not in ("",):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="list_type은 rounding, social, participating 중 하나여야 합니다.")

        if meeting_type_filter:
            meetings_query = meetings_query.filter(Meeting.meeting_type == meeting_type_filter)

        if status_filter:
            meetings_query = meetings_query.filter(Meeting.status == status_filter)

        if status_group == "active":
            meetings_query = meetings_query.filter(Meeting.status.in_([MeetingStatus.SCHEDULED, MeetingStatus.IN_PROGRESS]))
        elif status_group == "completed":
            meetings_query = meetings_query.filter(Meeting.status.in_([MeetingStatus.COMPLETED, MeetingStatus.CANCELED]))

        user_participant_meeting_ids = select(MeetingParticipant.meeting_id).where(
            MeetingParticipant.user_id == current_user.id
        )
        user_created_meeting_ids = select(Meeting.id).where(Meeting.created_by == current_user.id)
        manager_club_ids = select(ClubMembership.club_id).where(
            ClubMembership.user_id == current_user.id,
            ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES),
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER]),
        )

        meetings_query = meetings_query.filter(
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
            )
        )

        if search:
            search_term = f"%{search}%"
            meetings_query = meetings_query.filter(
                or_(
                    Meeting.name.ilike(search_term),
                    Meeting.description.ilike(search_term),
                    Meeting.location.ilike(search_term),
                    Meeting.course_name.ilike(search_term),
                    Meeting.venue_name.ilike(search_term),
                )
            )

        if start_date:
            meetings_query = meetings_query.filter(Meeting.meeting_time >= datetime.combine(start_date, time.min))
        if end_date:
            meetings_query = meetings_query.filter(Meeting.meeting_time <= datetime.combine(end_date, time.max))

        total = meetings_query.count()
        meetings = meetings_query.order_by(Meeting.meeting_time.desc()).offset(offset).limit(limit).all()

        meeting_data = []
        for meeting in meetings:
            club = db.query(Club).filter(Club.id == meeting.club_id).first()
            participant_count = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting.id).count()

            meeting_data.append({
                "id": meeting.id,
                "name": meeting.name,
                "description": meeting.description,
                "location": meeting.location,
                "meeting_time": meeting.meeting_time,
                "application_deadline": meeting.application_deadline,
                "max_participants": meeting.max_participants,
                "meeting_type": meeting.meeting_type.value if meeting.meeting_type else None,
                "meeting_subtype": meeting.meeting_subtype.value if meeting.meeting_subtype else None,
                "course_name": meeting.course_name,
                "hole_count": meeting.hole_count,
                "venue_name": meeting.venue_name,
                "total_cost": meeting.total_cost,
                "social_cost": meeting.social_cost,
                "status": meeting.status,
                "cancel_reason": meeting.cancel_reason,
                "club_id": meeting.club_id,
                "club_name": club.name if club else "알 수 없는 클럽",
                "participant_count": participant_count,
                "application_closed_early": meeting.application_closed_early,
                "team_formation_confirmed_at": meeting.team_formation_confirmed_at,
                "rounding_started_at": meeting.rounding_started_at,
                "rounding_completed_at": meeting.rounding_completed_at,
                "settlement_confirmed": meeting.settlement_confirmed,
                "is_completed": meeting.is_completed,
                "is_private": meeting.is_private,
                "created_by": meeting.created_by,
                "created_at": meeting.created_at,
                "updated_at": meeting.updated_at
            })

        total_pages = (total + limit - 1) // limit
        return {"data": meeting_data, "total": total, "page": page, "limit": limit, "total_pages": total_pages}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"모임 목록 조회 중 오류 발생: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 내부 오류가 발생했습니다: {str(e)}")


@router.get("/my", response_model=PaginatedResponse)
async def get_my_meetings(page: int = 1,
                          limit: int = 10,
                          status_filter: MeetingStatus | None = None,
                          meeting_type_filter: MeetingType | None = None,
                          db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_active_user)):
    """내가 참가한 모임 목록 조회"""
    try:
        # 페이지네이션 계산
        offset = (page - 1) * limit

        # 내가 참가한 모임 조회 (참가자이거나 생성자인 경우)
        # 참가한 모임
        participant_meetings = select(MeetingParticipant.meeting_id).where(
            MeetingParticipant.user_id == current_user.id
        )

        # 생성한 모임 (프라이빗 라운딩 포함)
        created_meetings = select(Meeting.id).where(Meeting.created_by == current_user.id)

        my_meetings_query = db.query(Meeting).filter(
            or_(Meeting.id.in_(participant_meetings), Meeting.id.in_(created_meetings)))

        if status_filter:
            my_meetings_query = my_meetings_query.filter(Meeting.status == status_filter)

        if meeting_type_filter:
            my_meetings_query = my_meetings_query.filter(Meeting.meeting_type == meeting_type_filter)

        # 총 개수
        total_count = my_meetings_query.count()

        # 페이지네이션 적용
        meetings = my_meetings_query.order_by(Meeting.meeting_time.desc()).offset(offset).limit(limit).all()

        # 응답 데이터 구성
        meetings_data = []
        for meeting in meetings:
            # 클럽 정보 조회
            club = db.query(Club).filter(Club.id == meeting.club_id).first()

            # 참가자 수 조회
            participant_count = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting.id).count()

            # 내 참가 정보 조회
            my_participation = db.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == meeting.id, MeetingParticipant.user_id == current_user.id).first()

            meetings_data.append({
                "id": meeting.id,
                "name": meeting.name,
                "description": meeting.description,
                "location": meeting.location,
                "meeting_time": meeting.meeting_time,
                "max_participants": meeting.max_participants,
                "meeting_type": meeting.meeting_type.value if meeting.meeting_type else None,
                "course_name": meeting.course_name,
                "hole_count": meeting.hole_count,
                "status": meeting.status,
                "cancel_reason": meeting.cancel_reason,
                "club_id": meeting.club_id,
                "club_name": club.name if club else "알 수 없는 클럽",
                "participant_count": participant_count,
                "my_participation": {
                    "id": my_participation.id,
                    "participant_type": my_participation.participant_type
                } if my_participation else None,
                "created_at": meeting.created_at,
                "updated_at": meeting.updated_at
            })

        # 총 페이지 수 계산
        total_pages = (total_count + limit - 1) // limit

        return {"data": meetings_data, "total": total_count, "total_pages": total_pages, "page": page, "limit": limit}

    except Exception as e:
        logger.error(f"내 모임 목록 조회 중 오류 발생: {str(e)}")
        logger.error(f"미팅 생성 중 오류 발생: {str(e)}")
        print(f"ERROR: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 내부 오류가 발생했습니다: {str(e)}")


@router.get("/my/participating", response_model=PaginatedResponse)
async def get_my_participating_meetings(page: int = 1,
                                        limit: int = 10,
                                        status_group: str | None = None,
                                        search: str | None = None,
                                        start_date: date | None = None,
                                        end_date: date | None = None,
                                        db: Session = Depends(get_db),
                                        current_user: User = Depends(get_current_active_user)):
    """내가 참여중인(참가자) 모임 목록 조회"""
    try:
        # 페이지네이션 계산
        offset = (page - 1) * limit

        # 내가 참가자인 모임만 조회 (생성자 조건 제외)
        participant_meetings = select(MeetingParticipant.meeting_id).where(
            MeetingParticipant.user_id == current_user.id,
            MeetingParticipant.status == MeetingParticipantStatus.CONFIRMED,
        )

        my_meetings_query = db.query(Meeting).filter(Meeting.id.in_(participant_meetings))

        if status_group == "active":
            my_meetings_query = my_meetings_query.filter(
                Meeting.status.in_([MeetingStatus.SCHEDULED, MeetingStatus.IN_PROGRESS])
            )
        elif status_group == "completed":
            my_meetings_query = my_meetings_query.filter(
                Meeting.status.in_([MeetingStatus.COMPLETED, MeetingStatus.CANCELED])
            )

        if search:
            search_term = f"%{search}%"
            my_meetings_query = my_meetings_query.filter(
                or_(
                    Meeting.name.ilike(search_term),
                    Meeting.description.ilike(search_term),
                    Meeting.location.ilike(search_term),
                    Meeting.course_name.ilike(search_term),
                    Meeting.venue_name.ilike(search_term),
                )
            )
        if start_date:
            my_meetings_query = my_meetings_query.filter(
                Meeting.meeting_time >= datetime.combine(start_date, time.min)
            )
        if end_date:
            my_meetings_query = my_meetings_query.filter(
                Meeting.meeting_time <= datetime.combine(end_date, time.max)
            )

        # 총 개수
        total_count = my_meetings_query.count()

        # 페이지네이션 적용
        meetings = my_meetings_query.order_by(Meeting.meeting_time.desc()).offset(offset).limit(limit).all()

        # 응답 데이터 구성
        meetings_data = []
        for meeting in meetings:
            # 클럽 정보 조회
            club = db.query(Club).filter(Club.id == meeting.club_id).first()

            # 참가자 수 조회
            participant_count = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting.id).count()

            # 내 참가 정보 조회
            my_participation = db.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == meeting.id, MeetingParticipant.user_id == current_user.id).first()

            meetings_data.append({
                "id": meeting.id,
                "name": meeting.name,
                "description": meeting.description,
                "location": meeting.location,
                "meeting_time": meeting.meeting_time,
                "max_participants": meeting.max_participants,
                "meeting_type": meeting.meeting_type.value if meeting.meeting_type else None,
                "course_name": meeting.course_name,
                "hole_count": meeting.hole_count,
                "status": meeting.status,
                "cancel_reason": meeting.cancel_reason,
                "club_id": meeting.club_id,
                "club_name": club.name if club else "알 수 없는 클럽",
                "participant_count": participant_count,
                "my_participation": {
                    "id": my_participation.id,
                    "participant_type": my_participation.participant_type
                } if my_participation else None,
                "created_at": meeting.created_at,
                "updated_at": meeting.updated_at
            })

        # 총 페이지 수 계산
        total_pages = (total_count + limit - 1) // limit

        return {"data": meetings_data, "total": total_count, "total_pages": total_pages, "page": page, "limit": limit}

    except Exception as e:
        logger.error(f"내 참여중 모임 목록 조회 중 오류 발생: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 내부 오류가 발생했습니다: {str(e)}")


@router.get("/{meeting_id}", response_model=MeetingResponse)
async def get_meeting(meeting_id: int,
                      db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_active_user)):
    """모임 상세 조회"""
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()

        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # JOIN을 사용한 최적화된 쿼리로 클럽과 참가자 정보를 한 번에 조회
        meeting_with_club = db.query(Meeting,
                                     Club).join(Club,
                                                Meeting.club_id == Club.id).filter(Meeting.id == meeting_id).first()

        if not meeting_with_club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        meeting, club = meeting_with_club

        # 프라이빗 라운딩인 경우 권한 체크
        if meeting.is_private:
            # 참가자 또는 생성자인지 확인
            is_participant = db.query(MeetingParticipant).filter(
                and_(MeetingParticipant.meeting_id == meeting.id,
                     MeetingParticipant.user_id == current_user.id)).first()

            is_creator = meeting.created_by == current_user.id
            is_manager_or_leader = db.query(ClubMembership).filter(
                ClubMembership.club_id == meeting.club_id,
                ClubMembership.user_id == current_user.id,
                ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES),
                ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER]),
            ).first()

            if not is_participant and not is_creator and not is_manager_or_leader:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                    detail="프라이빗 라운딩은 참가자/생성자/클럽 리더·매니저만 조회할 수 있습니다.")
        else:
            # 일반 라운딩: 클럽 멤버십 확인
            membership = db.query(ClubMembership).filter(
                and_(ClubMembership.user_id == current_user.id, ClubMembership.club_id == meeting.club_id,
                     ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES))).first()

            if not membership:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="해당 클럽의 멤버가 아닙니다.")

        # 참가자와 사용자 정보를 JOIN으로 한 번에 조회
        participants_with_users = db.query(MeetingParticipant, User.realname, User.nickname).outerjoin(
            User, MeetingParticipant.user_id == User.id).filter(MeetingParticipant.meeting_id == meeting_id).all()

        participant_data = []
        for participant, user_realname, user_nickname in participants_with_users:
            participant_data.append({
                "id":
                participant.id,
                "user_id":
                participant.user_id,
                "user_name":
                user_realname if user_realname else "이름 없음",
                "user_nickname":
                user_nickname if user_nickname else "닉네임 없음",
                "handicap":
                participant.handicap if participant.handicap is not None else 0.0,
                "average_score":
                participant.average_score if participant.average_score is not None else 0,
                "pace_preference":
                participant.pace_preference if participant.pace_preference is not None else "",
                "tee_preference":
                participant.tee_preference if participant.tee_preference is not None else "",
                "is_newbie":
                participant.is_newbie if participant.is_newbie is not None else False,
                "prefer_with":
                participant.prefer_with if participant.prefer_with is not None else [],
                "avoid_with":
                participant.avoid_with if participant.avoid_with is not None else [],
                "created_at":
                participant.created_at
            })

        participant_count = len(participants_with_users)

        # 생성자 정보 조회
        created_by_name = None
        if meeting.created_by:
            creator = db.query(User).filter(User.id == meeting.created_by).first()
            if creator:
                created_by_name = creator.nickname or creator.name

        meeting_dict = {col.key: getattr(meeting, col.key) for col in Meeting.__table__.columns}
        meeting_dict["tee_times"] = meeting.tee_times or []
        
        return MeetingResponse(
            **meeting_dict,
            name=meeting.name,
            description=meeting.description,
            location=meeting.location,
            meeting_time=meeting.meeting_time,
            application_deadline=meeting.application_deadline,
            tee_times=meeting.tee_times or [],
            max_participants=meeting.max_participants,
            meeting_type=meeting.meeting_type,
            meeting_subtype=meeting.meeting_subtype,
            team_formation_mode=meeting.team_formation_mode,
            team_size=meeting.team_size,
            total_cost=meeting.total_cost,
            green_fee=meeting.green_fee,
            caddy_fee=meeting.caddy_fee,
            cart_fee=meeting.cart_fee,
            settlement_method=meeting.settlement_method.value if meeting.settlement_method else None,
            course_name=meeting.course_name,
            hole_count=meeting.hole_count,
            reservation_name=meeting.reservation_name,
            venue_name=meeting.venue_name,
            status=meeting.status,
            cancel_reason=meeting.cancel_reason,
            club_id=meeting.club_id,
            club_name=club.name if club else None,
            participant_count=participant_count,
            created_by=meeting.created_by,
            created_by_name=created_by_name,
            social_cost=meeting.social_cost,
            social_notes=meeting.social_notes,
            team_formation_confirmed_at=meeting.team_formation_confirmed_at,
            rounding_started_at=meeting.rounding_started_at,
            rounding_completed_at=meeting.rounding_completed_at,
            settlement_confirmed=meeting.settlement_confirmed,
            settlement_enabled=club_settlement_enabled(club),
            created_at=meeting.created_at,
            updated_at=meeting.updated_at
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"미팅 생성 중 오류 발생: {str(e)}")
        print(f"ERROR: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 내부 오류가 발생했습니다: {str(e)}")


@router.put("/{meeting_id}", response_model=MeetingResponse, dependencies=[Depends(reject_banned_content)])
async def update_meeting(meeting_id: int,
                         meeting_data: MeetingUpdate,
                         db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_active_user)):
    """모임 정보 수정 (모임 매니저만 가능)"""
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()

        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # 모임 매니저 권한 확인 (개설자 또는 리더/매니저)
        from utils.permissions import is_meeting_organizer_or_manager
        if not is_meeting_organizer_or_manager(meeting_id, current_user.id, db):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="모임 매니저만 모임 정보를 수정할 수 있습니다.")

        previous_status = meeting.status

        # 모임 정보 수정
        if meeting_data.name is not None:
            meeting.name = meeting_data.name
        if meeting_data.description is not None:
            meeting.description = meeting_data.description
        if meeting_data.location is not None:
            meeting.location = meeting_data.location
        if meeting_data.meeting_time is not None:
            meeting.meeting_time = meeting_data.meeting_time
        if meeting_data.max_participants is not None:
            meeting.max_participants = meeting_data.max_participants
        # meeting_type은 수정할 수 없음 (보안상 이유)
        if meeting_data.course_name is not None:
            meeting.course_name = meeting_data.course_name
        if meeting_data.hole_count is not None:
            meeting.hole_count = meeting_data.hole_count
        if meeting_data.status is not None:
            meeting.status = meeting_data.status
        if meeting_data.cancel_reason is not None:
            meeting.cancel_reason = meeting_data.cancel_reason
        if meeting_data.settlement_method is not None:
            meeting.settlement_method = ModelSettlementMethod(meeting_data.settlement_method.value)
        if meeting_data.meeting_subtype is not None:
            meeting.meeting_subtype = ModelMeetingSubtype(meeting_data.meeting_subtype.value)
        
        db.commit()
        db.refresh(meeting)

        try:
            notify_round_participants_status_changed(
                db=db,
                meeting_id=meeting_id,
                previous_status=previous_status,
                current_status=meeting.status,
            )
        except Exception as e:
            logger.error(f"라운딩 상태 변경 알림 전송 실패 - meeting_id: {meeting_id}, error: {str(e)}")

        # 클럽 정보 조회
        club = db.query(Club).filter(Club.id == meeting.club_id).first()

        # 참가자 수 조회
        participant_count = db.query(MeetingParticipant).filter(
            MeetingParticipant.meeting_id == meeting_id,
            MeetingParticipant.status == MeetingParticipantStatus.CONFIRMED
        ).count()
        
        return MeetingResponse(
            id=meeting.id,            name=meeting.name,
            description=meeting.description,
            location=meeting.location,
            meeting_time=meeting.meeting_time,
            tee_times=meeting.tee_times,
            max_participants=meeting.max_participants,
            meeting_type=meeting.meeting_type,
            meeting_subtype=meeting.meeting_subtype,
            total_cost=meeting.total_cost,
            green_fee=meeting.green_fee,
            caddy_fee=meeting.caddy_fee,
            cart_fee=meeting.cart_fee,
            settlement_method=meeting.settlement_method.value if meeting.settlement_method else None,
            course_name=meeting.course_name,
            hole_count=meeting.hole_count,
            reservation_name=meeting.reservation_name,
            status=meeting.status,
            cancel_reason=meeting.cancel_reason,
            club_id=meeting.club_id,
            club_name=club.name if club else None,
            participant_count=participant_count,
            team_formation_confirmed_at=meeting.team_formation_confirmed_at,
            rounding_started_at=meeting.rounding_started_at,
            rounding_completed_at=meeting.rounding_completed_at,
            settlement_confirmed=meeting.settlement_confirmed,
            settlement_enabled=club_settlement_enabled(club),
            created_at=meeting.created_at,
            updated_at=meeting.updated_at
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"미팅 생성 중 오류 발생: {str(e)}")
        print(f"ERROR: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 내부 오류가 발생했습니다: {str(e)}")


@router.delete("/{meeting_id}", response_model=MessageResponse)
async def delete_meeting(meeting_id: int,
                         db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_active_user)):
    """모임 삭제 (모임 매니저만 가능)"""
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()

        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # 모임 매니저 권한 확인 (개설자 또는 리더/매니저)
        from utils.permissions import is_meeting_organizer_or_manager
        if not is_meeting_organizer_or_manager(meeting_id, current_user.id, db):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="모임 매니저만 모임을 삭제할 수 있습니다.")

        # 참가자들을 먼저 삭제 (SQLAlchemy ORM 사용)
        participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).all()

        for participant in participants:
            db.delete(participant)

        # 참가자 삭제 커밋
        db.commit()

        # 모임 삭제
        db.delete(meeting)
        db.commit()

        return {"message": "모임이 성공적으로 삭제되었습니다.", "success": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"미팅 생성 중 오류 발생: {str(e)}")
        print(f"ERROR: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 내부 오류가 발생했습니다: {str(e)}")


# 참가자 관리, 팀 관리, 점수 관리, 비용 관리는 각각의 전용 파일로 이동됨
# workflow.py, participants.py, teams.py, scores.py, expenses.py 참조

# =============================================================================
# 모임 취소
# =============================================================================

# 참가자 조회는 participants.py로 이동됨

# 참가자 관리 API는 participants.py로 이동됨

# =============================================================================
# 모임 취소
# =============================================================================


@router.post("/{meeting_id}/cancel", response_model=MessageResponse)
async def cancel_meeting(meeting_id: int,
                         request_data: dict,
                         db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_active_user)):
    """모임 취소 (모임 매니저만 가능)"""
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()

        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # 모임 매니저 권한 확인 (개설자 또는 클럽 리더/매니저)
        from utils.permissions import is_meeting_organizer_or_manager
        if not is_meeting_organizer_or_manager(meeting_id, current_user.id, db):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="모임 매니저만 모임을 취소할 수 있습니다.")

        # 이미 취소된 모임인지 확인
        if meeting.status == MeetingStatus.CANCELED:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 취소된 모임입니다.")

        if meeting.status == MeetingStatus.IN_PROGRESS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="진행중인 모임은 취소할 수 없습니다.")

        previous_status = meeting.status

        # 모임 취소
        cancel_reason = request_data.get('reason', '개설자에 의한 모임 취소')
        meeting.status = MeetingStatus.CANCELED
        meeting.cancel_reason = cancel_reason
        db.commit()

        try:
            notify_round_participants_status_changed(
                db=db,
                meeting_id=meeting_id,
                previous_status=previous_status,
                current_status=meeting.status,
            )
        except Exception as e:
            logger.error(f"라운딩 상태 변경 알림 전송 실패 - meeting_id: {meeting_id}, error: {str(e)}")

        return {"message": "모임이 성공적으로 취소되었습니다.", "success": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"미팅 생성 중 오류 발생: {str(e)}")
        print(f"ERROR: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 내부 오류가 발생했습니다: {str(e)}")


# =============================================================================
# 클럽별 모임 관리 API
# =============================================================================


@router.get("/clubs/{club_id}", response_model=PaginatedResponse)
async def get_club_meetings(club_id: str,
                            page: int = 1,
                            limit: int = 10,
                            status_filter: MeetingStatus | None = None,
                            meeting_type_filter: MeetingType | None = None,
                            search: str | None = None,
                            db: Session = Depends(get_db),
                            current_user: User = Depends(get_current_active_user)):
    """클럽별 모임 목록 조회"""
    try:
        # 클럽 존재 확인 (display_id로 먼저 조회, 없으면 id로 조회)
        club = db.query(Club).filter(Club.display_id == club_id, Club.deleted_at.is_(None)).first()

        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(Club.id == club_id_int, Club.deleted_at.is_(None)).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None

        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다.")

        # 디버깅: 클럽 ID 확인
        print(f"DEBUG - 클럽 조회 결과: id={club.id}")

        # 클럽 멤버인지 확인
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,  # 실제 클럽 ID 사용
            ClubMembership.user_id == current_user.id).first()

        if not membership:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="클럽 멤버만 모임을 조회할 수 있습니다.")

        # 페이지네이션 계산
        offset = (page - 1) * limit

        # 모임 조회 쿼리
        meetings_query = db.query(Meeting).filter(Meeting.club_id == club.id)

        if status_filter:
            meetings_query = meetings_query.filter(Meeting.status == status_filter)

        if meeting_type_filter:
            meetings_query = meetings_query.filter(Meeting.meeting_type == meeting_type_filter)

        # 검색 기능 추가
        if search:
            search_term = f"%{search}%"
            meetings_query = meetings_query.filter((Meeting.name.ilike(search_term))
                                                   | (Meeting.description.ilike(search_term))
                                                   | (Meeting.location.ilike(search_term))
                                                   | (Meeting.course_name.ilike(search_term)))

        total = meetings_query.count()

        # JOIN을 사용한 최적화된 쿼리
        from sqlalchemy import func

        meetings_with_data = db.query(
            Meeting,
            func.count(MeetingParticipant.id).label('participant_count')).outerjoin(
                MeetingParticipant,
                MeetingParticipant.meeting_id == Meeting.id).filter(Meeting.club_id == club.id).group_by(
                    Meeting.id).order_by(Meeting.meeting_time.desc()).offset(offset).limit(limit).all()

        # 응답 데이터 구성
        meeting_data = []
        for meeting, participant_count in meetings_with_data:
            meeting_data.append({
                "id": meeting.id,
                "name": meeting.name,
                "description": meeting.description,
                "location": meeting.location,
                "meeting_time": meeting.meeting_time,
                "max_participants": meeting.max_participants,
                "meeting_type": meeting.meeting_type.value if meeting.meeting_type else None,
                "course_name": meeting.course_name,
                "hole_count": meeting.hole_count,
                "status": meeting.status,
                "cancel_reason": meeting.cancel_reason,
                "club_id": meeting.club_id,
                "club_name": club.name,
                "participant_count": participant_count,
                "created_at": meeting.created_at,
                "updated_at": meeting.updated_at
            })

        total_pages = (total + limit - 1) // limit

        return {"data": meeting_data, "total": total, "page": page, "limit": limit, "total_pages": total_pages}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"미팅 생성 중 오류 발생: {str(e)}")
        print(f"ERROR: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 내부 오류가 발생했습니다: {str(e)}")


# 모임 통계는 /api/v1/admin/meetings/stats 에서 조회 가능

# 모임 알림 관련 스키마
from pydantic import BaseModel


class MeetingNotificationRequest(BaseModel):
    title: str
    content: str
    notification_type: str = "MEETING_UPDATE"  # MEETING_UPDATE, MEETING_REMINDER, MEETING_CANCEL
    send_to_all_participants: bool = True
    participant_ids: list[int] | None = None


class MeetingNotificationResponse(BaseModel):
    notification_id: int
    title: str
    content: str
    notification_type: str
    sent_to_count: int
    created_at: datetime


@router.post("/{meeting_id}/notify", response_model=MeetingNotificationResponse)
async def send_meeting_notification(meeting_id: int,
                                    notification_data: MeetingNotificationRequest,
                                    db: Session = Depends(get_db),
                                    current_user: User = Depends(get_current_active_user)):
    """모임 참가자들에게 알림 전송 (모임 매니저만 가능)"""
    try:
        from services.push_delivery_service import send_push_to_user
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # 모임 매니저 권한 확인 (개설자 또는 클럽 리더/매니저)
        from utils.permissions import is_meeting_organizer_or_manager
        if not is_meeting_organizer_or_manager(meeting_id, current_user.id, db):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="모임 매니저만 알림을 전송할 수 있습니다.")

        # 알림을 받을 참가자들 조회
        if notification_data.send_to_all_participants:
            participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id,
                                                               MeetingParticipant.user_id.isnot(None)).all()
            target_user_ids = [p.user_id for p in participants]
        else:
            if not notification_data.participant_ids:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="알림을 받을 참가자 ID 목록이 필요합니다.")
            target_user_ids = notification_data.participant_ids

        # 알림 생성 및 전송
        from models import Notification, NotificationType, NotificationStatus
        from datetime import datetime

        sent_count = 0
        notification_ids = []

        for user_id in target_user_ids:
            try:
                notification = Notification(user_id=user_id,
                                            type=NotificationType(notification_data.notification_type).value if hasattr(
                                                NotificationType(notification_data.notification_type), 'value') else
                                            str(NotificationType(notification_data.notification_type)),
                                            title=notification_data.title,
                                            content=notification_data.content,
                                            status=NotificationStatus.UNREAD.value)

                db.add(notification)
                send_push_to_user(db=db,
                                  user_id=user_id,
                                  title=notification.title,
                                  content=notification.content,
                                  category="meeting",
                                  target_id=meeting_id,
                                  extra_data={"meeting_type": meeting.meeting_type.value.lower()})
                db.flush()  # notification.id를 얻기 위해 flush
                notification_ids.append(notification.id)
                sent_count += 1

            except Exception as e:
                print(f"알림 생성 실패 - user_id: {user_id}, error: {str(e)}")
                continue

        db.commit()

        response = MeetingNotificationResponse(notification_id=notification_ids[0] if notification_ids else "",
                                               title=notification_data.title,
                                               content=notification_data.content,
                                               notification_type=notification_data.notification_type,
                                               sent_to_count=sent_count,
                                               created_at=datetime.now())

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"미팅 생성 중 오류 발생: {str(e)}")
        print(f"ERROR: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 내부 오류가 발생했습니다: {str(e)}")


@router.post("/{meeting_id}/remind", response_model=MessageResponse)
async def send_meeting_reminder(meeting_id: int,
                                reminder_hours: int = 24,
                                db: Session = Depends(get_db),
                                current_user: User = Depends(get_current_active_user)):
    """모임 리마인더 전송 (모임 매니저만 가능)"""
    try:
        from services.push_delivery_service import send_push_to_user
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # 모임 매니저 권한 확인 (개설자 또는 클럽 리더/매니저)
        from utils.permissions import is_meeting_organizer_or_manager
        if not is_meeting_organizer_or_manager(meeting_id, current_user.id, db):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="모임 매니저만 리마인더를 전송할 수 있습니다.")

        # 참가자들 조회
        participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id,
                                                           MeetingParticipant.user_id.isnot(None)).all()

        # 리마인더 알림 생성
        from models import Notification, NotificationType, NotificationStatus
        from datetime import datetime

        club = db.query(Club).filter(Club.id == meeting.club_id).first()
        club_name = club.name if club else "알 수 없는 클럽"

        title = f"모임 리마인더 - {meeting.name}"
        content = f"""
{club_name}의 모임 '{meeting.name}'이 {reminder_hours}시간 후에 시작됩니다.

📅 일시: {meeting.meeting_time.strftime('%Y년 %m월 %d일 %H:%M')}
📍 장소: {meeting.location}
🏌️ 코스: {meeting.course_name if meeting.course_name else '미정'}

모임에 참석할 준비를 해주세요!
        """.strip()

        sent_count = 0
        for participant_obj in participants:
            try:
                notification = Notification(user_id=participant_obj.user_id,
                                            type=NotificationType.MEETING_REMINDER.value,
                                            title=title,
                                            content=content,
                                            status=NotificationStatus.UNREAD.value)

                db.add(notification)
                send_push_to_user(db=db,
                                  user_id=participant_obj.user_id,
                                  title=notification.title,
                                  content=notification.content,
                                  category="meeting",
                                  target_id=meeting_id,
                                  extra_data={"meeting_type": meeting.meeting_type.value.lower()})
                sent_count += 1

            except Exception as e:
                print(f"리마인더 알림 생성 실패 - user_id: {participant_obj.user_id}, error: {str(e)}")
                continue

        db.commit()

        return {"message": f"{sent_count}명에게 모임 리마인더가 전송되었습니다.", "success": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"미팅 생성 중 오류 발생: {str(e)}")
        print(f"ERROR: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 내부 오류가 발생했습니다: {str(e)}")


# 팀 관리 API는 teams.py로 이동됨
# 게스트 관리 API는 participants.py로 이동됨 (게스트도 참가자이므로)
# 점수 관리 API는 scores.py로 이동됨
# 비용 관리 API는 expenses.py로 이동됨

# 중복된 GET /my는 이미 위에 있음 (584번 라인)
# 참가/탈퇴는 workflow.py로 이동됨
