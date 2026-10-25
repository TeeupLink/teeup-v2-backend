"""
백오피스 모임 API
"""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Query, Body
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func, extract
import logging

from database import get_db
from schemas import MeetingParticipantScoreCreate, ScoreUpdate
from utils.datetime_utils import get_kst_now
from utils.amount_split import split_amount_10won, allocate_by_total, allocate_items_to_exact_totals
from utils.notification_service import notify_round_participants_status_changed

from .deps import get_admin_user


def _resolve_extra_payer_index(effective_ids: list, extra_payer_id) -> int | None:
    """나머지 10원을 부담할 참가자의 effective_ids 내 인덱스 반환. 없으면 None."""
    if extra_payer_id is None:
        return None
    try:
        pid = int(extra_payer_id)
        for i, eid in enumerate(effective_ids):
            if eid is not None and int(eid) == pid:
                return i
    except (TypeError, ValueError):
        pass
    return None


def _admin_sync_private_round_participants(db: Session, meeting, club_id: int, meeting_data: dict) -> None:
    """프라이빗 라운딩(예정): 클럽 멤버·게스트 참가자를 요청 본문과 동기화."""
    from decimal import Decimal
    from sqlalchemy import and_
    from models import MeetingParticipant, ClubMembership, Guest, ParticipantType, Gender as ModelGender
    from utils.permissions import MEMBERSHIP_ACTIVE_STATUSES
    from utils.team_formation import add_guest_to_meeting, calculate_guest_handicap

    raw_sel = meeting_data.get("selected_participants")
    if raw_sel is None:
        raw_sel = []
    if isinstance(raw_sel, str):
        raw_sel = [int(x.strip()) for x in raw_sel.replace(",", " ").split() if x.strip().isdigit()]
    if not isinstance(raw_sel, list):
        raw_sel = []
    norm_uid = []
    for x in raw_sel:
        try:
            norm_uid.append(int(x))
        except (TypeError, ValueError):
            continue

    raw_guests = meeting_data.get("selected_guests") or []
    guest_payloads = []
    if isinstance(raw_guests, list):
        for item in raw_guests:
            if not isinstance(item, dict):
                continue
            nm = (item.get("name") or "").strip()
            if not nm:
                continue
            gid = item.get("guest_id")
            if gid is not None:
                try:
                    gid = int(gid)
                except (TypeError, ValueError):
                    gid = None
            guest_payloads.append({
                "guest_id": gid,
                "name": nm,
                "birthdate": item.get("birthdate") or None,
                "gender": item.get("gender"),
                "average_score": item.get("average_score"),
                "handicap": item.get("handicap"),
            })

    if not norm_uid and not guest_payloads:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="프라이빗 라운딩은 클럽 멤버 1명 이상 또는 게스트 1명 이상을 지정해주세요.",
        )

    for uid in norm_uid:
        member_check = db.query(ClubMembership).filter(
            and_(
                ClubMembership.user_id == uid,
                ClubMembership.club_id == club_id,
                ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES),
            )
        ).first()
        if not member_check:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"user_id {uid}는 해당 클럽의 활성 멤버가 아닙니다.",
            )

    uid_set = set(norm_uid)
    for p in db.query(MeetingParticipant).filter(
        MeetingParticipant.meeting_id == meeting.id,
        MeetingParticipant.user_id.isnot(None),
    ).all():
        if p.user_id not in uid_set:
            db.delete(p)

    for uid in norm_uid:
        ex = db.query(MeetingParticipant).filter(
            and_(MeetingParticipant.meeting_id == meeting.id, MeetingParticipant.user_id == uid)
        ).first()
        if not ex:
            db.add(
                MeetingParticipant(
                    meeting_id=meeting.id,
                    user_id=uid,
                    participant_type=ParticipantType.USER,
                )
            )

    target_gids = set()

    for gd in guest_payloads:
        gid = gd.get("guest_id")
        if gid:
            g_row = db.query(Guest).filter(Guest.id == gid).first()
            if not g_row:
                continue
            g_row.name = gd["name"]
            if gd.get("birthdate"):
                try:
                    birth_date = datetime.strptime(str(gd["birthdate"])[:10], "%Y-%m-%d").date()
                    g_row.birthdate = datetime.combine(birth_date, datetime.min.time())
                except ValueError:
                    pass
            gen = gd.get("gender")
            if gen in ("MALE", "FEMALE"):
                g_row.gender = ModelGender[gen]
            gh = gd.get("handicap")
            av = gd.get("average_score")
            gh_dec = None
            if gh is not None and str(gh).strip() != "":
                try:
                    gh_dec = Decimal(str(gh))
                except Exception:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"게스트 '{gd['name']}'의 핸디캡 형식이 올바르지 않습니다.",
                    )
            if gh_dec is None and av is not None and str(av).strip() != "":
                try:
                    gh_dec = calculate_guest_handicap(int(av))
                except Exception:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"게스트 '{gd['name']}'의 평균 타수가 올바르지 않습니다.",
                    )
            if gh_dec is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"게스트 '{gd['name']}'의 핸디캡 또는 평균 타수가 필요합니다.",
                )
            g_row.handicap = gh_dec
            mp = db.query(MeetingParticipant).filter(
                and_(MeetingParticipant.meeting_id == meeting.id, MeetingParticipant.guest_id == gid)
            ).first()
            if not mp:
                db.add(
                    MeetingParticipant(
                        meeting_id=meeting.id,
                        guest_id=int(gid),
                        participant_type=ParticipantType.GUEST,
                    )
                )
            target_gids.add(int(gid))
        else:
            gh_dec = None
            if gd.get("handicap") is not None and str(gd.get("handicap")).strip() != "":
                try:
                    gh_dec = Decimal(str(gd.get("handicap")))
                except Exception:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="게스트 핸디캡 형식이 올바르지 않습니다.",
                    )
            av_int = None
            if gd.get("average_score") is not None and str(gd.get("average_score")).strip() != "":
                try:
                    av_int = int(gd.get("average_score"))
                except (TypeError, ValueError):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="게스트 평균 타수 형식이 올바르지 않습니다.",
                    )
            gen2 = None
            if gd.get("gender") in ("MALE", "FEMALE"):
                gen2 = ModelGender[gd["gender"]]
            try:
                part = add_guest_to_meeting(
                    meeting_id=meeting.id,
                    guest_name=gd["name"],
                    guest_handicap=gh_dec,
                    average_score=av_int,
                    guest_birthdate=gd.get("birthdate"),
                    guest_gender=gen2,
                    db=db,
                    auto_commit=False,
                )
                if part.guest_id:
                    target_gids.add(int(part.guest_id))
            except ValueError as ve:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))

    for p in db.query(MeetingParticipant).filter(
        MeetingParticipant.meeting_id == meeting.id,
        MeetingParticipant.guest_id.isnot(None),
    ).all():
        if p.guest_id not in target_gids:
            gid = p.guest_id
            db.delete(p)
            db.flush()
            remaining = db.query(MeetingParticipant).filter(MeetingParticipant.guest_id == gid).count()
            if remaining == 0:
                og = db.query(Guest).filter(Guest.id == gid).first()
                if og:
                    db.delete(og)


logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-meetings"])


class MeetingStatsResponse(BaseModel):
    total_meetings: int
    active_meetings: int
    canceled_meetings: int
    completed_meetings: int
    meetings_by_type: dict
    meetings_by_month: list[dict]
    total_participants: int
    average_participants_per_meeting: float
    meetings_by_club: list[dict]
    upcoming_meetings_count: int
    past_meetings_count: int


@router.get("/meetings")
async def admin_get_meetings(
    page: int = Query(1, ge=1, description="페이지 번호"),
    limit: int = Query(10, ge=1, le=100, description="페이지당 항목 수"),
    status: str | None = Query(None, description="모임 상태 필터"),
    search: str | None = Query(None, description="검색어"),
    club_id: int | None = Query(None, description="클럽 ID 필터"),
    meeting_type: str | None = Query(None, description="모임 유형: ROUND, SOCIAL"),
    current_user: dict = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """관리자용 모임 목록 조회 (라운딩+소셜 통합, 유형 필터 가능)"""
    try:
        from models import Meeting, Club, MeetingParticipant
        from schemas import MeetingType, MeetingParticipantStatus, MeetingParticipantRole
        from sqlalchemy import desc, and_, or_

        query = db.query(Meeting).join(Club)
        if meeting_type == "ROUND":
            query = query.filter(Meeting.meeting_type == MeetingType.ROUND)
        elif meeting_type == "SOCIAL":
            query = query.filter(Meeting.meeting_type == MeetingType.SOCIAL)

        if club_id:
            query = query.filter(Meeting.club_id == club_id)
        if status:
            query = query.filter(Meeting.status == status)
        if search:
            query = query.filter(
                or_(
                    Meeting.name.contains(search),
                    Meeting.location.contains(search),
                    Meeting.course_name.contains(search),
                    Meeting.venue_name.contains(search),
                )
            )

        total = query.count()
        meetings = query.order_by(desc(Meeting.created_at)).offset((page - 1) * limit).limit(limit).all()

        meeting_data = []
        for meeting in meetings:
            participant_count = db.query(MeetingParticipant).filter(
                and_(MeetingParticipant.meeting_id == meeting.id, MeetingParticipant.status == MeetingParticipantStatus.CONFIRMED)
            ).count()
            organizer_participant = db.query(MeetingParticipant).filter(
                and_(MeetingParticipant.meeting_id == meeting.id, MeetingParticipant.role == MeetingParticipantRole.ORGANIZER)
            ).first()
            creator_info = None
            if organizer_participant and organizer_participant.user:
                creator_info = {
                    "id": organizer_participant.user.id,
                    "realname": organizer_participant.user.realname,
                    "nickname": organizer_participant.user.nickname,
                    "email": organizer_participant.user.email,
                }
            meeting_type_str = meeting.meeting_type.value if hasattr(meeting.meeting_type, "value") else str(meeting.meeting_type)
            meeting_subtype_str = meeting.meeting_subtype.value if meeting.meeting_subtype and hasattr(meeting.meeting_subtype, "value") else str(meeting.meeting_subtype) if meeting.meeting_subtype else None
            status_str = meeting.status.value if hasattr(meeting.status, "value") else str(meeting.status)
            meeting_data.append({
                "id": meeting.id,
                "name": meeting.name or "",
                "description": meeting.description or "",
                "meeting_type": meeting_type_str,
                "meeting_subtype": meeting_subtype_str,
                "location": meeting.location or "",
                "course_name": meeting.course_name or "",
                "venue_name": getattr(meeting, "venue_name", None) or "",
                "tee_time": meeting.tee_times[0] if meeting.tee_times and len(meeting.tee_times) > 0 else None,
                "meeting_time": meeting.meeting_time.isoformat() if meeting.meeting_time else None,
                "max_participants": int(meeting.max_participants) if meeting.max_participants else 0,
                "status": status_str,
                "club_id": meeting.club_id,
                "club_name": meeting.club.name if meeting.club else "",
                "participant_count": participant_count,
                "creator": creator_info,
                "created_at": meeting.created_at.isoformat() if meeting.created_at else None,
                "updated_at": meeting.updated_at.isoformat() if meeting.updated_at else None,
            })

        total_pages = (total + limit - 1) // limit
        return {"data": meeting_data, "total": total, "page": page, "limit": limit, "total_pages": total_pages}
    except Exception as e:
        logger.error(f"관리자 모임 목록 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="모임 목록 조회 중 오류가 발생했습니다")


@router.get("/meetings/rounding")
async def admin_get_rounding_meetings(page: int = Query(1, ge=1, description="페이지 번호"),
                                      limit: int = Query(10, ge=1, le=100, description="페이지당 항목 수"),
                                      status: str | None = Query(None, description="모임 상태 필터"),
                                      search: str | None = Query(None, description="검색어"),
                                      club_id: int | None = Query(None, description="클럽 ID 필터"),
                                      current_user: dict = Depends(get_admin_user),
                                      db: Session = Depends(get_db)):
    """관리자용 라운딩 모임 목록 조회 (모든 클럽 조회 가능)"""
    try:
        from models import Meeting, Club, MeetingParticipant, User
        from schemas import MeetingType
        from sqlalchemy import desc, and_, or_

        query = db.query(Meeting).join(Club).filter(Meeting.meeting_type == MeetingType.ROUND)

        # 클럽 필터
        if club_id:
            query = query.filter(Meeting.club_id == club_id)

        # 상태 필터
        if status:
            query = query.filter(Meeting.status == status)

        # 검색 필터
        if search:
            search_filter = or_(Meeting.name.contains(search), Meeting.course_name.contains(search),
                                Meeting.location.contains(search))
            query = query.filter(search_filter)

        # 총 개수 조회
        total = query.count()

        # 페이지네이션 (최신순)
        meetings = query.order_by(desc(Meeting.created_at)).offset((page - 1) * limit).limit(limit).all()

        # 응답 데이터 구성
        meeting_data = []
        for meeting in meetings:
            # 참가자 수 조회
            participant_count = db.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == meeting.id
            ).count()

            # 개설자(생성자) 조회
            creator_info = None
            if meeting.created_by:
                creator = db.query(User).filter(User.id == meeting.created_by).first()
                if creator:
                    creator_info = {
                        "id": creator.id,
                        "realname": creator.realname,
                        "nickname": creator.nickname,
                        "email": creator.email
                    }

            # meeting_type과 status는 데이터베이스에서 문자열로 저장되므로 .value 접근 불필요
            meeting_type_str = meeting.meeting_type
            if hasattr(meeting_type_str, 'value'):
                meeting_type_str = meeting_type_str.value

            meeting_subtype_str = meeting.meeting_subtype
            if meeting_subtype_str and hasattr(meeting_subtype_str, 'value'):
                meeting_subtype_str = meeting_subtype_str.value

            status_str = meeting.status
            if hasattr(status_str, 'value'):
                status_str = status_str.value

            meeting_data.append({
                "id":
                str(meeting.id),
                "name":
                str(meeting.name) if meeting.name else "",
                "description":
                str(meeting.description) if meeting.description else "",
                "meeting_type":
                str(meeting_type_str) if meeting_type_str else "",
                "meeting_subtype":
                str(meeting_subtype_str) if meeting_subtype_str else "",
                "location":
                str(meeting.location) if meeting.location else "",
                "course_name":
                str(meeting.course_name) if meeting.course_name else "",
                "tee_time":
                meeting.tee_times[0] if meeting.tee_times and len(meeting.tee_times) > 0 else None,
                "meeting_time":
                meeting.meeting_time.isoformat() if meeting.meeting_time else None,
                "max_participants":
                int(meeting.max_participants) if meeting.max_participants else 0,
                "status":
                str(status_str) if status_str else "",
                "club_id":
                str(meeting.club_id),
                "club_name":
                str(meeting.club.name) if meeting.club and meeting.club.name else "",
                "participant_count":
                int(participant_count),
                "creator":
                creator_info,
                "created_at":
                meeting.created_at.isoformat() if meeting.created_at else None,
                "updated_at":
                meeting.updated_at.isoformat() if meeting.updated_at else None
            })

        total_pages = (total + limit - 1) // limit

        return {"data": meeting_data, "total": total, "page": page, "limit": limit, "total_pages": total_pages}

    except Exception as e:
        logger.error(f"관리자 라운딩 모임 목록 조회 중 오류: {str(e)}")
        from fastapi import status as http_status
        raise HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail="라운딩 모임 목록 조회 중 오류가 발생했습니다")


@router.get("/meetings/event")
async def admin_get_event_meetings(page: int = Query(1, ge=1, description="페이지 번호"),
                                   limit: int = Query(10, ge=1, le=100, description="페이지당 항목 수"),
                                   status: str | None = Query(None, description="모임 상태 필터"),
                                   search: str | None = Query(None, description="검색어"),
                                   club_id: int | None = Query(None, description="클럽 ID 필터"),
                                   current_user: dict = Depends(get_admin_user),
                                   db: Session = Depends(get_db)):
    """관리자용 이벤트 모임 목록 조회 (모든 클럽 조회 가능)"""
    try:
        from models import Meeting, Club, MeetingParticipant, User
        from schemas import MeetingType
        from sqlalchemy import desc, and_, or_

        query = db.query(Meeting).join(Club).filter(Meeting.meeting_type == MeetingType.SOCIAL)

        # 클럽 필터
        if club_id:
            query = query.filter(Meeting.club_id == club_id)

        # 상태 필터
        if status:
            query = query.filter(Meeting.status == status)

        # 검색 필터
        if search:
            search_filter = or_(Meeting.name.contains(search), Meeting.venue_name.contains(search),
                                Meeting.location.contains(search))
            query = query.filter(search_filter)

        # 총 개수 조회
        total = query.count()

        # 페이지네이션 (최신순)
        meetings = query.order_by(desc(Meeting.created_at)).offset((page - 1) * limit).limit(limit).all()

        # 응답 데이터 구성
        meeting_data = []
        for meeting in meetings:
            # 참가자 수 조회
            participant_count = db.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == meeting.id
            ).count()

            # 개설자(생성자) 조회
            creator_info = None
            if meeting.created_by:
                creator = db.query(User).filter(User.id == meeting.created_by).first()
                if creator:
                    creator_info = {
                        "id": creator.id,
                        "realname": creator.realname,
                        "nickname": creator.nickname,
                        "email": creator.email
                    }

            meeting_data.append({
                "id":
                meeting.id,
                "name":
                meeting.name,
                "description":
                meeting.description,
                "meeting_type":
                meeting.meeting_type.value if hasattr(meeting.meeting_type, 'value') else str(meeting.meeting_type),
                "meeting_subtype":
                meeting.meeting_subtype.value
                if hasattr(meeting.meeting_subtype, 'value') else str(meeting.meeting_subtype),
                "location":
                meeting.location,
                "venue_name":
                meeting.venue_name,
                "meeting_time":
                meeting.meeting_time.isoformat() if meeting.meeting_time else None,
                "max_participants":
                meeting.max_participants,
                "status":
                meeting.status.value if hasattr(meeting.status, 'value') else str(meeting.status),
                "club_id":
                meeting.club_id,
                "club_name":
                meeting.club.name,
                "participant_count":
                participant_count,
                "creator":
                creator_info,
                "created_at":
                meeting.created_at.isoformat() if meeting.created_at else None,
                "updated_at":
                meeting.updated_at.isoformat() if meeting.updated_at else None
            })

        total_pages = (total + limit - 1) // limit

        return {"data": meeting_data, "total": total, "page": page, "limit": limit, "total_pages": total_pages}

    except Exception as e:
        logger.error(f"관리자 이벤트 모임 목록 조회 중 오류: {str(e)}")
        from fastapi import status as http_status
        raise HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail="이벤트 모임 목록 조회 중 오류가 발생했습니다")


# =============================================================================
# 관리자 팀 편성 API (라운딩 전용)
# =============================================================================

@router.get("/meetings/{meeting_id}/teams")
async def get_admin_meeting_teams(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 라운딩 팀 목록 조회"""
    try:
        from models import Meeting, Team, TeamMember, MeetingParticipant, User, Guest
        from sqlalchemy import or_

        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")
        if getattr(meeting.meeting_type, "value", str(meeting.meeting_type)) != "ROUND":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라운딩 모임만 팀 편성을 지원합니다.")

        teams = db.query(Team).filter(Team.meeting_id == meeting_id).all()
        result = []
        for team in teams:
            members = []
            for tm in db.query(TeamMember).filter(TeamMember.team_id == team.id).all():
                if tm.guest_id:
                    p = db.query(MeetingParticipant).filter(
                        MeetingParticipant.meeting_id == meeting_id,
                        MeetingParticipant.guest_id == tm.guest_id,
                    ).first()
                    g = db.query(Guest).filter(Guest.id == tm.guest_id).first()
                    members.append({"user_name": g.name if g else "게스트", "user_nickname": g.name if g else "게스트"})
                elif tm.user_id:
                    p = db.query(MeetingParticipant).filter(
                        MeetingParticipant.meeting_id == meeting_id,
                        MeetingParticipant.user_id == tm.user_id,
                    ).first()
                    u = db.query(User).filter(User.id == tm.user_id).first()
                    members.append({"user_name": u.realname or u.nickname if u else "-", "user_nickname": u.nickname if u else "-"})
            result.append({
                "id": team.id, "name": team.name or f"팀 {team.id}",
                "meeting_id": team.meeting_id, "members": members,
                "formation_mode": team.formation_mode, "status": team.status,
            })
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 팀 목록 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/meetings/{meeting_id}/teams")
async def create_admin_team(
    meeting_id: int,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 팀 생성"""
    try:
        from models import Meeting, Team
        from schemas import TeamFormationMode, TeamStatus

        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")
        if getattr(meeting.meeting_type, "value", str(meeting.meeting_type)) != "ROUND":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라운딩 모임만 팀 편성을 지원합니다.")

        name = (data.get("name") or "").strip() or f"팀"
        formation_mode = data.get("formation_mode") or TeamFormationMode.GENDER_MIXED_HANDICAP.value
        if isinstance(formation_mode, str) and not hasattr(TeamFormationMode, formation_mode):
            formation_mode = TeamFormationMode.GENDER_MIXED_HANDICAP.value

        team = Team(
            name=name,
            meeting_id=meeting_id,
            formation_mode=formation_mode if isinstance(formation_mode, str) else getattr(formation_mode, "value", "GENDER_MIXED_HANDICAP"),
            status="DRAFT",
            formation_notes=data.get("formation_notes"),
        )
        db.add(team)
        db.commit()
        db.refresh(team)
        return {"id": team.id, "name": team.name, "meeting_id": team.meeting_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 팀 생성 중 오류: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.put("/meetings/{meeting_id}/teams/{team_id}")
async def update_admin_team(
    meeting_id: int,
    team_id: int,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 팀 수정"""
    try:
        from models import Team

        team = db.query(Team).filter(Team.id == team_id, Team.meeting_id == meeting_id).first()
        if not team:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="팀을 찾을 수 없습니다.")

        if "name" in data and data["name"] is not None:
            team.name = str(data["name"]).strip() or team.name
        db.commit()
        db.refresh(team)
        return {"id": team.id, "name": team.name}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 팀 수정 중 오류: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.delete("/meetings/{meeting_id}/teams/{team_id}")
async def delete_admin_team(
    meeting_id: int,
    team_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 팀 삭제"""
    try:
        from models import Team, TeamMember

        team = db.query(Team).filter(Team.id == team_id, Team.meeting_id == meeting_id).first()
        if not team:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="팀을 찾을 수 없습니다.")

        db.query(TeamMember).filter(TeamMember.team_id == team_id).delete()
        db.delete(team)
        db.commit()
        return {"message": "팀이 삭제되었습니다."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 팀 삭제 중 오류: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/meetings/{meeting_id}/teams/auto-formation")
async def admin_auto_form_teams(
    meeting_id: int,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 자동 팀 편성 (모집마감/권한 체크 없음)"""
    try:
        from models import Meeting, MeetingParticipant, Team
        from schemas import MeetingType, MeetingParticipantStatus, TeamFormationMode, TeamFormationRequest
        from utils.team_formation import TeamFormationEngine
        from sqlalchemy.orm import joinedload
        from sqlalchemy import or_

        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")
        if meeting.meeting_type != MeetingType.ROUND:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라운딩 모임만 팀 편성을 지원합니다.")

        formation_mode_str = data.get("formation_mode", "GENDER_MIXED_HANDICAP")
        try:
            formation_mode = TeamFormationMode(formation_mode_str)
        except ValueError:
            formation_mode = TeamFormationMode.GENDER_MIXED_HANDICAP
        team_size = int(data.get("team_size", 4))
        if team_size < 2 or team_size > 4:
            team_size = 4

        formation_request = TeamFormationRequest(
            formation_mode=formation_mode,
            team_size=team_size,
            preferences=data.get("preferences"),
        )

        participants = db.query(MeetingParticipant).filter(
            MeetingParticipant.meeting_id == meeting_id,
            MeetingParticipant.status == MeetingParticipantStatus.CONFIRMED,
        ).options(joinedload(MeetingParticipant.user)).all()

        if len(participants) < 4:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="팀 편성을 위해서는 최소 4명의 확정된 참가자가 필요합니다."
            )

        from models import TeamMember
        existing_teams = db.query(Team).filter(Team.meeting_id == meeting_id).all()
        for t in existing_teams:
            db.query(TeamMember).filter(TeamMember.team_id == t.id).delete()
            db.delete(t)
        db.commit()

        formation_engine = TeamFormationEngine(db)
        teams = formation_engine.create_teams_from_formation(meeting_id, formation_request, participants)

        from models import User, Guest

        team_list = []
        for team in teams:
            members = []
            for tm in team.members:
                if tm.user_id:
                    u = db.query(User).filter(User.id == tm.user_id).first()
                    members.append({"user_name": u.realname or u.nickname if u else "-", "user_nickname": u.nickname if u else "-"})
                elif tm.guest_id:
                    g = db.query(Guest).filter(Guest.id == tm.guest_id).first()
                    members.append({"user_name": g.name if g else "게스트", "user_nickname": g.name if g else "게스트"})
            team_list.append({
                "id": team.id, "name": team.name, "meeting_id": team.meeting_id,
                "members": members, "member_count": len(members),
            })
        summary = formation_engine.get_formation_summary(teams) if teams else {}
        return {
            "teams": team_list,
            "total_teams": len(team_list),
            "unassigned_participants": [],
            "total_participants": len(participants),
            "formation_summary": summary,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 자동 팀 편성 중 오류: {str(e)}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/meetings/{meeting_id}/participants")
async def get_admin_meeting_participants(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 모임 참가자 목록 조회 (라운딩/소셜 공통)"""
    try:
        from models import Meeting, MeetingParticipant, User, Guest
        from schemas import MeetingParticipantStatus

        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).all()
        result = []
        for p in participants:
            status_val = p.status.value if hasattr(p.status, "value") else str(p.status) if p.status else "CONFIRMED"
            role_val = p.role.value if hasattr(p.role, "value") else str(p.role) if p.role else "PARTICIPANT"
            if p.guest_id:
                guest = db.query(Guest).filter(Guest.id == p.guest_id).first()
                name = guest.name if guest else "게스트"
                ggender = guest.gender.value if guest and guest.gender and hasattr(guest.gender, 'value') else (
                    str(guest.gender) if guest and guest.gender else None)
                result.append({
                    "id": p.id, "user_id": p.user_id, "guest_id": p.guest_id,
                    "user_name": name, "user_nickname": name, "name": name,
                    "status": status_val,
                    "role": role_val,
                    "is_guest": True,
                    "guest_birthdate":
                    (guest.birthdate.date().isoformat() if guest and guest.birthdate and hasattr(guest.birthdate, 'date')
                     else (str(guest.birthdate)[:10] if guest and guest.birthdate else None)),
                    "guest_gender": ggender,
                    "guest_handicap": float(guest.handicap) if guest and guest.handicap is not None else None,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                })
            else:
                user = db.query(User).filter(User.id == p.user_id).first() if p.user_id else None
                if not user:
                    continue
                result.append({
                    "id": p.id, "user_id": p.user_id, "guest_id": None,
                    "user_name": user.realname or "이름 없음", "user_nickname": user.nickname or "닉네임 없음",
                    "name": user.realname or "이름 없음",
                    "status": status_val,
                    "role": role_val,
                    "is_guest": False, "created_at": p.created_at.isoformat() if p.created_at else None,
                })
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 참가자 목록 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/meetings/{meeting_id}/scores")
async def get_admin_meeting_scores(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """라운딩 모임 참가자별 총타(간단 스코어)·홀별 합산 조회 (백오피스 점수 관리)"""
    try:
        from models import Meeting, MeetingParticipant, UserScoreHistory
        from schemas import MeetingType
        from utils.handicap_calculator import calculate_gross_score

        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")
        _mt = getattr(meeting.meeting_type, "value", meeting.meeting_type)
        if _mt != MeetingType.ROUND.value:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라운딩 모임만 점수를 조회할 수 있습니다.")

        participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).all()
        scores = []
        for p in participants:
            score_val = None
            if p.user_id:
                hist = (
                    db.query(UserScoreHistory)
                    .filter(
                        UserScoreHistory.user_id == p.user_id,
                        UserScoreHistory.meeting_id == meeting_id,
                    )
                    .first()
                )
                if hist:
                    score_val = hist.gross_score
            if score_val is None:
                gross = calculate_gross_score(db, p.id)
                if gross is not None:
                    score_val = gross
            scores.append(
                {
                    "participant_id": p.id,
                    "user_id": p.user_id,
                    "score": score_val,
                }
            )
        return {"scores": scores}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 모임 점수 목록 조회 중 오류: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


class AdminParticipantScoreBody(BaseModel):
    score: int = Field(..., ge=55, le=144, description="라운딩 총타(Gross)")


@router.put("/meetings/{meeting_id}/scores/{participant_id}")
async def put_admin_meeting_participant_score(
    meeting_id: int,
    participant_id: int,
    data: AdminParticipantScoreBody = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """참가자 라운딩 총타 저장·수정 (UserScoreHistory, 회원만)"""
    try:
        from decimal import Decimal

        from models import Meeting, MeetingParticipant, UserScoreHistory
        from schemas import MeetingType
        from utils.handicap_calculator import get_user_handicap_for_formation, update_user_handicap

        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")
        _mt = getattr(meeting.meeting_type, "value", meeting.meeting_type)
        if _mt != MeetingType.ROUND.value:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라운딩 모임만 점수를 저장할 수 있습니다.")

        p = (
            db.query(MeetingParticipant)
            .filter(
                MeetingParticipant.id == participant_id,
                MeetingParticipant.meeting_id == meeting_id,
            )
            .first()
        )
        if not p:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="참가자를 찾을 수 없습니다.")
        if not p.user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="게스트 참가자는 총타를 등록할 수 없습니다.",
            )

        gross = int(data.score)
        handicap_used = get_user_handicap_for_formation(db, p.user_id)
        if handicap_used is None:
            handicap_used = Decimal("0")
        played_at = meeting.meeting_time or get_kst_now()
        net_score = Decimal(str(gross)) - handicap_used

        existing = (
            db.query(UserScoreHistory)
            .filter(
                UserScoreHistory.user_id == p.user_id,
                UserScoreHistory.meeting_id == meeting_id,
            )
            .first()
        )
        if existing:
            existing.gross_score = gross
            existing.net_score = net_score
            existing.handicap_used = handicap_used
        else:
            db.add(
                UserScoreHistory(
                    user_id=p.user_id,
                    meeting_id=meeting_id,
                    gross_score=gross,
                    net_score=net_score,
                    handicap_used=handicap_used,
                    played_at=played_at,
                )
            )
        db.commit()

        try:
            update_user_handicap(db=db, user_id=p.user_id, score_count=5)
        except Exception as ex:
            logger.warning(f"관리자 점수 저장 후 핸디캡 갱신 실패: {ex}")

        return {"success": True, "participant_id": participant_id, "score": gross}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 모임 점수 저장 중 오류: {str(e)}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/meetings/{meeting_id}/participants/{participant_id}/hole-scores", response_model=None)
async def get_admin_participant_hole_scores(
    meeting_id: int,
    participant_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """참가자 홀별 스코어 전체 조회 (백오피스)"""
    try:
        from sqlalchemy import func
        from sqlalchemy.orm import joinedload

        from models import MeetingParticipant, Score
        from schemas import ScoreListResponse
        from routers.meetings.scores import (
            _build_score_response,
            _calc_total_pages,
            _ensure_round_meeting,
        )

        meeting = _ensure_round_meeting(db, meeting_id)
        participant = (
            db.query(MeetingParticipant)
            .filter(
                MeetingParticipant.id == participant_id,
                MeetingParticipant.meeting_id == meeting_id,
            )
            .first()
        )
        if not participant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="참가자를 찾을 수 없습니다.")

        scores_query = (
            db.query(Score)
            .options(
                joinedload(Score.participant).joinedload(MeetingParticipant.user),
                joinedload(Score.participant).joinedload(MeetingParticipant.meeting),
            )
            .filter(Score.participant_id == participant.id)
        )
        total = (
            scores_query.enable_eagerloads(False).order_by(None).with_entities(func.count(Score.id)).scalar() or 0
        )
        scores = scores_query.order_by(Score.hole_number).all()
        score_responses = [_build_score_response(s, meeting=meeting) for s in scores]
        limit = max(total, 1)
        return ScoreListResponse(
            scores=score_responses,
            total=total,
            page=1,
            limit=limit,
            total_pages=_calc_total_pages(total, limit) if limit else 0,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 홀별 스코어 조회 오류: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/meetings/{meeting_id}/participants/{participant_id}/hole-scores", response_model=None)
async def admin_create_participant_hole_score(
    meeting_id: int,
    participant_id: int,
    score_data: MeetingParticipantScoreCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """홀별 스코어 등록 (백오피스)"""
    try:
        from sqlalchemy.orm import joinedload

        from models import MeetingParticipant, Score
        from routers.meetings.scores import (
            _build_score_response,
            _ensure_round_meeting,
            _set_participant_hole_score_flag,
        )

        meeting = _ensure_round_meeting(db, meeting_id)
        participant = (
            db.query(MeetingParticipant)
            .filter(
                MeetingParticipant.id == participant_id,
                MeetingParticipant.meeting_id == meeting_id,
            )
            .first()
        )
        if not participant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="참가자를 찾을 수 없습니다.")

        existing_score = (
            db.query(Score)
            .filter(
                Score.participant_id == participant.id,
                Score.hole_number == score_data.hole_number,
            )
            .first()
        )
        if existing_score:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"홀 {score_data.hole_number}번의 스코어가 이미 있습니다. 수정하거나 삭제 후 다시 등록하세요.",
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

        score = (
            db.query(Score)
            .options(
                joinedload(Score.participant).joinedload(MeetingParticipant.user),
                joinedload(Score.participant).joinedload(MeetingParticipant.meeting),
            )
            .filter(Score.id == score.id)
            .first()
        )
        return _build_score_response(score, meeting=meeting)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"관리자 홀별 스코어 등록 오류: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.put("/meetings/{meeting_id}/participants/{participant_id}/hole-scores/{score_id}", response_model=None)
async def admin_update_participant_hole_score(
    meeting_id: int,
    participant_id: int,
    score_id: int,
    score_data: ScoreUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """홀별 스코어 수정 (백오피스)"""
    try:
        from sqlalchemy.orm import joinedload

        from models import MeetingParticipant, Score
        from routers.meetings.scores import _build_score_response, _ensure_round_meeting

        meeting = _ensure_round_meeting(db, meeting_id)
        participant = (
            db.query(MeetingParticipant)
            .filter(
                MeetingParticipant.id == participant_id,
                MeetingParticipant.meeting_id == meeting_id,
            )
            .first()
        )
        if not participant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="참가자를 찾을 수 없습니다.")

        score = (
            db.query(Score)
            .filter(
                Score.id == score_id,
                Score.participant_id == participant.id,
            )
            .first()
        )
        if not score:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="스코어를 찾을 수 없습니다.")

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
            existing_score = (
                db.query(Score)
                .filter(
                    Score.participant_id == participant.id,
                    Score.hole_number == next_hole_number,
                    Score.id != score_id,
                )
                .first()
            )
            if existing_score:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"홀 {next_hole_number}번의 스코어가 이미 등록되어 있습니다.",
                )

        for field, value in update_data.items():
            setattr(score, field, value)

        db.commit()
        db.refresh(score)

        score = (
            db.query(Score)
            .options(
                joinedload(Score.participant).joinedload(MeetingParticipant.user),
                joinedload(Score.participant).joinedload(MeetingParticipant.meeting),
            )
            .filter(Score.id == score.id)
            .first()
        )
        return _build_score_response(score, meeting=meeting)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"관리자 홀별 스코어 수정 오류: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.delete("/meetings/{meeting_id}/participants/{participant_id}/hole-scores/{score_id}", response_model=None)
async def admin_delete_participant_hole_score(
    meeting_id: int,
    participant_id: int,
    score_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """홀별 스코어 삭제 (백오피스)"""
    try:
        from models import MeetingParticipant, Score
        from schemas import MessageResponse
        from routers.meetings.scores import _ensure_round_meeting, _sync_participant_hole_score_flag

        _ensure_round_meeting(db, meeting_id)
        participant = (
            db.query(MeetingParticipant)
            .filter(
                MeetingParticipant.id == participant_id,
                MeetingParticipant.meeting_id == meeting_id,
            )
            .first()
        )
        if not participant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="참가자를 찾을 수 없습니다.")

        score = (
            db.query(Score)
            .filter(
                Score.id == score_id,
                Score.participant_id == participant.id,
            )
            .first()
        )
        if not score:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="스코어를 찾을 수 없습니다.")

        db.delete(score)
        db.flush()
        _sync_participant_hole_score_flag(db, participant.id)
        db.commit()
        return MessageResponse(success=True, message="스코어가 삭제되었습니다.")
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"관리자 홀별 스코어 삭제 오류: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


class MarkParticipantPaidBody(BaseModel):
    """납부 완료 표시 요청"""
    user_id: int | None = None
    guest_id: int | None = None
    is_paid: bool = True
    amount_paid: float | None = None


@router.patch("/meetings/{meeting_id}/expenses/{expense_id}/participants/mark-paid")
async def mark_participant_paid(
    meeting_id: int,
    expense_id: int,
    data: MarkParticipantPaidBody = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """정산 참가자 납부 완료/미완료 표시 (user_id 또는 guest_id로 해당 참가자의 모든 ExpenseItemParticipant 일괄 업데이트)"""
    try:
        from models import Meeting, Expense, ExpenseItem, ExpenseItemParticipant
        from decimal import Decimal

        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")
        expense = db.query(Expense).filter(
            Expense.id == expense_id,
            Expense.meeting_id == meeting_id
        ).first()
        if not expense:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="정산을 찾을 수 없습니다.")

        user_id = data.user_id
        guest_id = data.guest_id
        if (user_id is None and guest_id is None) or (user_id is not None and guest_id is not None):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="user_id 또는 guest_id 중 하나만 지정해주세요.")

        items = db.query(ExpenseItem).filter(ExpenseItem.expense_id == expense_id).all()
        item_ids = [i.id for i in items]
        filters = [
            ExpenseItemParticipant.expense_item_id.in_(item_ids),
        ]
        if user_id is not None:
            filters.append(ExpenseItemParticipant.user_id == user_id)
        else:
            filters.append(ExpenseItemParticipant.guest_id == guest_id)

        from sqlalchemy import and_
        eips = db.query(ExpenseItemParticipant).filter(and_(*filters)).all()
        if not eips:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="해당 참가자를 찾을 수 없습니다.")

        total_burden = sum(Decimal(str(eip.amount or 0)) for eip in eips)
        paid_at = get_kst_now() if data.is_paid else None
        amount_paid_val = Decimal(str(data.amount_paid)) if data.amount_paid is not None else None
        # 납부완료: (1) 금액 미입력=전액 납부로 간주, (2) 입력 시 부담금 이상일 때만
        if amount_paid_val is not None:
            effective_is_paid = data.is_paid and (amount_paid_val >= total_burden)
        else:
            effective_is_paid = data.is_paid  # 빈 값 = 수동 전액 납부확인
        # amount_paid는 "총 납부액" 의미 - 여러 EIP에 동일 값 저장 시 합산 시 중복되므로 첫 EIP에만 저장
        for idx, eip in enumerate(eips):
            eip.is_paid = effective_is_paid
            eip.paid_at = paid_at if effective_is_paid else None
            if amount_paid_val is not None:
                eip.amount_paid = amount_paid_val if idx == 0 else Decimal("0")
            elif data.is_paid:
                eip.amount_paid = eip.amount or Decimal("0")
            else:
                eip.amount_paid = Decimal("0")
        db.commit()
        return {"message": "납부 상태가 변경되었습니다.", "updated_count": len(eips)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"납부 완료 표시 중 오류: {e}")
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/meetings/{meeting_id}/expenses")
async def get_admin_meeting_expenses(
    meeting_id: int,
    page: int = 1,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 모임 비용 목록 조회 (소셜/라운딩 공통)"""
    try:
        from models import Meeting, Expense, ExpenseItem, ExpenseItemParticipant
        from schemas import ExpenseResponse, ExpenseListResponse
        from routers.meetings.expenses import _build_expense_response

        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        skip = (page - 1) * limit
        expenses = db.query(Expense).filter(Expense.meeting_id == meeting_id).order_by(Expense.created_at.desc()).offset(skip).limit(limit).all()
        total = db.query(Expense).filter(Expense.meeting_id == meeting_id).count()
        expense_responses = [_build_expense_response(e, db) for e in expenses]
        total_pages = (total + limit - 1) // limit if limit > 0 else 0
        return ExpenseListResponse(expenses=expense_responses, total=total, page=page, limit=limit, total_pages=total_pages)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 비용 목록 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


class AdminExpenseBody(BaseModel):
    title: str | None = None
    description: str | None = None
    amount: float | None = None
    category: str | None = None
    settlement_type: str | None = None  # EQUAL_SPLIT | CLUB_FUND | TREASURER_PREPAID


@router.post("/meetings/{meeting_id}/expenses")
async def create_admin_meeting_expense(
    meeting_id: int,
    data: AdminExpenseBody = Body(default=AdminExpenseBody()),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 모임 비용 등록 (description, amount, category 형식 지원)"""
    try:
        from models import Meeting, MeetingParticipant, Expense, ExpenseItem, ExpenseItemParticipant
        from models.enums import ExpenseItemType

        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        d = data.model_dump() if hasattr(data, "model_dump") else (data or {})
        title = d.get("title") or d.get("description") or "비용"
        amount = float(d.get("amount") or 0)
        if amount <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="금액을 입력해주세요.")

        import json as _json
        settlement_type = (d.get("settlement_type") or "EQUAL_SPLIT").upper()
        if settlement_type not in ("EQUAL_SPLIT", "CLUB_FUND", "TREASURER_PREPAID"):
            settlement_type = "EQUAL_SPLIT"
        covered_by_fee = settlement_type == "CLUB_FUND"
        category = d.get("category") or ""
        notes_val = _json.dumps({"category": category, "settlement_type": settlement_type}) if (category or settlement_type != "EQUAL_SPLIT") else (category or None)

        created_by = meeting.created_by or current_user.get("id")
        if not created_by:
            first_p = db.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == meeting_id,
                MeetingParticipant.user_id.isnot(None)
            ).first()
            created_by = first_p.user_id if first_p else current_user.get("id")

        expense = Expense(
            title=title,
            description=d.get("description") or d.get("category") or "",
            meeting_id=meeting_id,
            club_id=meeting.club_id,
            created_by=created_by,
            notes=notes_val or None,
        )
        db.add(expense)
        db.flush()

        ei = ExpenseItem(
            expense_id=expense.id,
            type=ExpenseItemType.OTHER,
            title=title,
            amount=amount,
            covered_by_fee=covered_by_fee,
            order_index=0,
        )
        db.add(ei)
        db.flush()
        db.commit()
        db.refresh(expense)
        from routers.meetings.expenses import _build_expense_response
        return _build_expense_response(expense, db)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"관리자 비용 등록 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.put("/meetings/{meeting_id}/expenses/{expense_id}")
async def update_admin_meeting_expense(
    meeting_id: int,
    expense_id: int,
    data: AdminExpenseBody = Body(default=AdminExpenseBody()),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 모임 비용 수정"""
    try:
        from models import Meeting, Expense, ExpenseItem, ExpenseItemParticipant
        from models.enums import ExpenseItemType

        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        expense = db.query(Expense).filter(
            Expense.id == expense_id,
            Expense.meeting_id == meeting_id
        ).first()
        if not expense:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="비용을 찾을 수 없습니다.")

        d = data.model_dump(exclude_none=True) if hasattr(data, "model_dump") else (data or {})
        if "title" in d:
            expense.title = d["title"]
        if "description" in d:
            expense.description = d["description"]

        import json as _json
        existing_category = expense.notes
        existing_settlement = "EQUAL_SPLIT"
        try:
            parsed = _json.loads(expense.notes or "{}") if isinstance(expense.notes, str) else {}
            if isinstance(parsed, dict):
                existing_category = parsed.get("category", expense.notes)
                existing_settlement = parsed.get("settlement_type", "EQUAL_SPLIT")
        except (_json.JSONDecodeError, TypeError):
            pass

        if "category" in d or "settlement_type" in d:
            category = d.get("category", existing_category) or ""
            settlement_type = (d.get("settlement_type") or existing_settlement).upper()
            if settlement_type not in ("EQUAL_SPLIT", "CLUB_FUND", "TREASURER_PREPAID"):
                settlement_type = "EQUAL_SPLIT"
            expense.notes = _json.dumps({"category": category, "settlement_type": settlement_type}) if (category or settlement_type != "EQUAL_SPLIT") else (category or None)
            covered_by_fee = settlement_type == "CLUB_FUND"
            items = db.query(ExpenseItem).filter(ExpenseItem.expense_id == expense.id).all()
            for item in items:
                item.covered_by_fee = covered_by_fee

        expense.updated_at = datetime.now()

        if "amount" in d:
            amount = float(d["amount"] or 0)
            items = db.query(ExpenseItem).filter(ExpenseItem.expense_id == expense.id).all()
            for item in items:
                if (item.type.value if hasattr(item.type, "value") else str(item.type)) == "OTHER":
                    item.amount = amount
                    break

        db.commit()
        db.refresh(expense)
        from routers.meetings.expenses import _build_expense_response
        return _build_expense_response(expense, db)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"관리자 비용 수정 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.delete("/meetings/{meeting_id}/expenses/{expense_id}")
async def delete_admin_meeting_expense(
    meeting_id: int,
    expense_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 모임 비용 삭제"""
    try:
        from models import Meeting, Expense

        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        expense = db.query(Expense).filter(
            Expense.id == expense_id,
            Expense.meeting_id == meeting_id
        ).first()
        if not expense:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="비용을 찾을 수 없습니다.")

        db.delete(expense)
        db.commit()
        return {"message": "비용이 삭제되었습니다.", "success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"관리자 비용 삭제 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/meetings/{meeting_id}/settlement")
async def get_admin_meeting_settlement(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 모임 정산 조회 (라운딩/소셜 공통)"""
    try:
        from routers.meetings.settlement import _build_settlement_response
        return _build_settlement_response(meeting_id, db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 정산 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/meetings/{meeting_id}/settlement/available-participants")
async def get_admin_settlement_available_participants(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 정산 대상자(참가자) 목록 조회"""
    try:
        from models import Meeting, MeetingParticipant
        from schemas import MeetingParticipantStatus
        from sqlalchemy.orm import joinedload

        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        participants = db.query(MeetingParticipant).options(
            joinedload(MeetingParticipant.user),
            joinedload(MeetingParticipant.guest)
        ).filter(
            MeetingParticipant.meeting_id == meeting_id,
            MeetingParticipant.status == MeetingParticipantStatus.CONFIRMED
        ).all()

        participant_list = []
        for mp in participants:
            if mp.guest_id:
                guest = mp.guest
                participant_list.append({
                    "id": mp.guest_id,
                    "participant_id": mp.id,
                    "name": guest.name if guest else "게스트",
                    "email": None,
                    "role": mp.role.value if mp.role else None,
                    "is_guest": True,
                })
            else:
                user = mp.user
                if user:
                    participant_list.append({
                        "id": user.id,
                        "participant_id": mp.id,
                        "name": user.nickname or user.realname,
                        "email": user.email,
                        "role": mp.role.value if mp.role else None,
                        "is_guest": False,
                    })
        return {"participants": participant_list}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 정산 대상자 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/meetings/{meeting_id}/settlement/rounding")
async def create_admin_rounding_settlement(
    meeting_id: int,
    settlement_data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 라운딩 정산 생성/수정 (정규화 모델: Expense → ExpenseItem → ExpenseItemParticipant)"""
    try:
        from models import Meeting, MeetingParticipant, Expense, ExpenseItem, ExpenseItemParticipant
        from models.enums import ExpenseItemType
        from schemas import MeetingType, MeetingParticipantStatus, MeetingParticipantRole
        from routers.meetings.settlement import send_settlement_created_notification
        from decimal import Decimal
        from sqlalchemy import or_

        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")
        if meeting.meeting_type != MeetingType.ROUND:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라운딩 모임이 아닙니다.")
        if meeting.settlement_confirmed:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="정산이 이미 확정되어 수정할 수 없습니다.")

        created_by_user_id = meeting.created_by
        if not created_by_user_id:
            organizer = db.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == meeting_id,
                MeetingParticipant.role == MeetingParticipantRole.ORGANIZER,
                MeetingParticipant.status == MeetingParticipantStatus.CONFIRMED
            ).first()
            if organizer and organizer.user_id:
                created_by_user_id = organizer.user_id
            else:
                first_p = db.query(MeetingParticipant).filter(
                    MeetingParticipant.meeting_id == meeting_id,
                    MeetingParticipant.status == MeetingParticipantStatus.CONFIRMED,
                    MeetingParticipant.user_id.isnot(None)
                ).first()
                created_by_user_id = first_p.user_id if first_p else None
        if not created_by_user_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="정산 생성자(user_id)를 찾을 수 없습니다. 참가자를 추가해주세요.")

        total_cost = Decimal(str(settlement_data.get('total_cost', 0)))
        green_fee = Decimal(str(settlement_data.get('green_fee', 0)))
        caddy_fee = Decimal(str(settlement_data.get('caddy_fee', 0)))
        cart_fee = Decimal(str(settlement_data.get('cart_fee', 0)))
        other_fee = Decimal(str(settlement_data.get('other_fee', 0)))
        notes = settlement_data.get('notes', '')
        green_fee_participant_ids = settlement_data.get('green_fee_participant_ids', [])
        green_fee_participants = settlement_data.get('green_fee_participants', [])
        green_fee_exempted = settlement_data.get('green_fee_exempted', [])
        green_fee_extra_payer_id = settlement_data.get('green_fee_extra_payer_id')
        green_fee_amounts = settlement_data.get('green_fee_amounts', [])  # [{ participant_id: int, amount: number }]
        cart_fee_participant_ids = settlement_data.get('cart_fee_participant_ids', [])
        cart_fee_participants = settlement_data.get('cart_fee_participants', [])
        cart_fee_exempted = settlement_data.get('cart_fee_exempted', [])
        cart_fee_extra_payer_id = settlement_data.get('cart_fee_extra_payer_id')
        cart_fee_amounts = settlement_data.get('cart_fee_amounts', [])
        caddy_fee_participant_ids = settlement_data.get('caddy_fee_participant_ids', [])
        caddy_fee_participants = settlement_data.get('caddy_fee_participants', [])
        caddy_fee_exempted = settlement_data.get('caddy_fee_exempted', [])
        caddy_fee_extra_payer_id = settlement_data.get('caddy_fee_extra_payer_id')
        caddy_fee_amounts = settlement_data.get('caddy_fee_amounts', [])
        other_expense_items = settlement_data.get('other_expense_items', [])
        exclude_remaining_amount = settlement_data.get('exclude_remaining_amount', False)
        all_covered_by_fee = settlement_data.get('all_covered_by_fee', False)
        green_fee_covered_by_fee = settlement_data.get('green_fee_covered_by_fee', False)
        caddy_fee_covered_by_fee = settlement_data.get('caddy_fee_covered_by_fee', False)
        cart_fee_covered_by_fee = settlement_data.get('cart_fee_covered_by_fee', False)
        # 정산 방법: settlement_data 우선, 없으면 meeting.settlement_method
        # INDIVIDUAL이면 항상 개별정산, EQUAL_SPLIT 또는 미지정이면 같은 참가자일 때 n분의1
        settlement_method_val = settlement_data.get('settlement_method')
        if settlement_method_val is None and meeting.settlement_method is not None:
            settlement_method_val = meeting.settlement_method.value if hasattr(meeting.settlement_method, 'value') else str(meeting.settlement_method)
        use_equal_split = (settlement_method_val != 'INDIVIDUAL')

        def _resolve_participant_ids(participant_ids, fallback_ids):
            """participant_ids(MeetingParticipant.id) 우선, 없으면 fallback_ids(user_id/guest_id) 사용"""
            int_ids = [x for x in (participant_ids or []) if isinstance(x, int)]
            if int_ids:
                mps = db.query(MeetingParticipant).filter(
                    MeetingParticipant.meeting_id == meeting_id,
                    MeetingParticipant.id.in_(int_ids)
                ).all()
                return [mp.user_id if mp.user_id else mp.guest_id for mp in mps]
            return fallback_ids or []

        _green = _resolve_participant_ids(green_fee_participant_ids, green_fee_participants)
        _cart = _resolve_participant_ids(cart_fee_participant_ids, cart_fee_participants)
        _caddy = _resolve_participant_ids(caddy_fee_participant_ids, caddy_fee_participants)
        green_fee_participants = _green
        cart_fee_participants = _cart
        caddy_fee_participants = _caddy

        all_settlement_targets = set()
        if not all_covered_by_fee and not green_fee_covered_by_fee:
            all_settlement_targets.update(green_fee_participants)
        if not all_covered_by_fee and not cart_fee_covered_by_fee:
            all_settlement_targets.update(cart_fee_participants)
        if not all_covered_by_fee and not caddy_fee_covered_by_fee:
            all_settlement_targets.update(caddy_fee_participants)
        if not all_covered_by_fee:
            for item in other_expense_items:
                if bool(item.get("covered_by_fee")):
                    continue
                for pid in (item.get('participants') or []):
                    if pid and pid != 'UNSETTLED':
                        all_settlement_targets.add(pid)

        settlement_targets = list(all_settlement_targets)
        target_count = len(settlement_targets)
        if target_count == 0 and not exclude_remaining_amount and not all_covered_by_fee:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="정산 대상자를 선택해주세요.")
        if not all_covered_by_fee and not green_fee_covered_by_fee and len(green_fee_participants) == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="그린피의 정산 대상자를 선택해주세요.")
        if not all_covered_by_fee and not cart_fee_covered_by_fee and len(cart_fee_participants) == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="카트비의 정산 대상자를 선택해주세요.")
        if not all_covered_by_fee and not caddy_fee_covered_by_fee and len(caddy_fee_participants) == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="캐디피의 정산 대상자를 선택해주세요.")
        if not all_covered_by_fee:
            for idx, item in enumerate(other_expense_items):
                if bool(item.get("covered_by_fee")):
                    continue
                if not (item.get('participants') or []):
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"기타 비용 항목 {idx + 1}의 정산 대상자를 선택해주세요.")

        amount_per_person = Decimal('0') if all_covered_by_fee else (total_cost / target_count if target_count > 0 else Decimal('0'))

        _cost_to_split = total_cost if not all_covered_by_fee and target_count > 0 else Decimal('0')
        _extra_idx = _resolve_extra_payer_index(settlement_targets, settlement_data.get('extra_payer_id'))
        _total_per_person = split_amount_10won(
            _cost_to_split, target_count,
            extra_recipient_indices=[_extra_idx] if _extra_idx is not None else None
        ) if target_count > 0 else []

        # 기존 정산 삭제 후 새로 생성 (수정 시 반영되도록)
        existing = db.query(Expense).filter(Expense.meeting_id == meeting_id).all()
        for ex in existing:
            db.delete(ex)
        db.flush()

        def _add_item_participants(expense_item, participant_ids, exempted_ids, amount_list):
            """amount_list: 10원 단위 나머지 배분법으로 계산된 인원별 금액 (비면제 참가자 순)"""
            exempted_set = set(exempted_ids or [])
            amount_idx = 0
            for pid in participant_ids:
                if pid in exempted_set:
                    continue
                if amount_idx >= len(amount_list):
                    break
                mp = db.query(MeetingParticipant).filter(
                    MeetingParticipant.meeting_id == meeting_id,
                    or_(MeetingParticipant.user_id == pid, MeetingParticipant.guest_id == pid)
                ).first()
                if mp:
                    eip = ExpenseItemParticipant(
                        expense_item_id=expense_item.id,
                        user_id=mp.user_id,
                        guest_id=mp.guest_id,
                        is_exempted=False,
                        amount=amount_list[amount_idx]
                    )
                    db.add(eip)
                    amount_idx += 1

        def _add_item_participants_with_amounts(expense_item, participant_amounts, exempted_ids):
            """participant_amounts: list of { participant_id: MeetingParticipant.id, amount: number }"""
            exempted = set(exempted_ids or [])
            for row in (participant_amounts or []):
                mp_id = row.get('participant_id') if isinstance(row, dict) else getattr(row, 'participant_id', None)
                if mp_id is None:
                    continue
                mp = db.query(MeetingParticipant).filter(
                    MeetingParticipant.meeting_id == meeting_id,
                    MeetingParticipant.id == int(mp_id)
                ).first()
                if not mp or mp_id in exempted:
                    continue
                amt = Decimal(str(row.get('amount', 0) if isinstance(row, dict) else getattr(row, 'amount', 0)))
                eip = ExpenseItemParticipant(
                    expense_item_id=expense_item.id,
                    user_id=mp.user_id,
                    guest_id=mp.guest_id,
                    is_exempted=False,
                    amount=amt
                )
                db.add(eip)

        expense = Expense(
            title=f"{meeting.name} 라운딩 정산",
            description=f"그린피: {green_fee:,}원, 캐디피: {caddy_fee:,}원, 카트비: {cart_fee:,}원, 기타: {other_fee:,}원",
            meeting_id=meeting_id,
            club_id=meeting.club_id,
            created_by=created_by_user_id,
            notes=notes,
            exclude_remaining_amount=exclude_remaining_amount,
        )
        db.add(expense)
        db.flush()

        def _eff(pids, exempted):
            return [p for p in (pids or []) if p not in set(exempted or [])]
        _g_eff = _eff(green_fee_participants, green_fee_exempted)
        _c_eff = _eff(caddy_fee_participants, caddy_fee_exempted)
        _k_eff = _eff(cart_fee_participants, cart_fee_exempted)
        _same_targets = (
            set(_g_eff) == set(settlement_targets) and
            set(_c_eff) == set(settlement_targets) and
            set(_k_eff) == set(settlement_targets)
        )
        _item_amounts = []
        if not green_fee_covered_by_fee and green_fee > 0:
            _item_amounts.append(green_fee)
        if not caddy_fee_covered_by_fee and caddy_fee > 0:
            _item_amounts.append(caddy_fee)
        if not cart_fee_covered_by_fee and cart_fee > 0:
            _item_amounts.append(cart_fee)
        for oi in (other_expense_items or []):
            if bool(oi.get("covered_by_fee")):
                continue
            am = Decimal(str(oi.get('amount', 0)))
            if am > 0:
                _item_amounts.append(am)
        _exact_allocations = []
        if use_equal_split and _total_per_person and _cost_to_split > 0 and _item_amounts and _same_targets:
            if not green_fee_amounts and not caddy_fee_amounts and not cart_fee_amounts:
                _exact_allocations = allocate_items_to_exact_totals(_total_per_person, _item_amounts, _cost_to_split)
        _alloc_idx = 0

        order_idx = 0
        if green_fee > 0:
            gf_item = ExpenseItem(
                expense_id=expense.id,
                type=ExpenseItemType.GREEN_FEE,
                amount=green_fee,
                covered_by_fee=green_fee_covered_by_fee,
                order_index=order_idx,
            )
            db.add(gf_item)
            db.flush()
            if not green_fee_covered_by_fee and green_fee_participants:
                if green_fee_amounts:
                    _add_item_participants_with_amounts(gf_item, green_fee_amounts, green_fee_exempted)
                else:
                    exempted = set(green_fee_exempted or [])
                    effective_ids = [p for p in green_fee_participants if p not in exempted]
                    if _exact_allocations and not green_fee_covered_by_fee and green_fee > 0:
                        alloc_row = _exact_allocations[_alloc_idx]
                        amount_list = [alloc_row[settlement_targets.index(pid)] for pid in effective_ids]
                        _alloc_idx += 1
                    elif use_equal_split and effective_ids and _total_per_person and set(effective_ids) == set(settlement_targets) and _cost_to_split > 0:
                        allocated = allocate_by_total(green_fee, _cost_to_split, _total_per_person)
                        amount_list = [allocated[settlement_targets.index(pid)] for pid in effective_ids]
                    else:
                        extra_idx = _resolve_extra_payer_index(effective_ids, green_fee_extra_payer_id)
                        amount_list = split_amount_10won(
                            green_fee, len(effective_ids),
                            extra_recipient_indices=[extra_idx] if extra_idx is not None else None
                        ) if effective_ids else []
                    _add_item_participants(gf_item, green_fee_participants, green_fee_exempted, amount_list)
            order_idx += 1

        if caddy_fee > 0:
            cf_item = ExpenseItem(
                expense_id=expense.id,
                type=ExpenseItemType.CADDY_FEE,
                amount=caddy_fee,
                covered_by_fee=caddy_fee_covered_by_fee,
                order_index=order_idx,
            )
            db.add(cf_item)
            db.flush()
            if not caddy_fee_covered_by_fee and caddy_fee_participants:
                if caddy_fee_amounts:
                    _add_item_participants_with_amounts(cf_item, caddy_fee_amounts, caddy_fee_exempted)
                else:
                    exempted = set(caddy_fee_exempted or [])
                    effective_ids = [p for p in caddy_fee_participants if p not in exempted]
                    if _exact_allocations and not caddy_fee_covered_by_fee and caddy_fee > 0:
                        alloc_row = _exact_allocations[_alloc_idx]
                        amount_list = [alloc_row[settlement_targets.index(pid)] for pid in effective_ids]
                        _alloc_idx += 1
                    elif use_equal_split and effective_ids and _total_per_person and set(effective_ids) == set(settlement_targets) and _cost_to_split > 0:
                        allocated = allocate_by_total(caddy_fee, _cost_to_split, _total_per_person)
                        amount_list = [allocated[settlement_targets.index(pid)] for pid in effective_ids]
                    else:
                        extra_idx = _resolve_extra_payer_index(effective_ids, caddy_fee_extra_payer_id)
                        amount_list = split_amount_10won(
                            caddy_fee, len(effective_ids),
                            extra_recipient_indices=[extra_idx] if extra_idx is not None else None
                        ) if effective_ids else []
                    _add_item_participants(cf_item, caddy_fee_participants, caddy_fee_exempted, amount_list)
            order_idx += 1

        if cart_fee > 0:
            crf_item = ExpenseItem(
                expense_id=expense.id,
                type=ExpenseItemType.CART_FEE,
                amount=cart_fee,
                covered_by_fee=cart_fee_covered_by_fee,
                order_index=order_idx,
            )
            db.add(crf_item)
            db.flush()
            if not cart_fee_covered_by_fee and cart_fee_participants:
                if cart_fee_amounts:
                    _add_item_participants_with_amounts(crf_item, cart_fee_amounts, cart_fee_exempted)
                else:
                    exempted = set(cart_fee_exempted or [])
                    effective_ids = [p for p in cart_fee_participants if p not in exempted]
                    if _exact_allocations and not cart_fee_covered_by_fee and cart_fee > 0:
                        alloc_row = _exact_allocations[_alloc_idx]
                        amount_list = [alloc_row[settlement_targets.index(pid)] for pid in effective_ids]
                        _alloc_idx += 1
                    elif use_equal_split and effective_ids and _total_per_person and set(effective_ids) == set(settlement_targets) and _cost_to_split > 0:
                        allocated = allocate_by_total(cart_fee, _cost_to_split, _total_per_person)
                        amount_list = [allocated[settlement_targets.index(pid)] for pid in effective_ids]
                    else:
                        extra_idx = _resolve_extra_payer_index(effective_ids, cart_fee_extra_payer_id)
                        amount_list = split_amount_10won(
                            cart_fee, len(effective_ids),
                            extra_recipient_indices=[extra_idx] if extra_idx is not None else None
                        ) if effective_ids else []
                    _add_item_participants(crf_item, cart_fee_participants, cart_fee_exempted, amount_list)
            order_idx += 1

        for oi in (other_expense_items or []):
            amt = Decimal(str(oi.get('amount', 0)))
            if amt <= 0:
                continue
            participant_ids = oi.get('participant_ids') or []
            participant_amounts = oi.get('participant_amounts') or []
            participants = oi.get('participants') or []
            resolved = _resolve_participant_ids(participant_ids, participants) if participant_ids or participants else []
            title = oi.get('title') or oi.get('name') or '기타 비용'
            _om = oi.get("memo")
            _om_str = (str(_om).strip() if _om is not None else "") or None
            other_covered = bool(oi.get("covered_by_fee"))
            other_item = ExpenseItem(
                expense_id=expense.id,
                type=ExpenseItemType.OTHER,
                title=title,
                memo=_om_str,
                amount=amt,
                covered_by_fee=other_covered,
                order_index=order_idx,
            )
            db.add(other_item)
            db.flush()
            if other_covered:
                order_idx += 1
                continue
            if participant_amounts:
                _add_item_participants_with_amounts(other_item, participant_amounts, [])
            elif resolved:
                if _exact_allocations and _alloc_idx < len(_exact_allocations) and set(resolved) == set(settlement_targets):
                    alloc_row = _exact_allocations[_alloc_idx]
                    amount_list = [alloc_row[settlement_targets.index(pid)] for pid in resolved]
                    _alloc_idx += 1
                else:
                    extra_payer_id = oi.get('extra_payer_id')
                    extra_idx = _resolve_extra_payer_index(resolved, extra_payer_id)
                    amount_list = split_amount_10won(
                        amt, len(resolved),
                        extra_recipient_indices=[extra_idx] if extra_idx is not None else None
                    )
                _add_item_participants(other_item, resolved, [], amount_list)
            order_idx += 1

        db.commit()
        logger.info(f"관리자 라운딩 정산 생성 완료 - expense.id: {expense.id}")
        try:
            send_settlement_created_notification(meeting_id, db, is_edit=False)
        except Exception as e:
            logger.error(f"정산 알림 전송 실패: {str(e)}")

        return {
            "message": "라운딩 정산이 생성되었습니다.",
            "settlement_id": expense.id,
            "total_cost": float(total_cost),
            "amount_per_person": float(amount_per_person),
            "target_count": target_count
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"관리자 라운딩 정산 생성 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/meetings/{meeting_id}/settlement/social")
async def create_admin_social_settlement(
    meeting_id: int,
    settlement_data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 소셜 모임 정산 생성 (여러 SOCIAL_ITEM 합산 → n분의 1)"""
    try:
        from models import Meeting, MeetingParticipant, Expense, ExpenseItem, ExpenseItemParticipant
        from models.enums import ExpenseItemType, MeetingType
        from routers.meetings.settlement import send_settlement_created_notification
        from decimal import Decimal
        from sqlalchemy import or_

        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")
        if meeting.meeting_type != MeetingType.SOCIAL:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="소셜 모임이 아닙니다.")
        if meeting.settlement_confirmed:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="정산이 이미 확정되어 수정할 수 없습니다.")

        expense_items_data = settlement_data.get("expense_items", [])
        settlement_targets = settlement_data.get("settlement_targets", [])
        notes = settlement_data.get("notes", "")
        exclude_remaining_amount = settlement_data.get("exclude_remaining_amount", False)
        default_extra_payer_id = settlement_data.get("extra_payer_id")

        total_cost = Decimal(str(settlement_data.get("total_cost", 0)))
        if total_cost <= 0:
            total_cost = sum(Decimal(str(item.get("amount", 0))) for item in expense_items_data)

        target_count = len(settlement_targets)
        if not exclude_remaining_amount and target_count == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="정산 대상자를 선택해주세요.")

        cost_to_split = sum(
            Decimal(str(x.get("amount", 0)))
            for x in (expense_items_data or [])
            if Decimal(str(x.get("amount", 0))) > 0 and not bool(x.get("covered_by_fee"))
        )
        amount_per_person = (
            Decimal("0")
            if (exclude_remaining_amount or target_count == 0)
            else cost_to_split / target_count
        )

        def _add_social_participants(expense_item, participant_ids, amount_list):
            """amount_list: 10원 단위 나머지 배분법으로 계산된 인원별 금액"""
            for idx, pid in enumerate(participant_ids):
                if idx >= len(amount_list):
                    break
                pid_int = int(pid) if pid is not None else None
                if pid_int is None:
                    continue
                mp = db.query(MeetingParticipant).filter(
                    MeetingParticipant.meeting_id == meeting_id,
                ).filter(
                    or_(
                        MeetingParticipant.id == pid_int,
                        MeetingParticipant.user_id == pid_int,
                        MeetingParticipant.guest_id == pid_int,
                    ),
                ).first()
                if mp:
                    eip = ExpenseItemParticipant(
                        expense_item_id=expense_item.id,
                        user_id=mp.user_id,
                        guest_id=mp.guest_id,
                        is_exempted=False,
                        amount=amount_list[idx],
                    )
                    db.add(eip)

        existing = db.query(Expense).filter(Expense.meeting_id == meeting_id).first()
        if existing:
            db.delete(existing)
            db.flush()

        created_by = meeting.created_by or current_user.get("id")
        if not created_by:
            first_p = db.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == meeting_id,
                MeetingParticipant.user_id.isnot(None),
            ).first()
            created_by = first_p.user_id if first_p else current_user.get("id")

        expense = Expense(
            title=f"{meeting.name} 소셜 모임 정산",
            description=f"총 {len(expense_items_data)}개 항목",
            meeting_id=meeting_id,
            club_id=meeting.club_id,
            created_by=created_by,
            notes=notes,
            exclude_remaining_amount=exclude_remaining_amount,
        )
        db.add(expense)
        db.flush()

        for idx, item in enumerate(expense_items_data or []):
            amt = Decimal(str(item.get("amount", 0)))
            if amt <= 0:
                continue
            title = item.get("title") or item.get("name") or item.get("description") or f"항목 {idx + 1}"
            _m = item.get("memo")
            _m_str = (str(_m).strip() if _m is not None else "") or None
            item_covered = bool(item.get("covered_by_fee"))
            ei = ExpenseItem(
                expense_id=expense.id,
                type=ExpenseItemType.SOCIAL_ITEM,
                title=title,
                memo=_m_str,
                amount=amt,
                covered_by_fee=item_covered,
                order_index=idx,
            )
            db.add(ei)
            db.flush()
            if (
                not item_covered
                and not exclude_remaining_amount
                and settlement_targets
                and target_count > 0
            ):
                extra_payer_id = item.get("extra_payer_id", default_extra_payer_id)
                extra_idx = _resolve_extra_payer_index(settlement_targets, extra_payer_id)
                amount_list = split_amount_10won(
                    amt, target_count,
                    extra_recipient_indices=[extra_idx] if extra_idx is not None else None
                )
                _add_social_participants(ei, settlement_targets, amount_list)

        db.commit()
        logger.info(f"관리자 소셜 정산 생성 완료 - expense.id: {expense.id}")
        try:
            send_settlement_created_notification(meeting_id, db, is_edit=False)
        except Exception as e:
            logger.error(f"정산 알림 전송 실패: {str(e)}")

        return {
            "message": "소셜 모임 정산이 생성되었습니다.",
            "settlement_id": expense.id,
            "total_cost": float(total_cost),
            "amount_per_person": float(amount_per_person),
            "target_count": target_count,
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"관리자 소셜 정산 생성 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/meetings/{meeting_id}")
async def get_admin_meeting(meeting_id: int,
                            db: Session = Depends(get_db),
                            current_user: dict = Depends(get_admin_user)):
    """관리자용 모임 상세 조회"""
    try:
        from models import Meeting, Club, MeetingParticipant
        from sqlalchemy import and_

        # 모임 조회
        meeting = db.query(Meeting).join(Club).filter(Meeting.id == meeting_id).first()

        if not meeting:
            from fastapi import status as http_status
            raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        # 참가자 수 조회
        participant_count = db.query(MeetingParticipant).filter(
            MeetingParticipant.meeting_id == meeting.id
        ).count()

        st = meeting.status.value if hasattr(meeting.status, 'value') else str(meeting.status)
        sm = meeting.settlement_method.value if meeting.settlement_method and hasattr(
            meeting.settlement_method, 'value') else (
                str(meeting.settlement_method) if meeting.settlement_method else None)

        def _dec_str(d):
            if d is None:
                return None
            try:
                return float(d)
            except Exception:
                return None

        return {
            "id":
            meeting.id,
            "name":
            meeting.name,
            "description":
            meeting.description,
            "meeting_type":
            meeting.meeting_type.value if hasattr(meeting.meeting_type, 'value') else str(meeting.meeting_type),
            "meeting_subtype":
            meeting.meeting_subtype.value
            if hasattr(meeting.meeting_subtype, 'value') else str(meeting.meeting_subtype),
            "location":
            meeting.location,
            "course_name":
            meeting.course_name,
            "venue_name":
            meeting.venue_name,
            "tee_times":
            meeting.tee_times or [],
            "tee_time":
            meeting.tee_times[0] if meeting.tee_times and len(meeting.tee_times) > 0 else None,
            "meeting_time":
            meeting.meeting_time.isoformat() if meeting.meeting_time else None,
            "application_deadline":
            meeting.application_deadline.isoformat() if meeting.application_deadline else None,
            "reservation_name":
            meeting.reservation_name,
            "hole_count":
            meeting.hole_count,
            "team_size":
            meeting.team_size,
            "team_formation_mode":
            meeting.team_formation_mode,
            "green_fee":
            _dec_str(meeting.green_fee),
            "caddy_fee":
            _dec_str(meeting.caddy_fee),
            "cart_fee":
            _dec_str(meeting.cart_fee),
            "total_cost":
            _dec_str(meeting.total_cost),
            "settlement_method":
            sm,
            "is_private":
            bool(getattr(meeting, "is_private", False)),
            "max_participants":
            meeting.max_participants,
            "status":
            st,
            "club_id":
            meeting.club_id,
            "club_name":
            meeting.club.name,
            "participant_count":
            participant_count,
            "created_at":
            meeting.created_at.isoformat() if meeting.created_at else None,
            "updated_at":
            meeting.updated_at.isoformat() if meeting.updated_at else None,
            "social_notes":
            getattr(meeting, "social_notes", None) or None,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 모임 상세 조회 중 오류: {str(e)}")
        from fastapi import status as http_status
        raise HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail="모임 상세 조회 중 오류가 발생했습니다")


@router.post("/meetings/rounding")
async def create_admin_rounding_meeting(meeting_data: dict,
                                        db: Session = Depends(get_db),
                                        current_user: dict = Depends(get_admin_user)):
    """관리자용 라운딩 모임 생성 — 클라이언트 POST /rounds 와 동일 필드 구조 (멤버십 역할 검증 없음)."""
    try:
        from decimal import Decimal
        from sqlalchemy import and_
        from models import Meeting, Club, ClubMembership, MeetingParticipant, MeetingType, ParticipantType
        from models.enums import MeetingSubtype, SettlementMethod, MeetingStatus
        from utils.permissions import MEMBERSHIP_ACTIVE_STATUSES

        club_id = meeting_data.get('club_id')
        if not club_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="클럽을 선택해주세요.")
        club = db.query(Club).filter(Club.id == club_id).first()
        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다.")

        admin_uid = current_user.get('id')

        def _parse_dt(v):
            if v is None or v == '':
                return None
            if isinstance(v, datetime):
                return v.replace(tzinfo=None) if v.tzinfo else v
            s = str(v).strip().replace('Z', '+00:00')
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="날짜/시간 형식이 올바르지 않습니다.")
            return dt.replace(tzinfo=None) if dt.tzinfo else dt

        meeting_time = _parse_dt(meeting_data.get('meeting_time'))
        if not meeting_time:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="모임 시간을 입력해주세요.")

        application_deadline = _parse_dt(meeting_data.get('application_deadline'))
        if not application_deadline:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="신청 마감일시를 입력해주세요.")
        if meeting_time < application_deadline:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="모임 시간은 신청 마감일시 이후여야 합니다.",
            )

        tee_times = meeting_data.get('tee_times')
        if isinstance(tee_times, str):
            tee_times = [x.strip() for x in tee_times.replace(',', ' ').split() if x.strip()]
        if not isinstance(tee_times, list):
            tee_times = []
        tee_times = [str(t).strip() for t in tee_times if str(t).strip()]
        if not tee_times:
            legacy = meeting_data.get('tee_time')
            if legacy and isinstance(legacy, str) and ':' in legacy:
                tee_times = [legacy.strip()]
        if not tee_times:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="티타임을 1개 이상 입력해주세요.")

        try:
            ms_raw = meeting_data.get('meeting_subtype') or 'REGULAR'
            meeting_subtype = MeetingSubtype(ms_raw) if isinstance(ms_raw, str) else ms_raw
        except ValueError:
            meeting_subtype = MeetingSubtype.REGULAR

        try:
            sm_raw = meeting_data.get('settlement_method') or 'EQUAL_SPLIT'
            settlement_method = SettlementMethod(sm_raw) if isinstance(sm_raw, str) else sm_raw
        except ValueError:
            settlement_method = SettlementMethod.EQUAL_SPLIT

        is_private = bool(meeting_data.get('is_private'))

        try:
            max_participants = int(meeting_data.get('max_participants', 4))
        except (TypeError, ValueError):
            max_participants = 4

        try:
            team_size = int(meeting_data.get('team_size') or 4)
        except (TypeError, ValueError):
            team_size = 4

        def _dec(x, default=None):
            if x is None or x == '':
                return default
            try:
                return Decimal(str(x))
            except Exception:
                return default

        gf = _dec(meeting_data.get('green_fee'), Decimal('0')) or Decimal('0')
        cf = _dec(meeting_data.get('caddy_fee'), Decimal('0')) or Decimal('0')
        caf = _dec(meeting_data.get('cart_fee'), Decimal('0')) or Decimal('0')
        total_from_payload = meeting_data.get('total_cost')
        if total_from_payload is not None and total_from_payload != '':
            total_cost = _dec(total_from_payload, gf + cf + caf) or (gf + cf + caf)
        else:
            total_cost = gf + cf + caf

        try:
            hole_count = int(meeting_data.get('hole_count') or 18)
        except (TypeError, ValueError):
            hole_count = 18

        selected_participants = meeting_data.get('selected_participants') or []
        if isinstance(selected_participants, str):
            selected_participants = [
                int(x.strip())
                for x in selected_participants.replace(',', ' ').split()
                if x.strip().isdigit()
            ]
        if not isinstance(selected_participants, list):
            selected_participants = []
        norm_uid = []
        for x in selected_participants:
            try:
                norm_uid.append(int(x))
            except (TypeError, ValueError):
                continue
        selected_participants = norm_uid

        raw_guests = meeting_data.get('selected_guests') or []
        if not isinstance(raw_guests, list):
            raw_guests = []
        guest_payloads = []
        for item in raw_guests:
            if not isinstance(item, dict):
                continue
            nm = (item.get('name') or '').strip()
            if not nm:
                continue
            guest_payloads.append({
                "name": nm,
                "birthdate": item.get('birthdate') or None,
                "gender": item.get("gender"),
                "average_score": item.get("average_score"),
                "handicap": item.get("handicap"),
            })

        if is_private:
            if not selected_participants and not guest_payloads:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="프라이빗 라운딩은 클럽 멤버 1명 이상 또는 게스트 1명 이상을 지정해주세요.",
                )
            for uid in selected_participants:
                member_check = db.query(ClubMembership).filter(
                    and_(
                        ClubMembership.user_id == uid,
                        ClubMembership.club_id == club_id,
                        ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES),
                    )
                ).first()
                if not member_check:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"user_id {uid}는 해당 클럽의 활성 멤버가 아닙니다.",
                    )
        else:
            selected_participants = []
            guest_payloads = []

        meeting = Meeting(
            name=meeting_data.get('name'),
            description=meeting_data.get('description'),
            location=meeting_data.get('location'),
            meeting_time=meeting_time,
            tee_times=tee_times,
            max_participants=max_participants,
            meeting_type=MeetingType.ROUND,
            meeting_subtype=meeting_subtype,
            total_cost=total_cost,
            green_fee=gf,
            caddy_fee=cf,
            cart_fee=caf,
            settlement_method=settlement_method,
            course_name=meeting_data.get('course_name'),
            hole_count=hole_count,
            reservation_name=meeting_data.get('reservation_name'),
            application_deadline=application_deadline,
            team_formation_mode=meeting_data.get('team_formation_mode'),
            team_size=team_size,
            club_id=club_id,
            status=MeetingStatus.SCHEDULED,
            is_private=is_private,
            created_by=admin_uid,
        )

        db.add(meeting)
        db.commit()
        db.refresh(meeting)

        participant_count = 0

        if is_private:
            for uid in selected_participants:
                existing = db.query(MeetingParticipant).filter(
                    and_(MeetingParticipant.meeting_id == meeting.id, MeetingParticipant.user_id == uid)
                ).first()
                if not existing:
                    db.add(
                        MeetingParticipant(
                            meeting_id=meeting.id,
                            user_id=uid,
                            participant_type=ParticipantType.USER,
                        )
                    )
                    participant_count += 1
            db.commit()

            if guest_payloads:
                from utils.team_formation import add_guest_to_meeting
                from models import Gender as ModelGender

                for gd in guest_payloads:
                    gh_dec = None
                    if gd.get("handicap") is not None and str(gd.get("handicap")).strip() != "":
                        try:
                            gh_dec = Decimal(str(gd.get("handicap")))
                        except Exception:
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"게스트 '{gd['name']}'의 핸디캡 형식이 올바르지 않습니다.",
                            )
                    av_int = None
                    if gd.get("average_score") is not None and str(gd.get("average_score")).strip() != "":
                        try:
                            av_int = int(gd.get("average_score"))
                        except (TypeError, ValueError):
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"게스트 '{gd['name']}'의 평균 타수 형식이 올바르지 않습니다.",
                            )
                    gen = None
                    g = gd.get("gender")
                    if g in ("MALE", "FEMALE"):
                        gen = ModelGender[g]
                    try:
                        add_guest_to_meeting(
                            meeting_id=meeting.id,
                            guest_name=gd["name"],
                            guest_handicap=gh_dec,
                            average_score=av_int,
                            guest_birthdate=gd.get("birthdate"),
                            guest_gender=gen,
                            db=db,
                            auto_commit=True,
                        )
                        participant_count += 1
                    except ValueError as ve:
                        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
        else:
            participant = MeetingParticipant(
                meeting_id=meeting.id,
                user_id=admin_uid,
                participant_type=ParticipantType.USER,
            )
            db.add(participant)
            db.commit()
            participant_count = 1

        return {
            "id": meeting.id,
            "name": meeting.name,
            "description": meeting.description,
            "meeting_type": meeting.meeting_type.value if hasattr(meeting.meeting_type, 'value') else str(meeting.meeting_type),
            "meeting_subtype": meeting.meeting_subtype.value
            if meeting.meeting_subtype and hasattr(meeting.meeting_subtype, 'value')
            else str(meeting.meeting_subtype),
            "location": meeting.location,
            "course_name": meeting.course_name,
            "tee_times": meeting.tee_times or [],
            "tee_time": meeting.tee_times[0] if meeting.tee_times and len(meeting.tee_times) > 0 else None,
            "meeting_time": meeting.meeting_time.isoformat() if meeting.meeting_time else None,
            "application_deadline": meeting.application_deadline.isoformat() if meeting.application_deadline else None,
            "max_participants": meeting.max_participants,
            "status": meeting.status.value if hasattr(meeting.status, 'value') else str(meeting.status),
            "club_id": meeting.club_id,
            "club_name": club.name,
            "participant_count": participant_count,
            "created_at": meeting.created_at.isoformat() if meeting.created_at else None,
            "updated_at": meeting.updated_at.isoformat() if meeting.updated_at else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 라운딩 모임 생성 중 오류: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="라운딩 모임 생성 중 오류가 발생했습니다")


@router.post("/meetings/event")
async def create_admin_event_meeting(meeting_data: dict,
                                     db: Session = Depends(get_db),
                                     current_user: dict = Depends(get_admin_user)):
    """관리자용 이벤트 모임 생성"""
    try:
        from models import Meeting, Club, MeetingParticipant, MeetingType, MeetingStatus, ParticipantType
        from models.enums import SocialType, SettlementMethod
        from datetime import datetime

        # 클럽 존재 확인
        club = db.query(Club).filter(Club.id == meeting_data.get('club_id')).first()
        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다.")

        def _parse_dt(key):
            raw = meeting_data.get(key)
            if not raw:
                return None
            return datetime.fromisoformat(str(raw).replace('Z', '+00:00'))

        social_type_raw = meeting_data.get('type') or meeting_data.get('social_type')
        try:
            social_type = SocialType(social_type_raw) if social_type_raw else SocialType.CASUAL
        except ValueError:
            social_type = SocialType.CASUAL

        sm_raw = meeting_data.get('settlement_method') or meeting_data.get('social_settlement_method')
        settlement_method = None
        if sm_raw:
            try:
                settlement_method = SettlementMethod(sm_raw)
            except ValueError:
                settlement_method = None

        # 모임 생성
        meeting = Meeting(name=meeting_data.get('name'),
                          description=meeting_data.get('description'),
                          meeting_time=_parse_dt('meeting_time'),
                          application_deadline=_parse_dt('application_deadline'),
                          max_participants=meeting_data.get('max_participants', 20),
                          meeting_type=MeetingType.SOCIAL,
                          venue_name=meeting_data.get('venue_name'),
                          location=meeting_data.get('location'),
                          social_cost=meeting_data.get('social_cost'),
                          social_notes=meeting_data.get('social_notes'),
                          settlement_method=settlement_method,
                          social_type=social_type,
                          club_id=meeting_data.get('club_id'),
                          status=MeetingStatus.SCHEDULED,
                          created_by=current_user['id'])

        db.add(meeting)
        db.commit()
        db.refresh(meeting)

        # 관리자를 참가자로 자동 추가
        participant = MeetingParticipant(
            meeting_id=meeting.id,
            user_id=current_user['id'],
            participant_type=ParticipantType.USER
        )
        db.add(participant)
        db.commit()

        return {
            "id":
            meeting.id,
            "name":
            meeting.name,
            "description":
            meeting.description,
            "meeting_type":
            meeting.meeting_type.value if hasattr(meeting.meeting_type, 'value') else str(meeting.meeting_type),
            "venue_name":
            meeting.venue_name,
            "meeting_time":
            meeting.meeting_time.isoformat() if meeting.meeting_time else None,
            "application_deadline":
            meeting.application_deadline.isoformat() if meeting.application_deadline else None,
            "max_participants":
            meeting.max_participants,
            "social_type":
            meeting.social_type.value if getattr(meeting, 'social_type', None) and hasattr(meeting.social_type, 'value') else None,
            "settlement_method":
            meeting.settlement_method.value if meeting.settlement_method and hasattr(meeting.settlement_method, 'value') else None,
            "status":
            meeting.status.value if hasattr(meeting.status, 'value') else str(meeting.status),
            "club_id":
            meeting.club_id,
            "club_name":
            club.name,
            "participant_count":
            1,
            "created_at":
            meeting.created_at.isoformat() if meeting.created_at else None,
            "updated_at":
            meeting.updated_at.isoformat() if meeting.updated_at else None,
            "social_notes":
            getattr(meeting, "social_notes", None) or None,
        }

    except Exception as e:
        logger.error(f"관리자 이벤트 모임 생성 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="이벤트 모임 생성 중 오류가 발생했습니다")


@router.post("/meetings")
async def create_admin_meeting(meeting_data: dict,
                               db: Session = Depends(get_db),
                               current_user: dict = Depends(get_admin_user)):
    """관리자용 모임 생성 (일반)"""
    # 관리자용 모임 생성은 별도 엔드포인트를 사용하세요
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED,
                        detail="관리자용 모임 생성은 /admin/meetings/rounding 또는 /admin/meetings/event 엔드포인트를 사용하세요")


@router.put("/meetings/{meeting_id}")
async def update_admin_meeting(meeting_id: int,
                               meeting_data: dict,
                               db: Session = Depends(get_db),
                               current_user: dict = Depends(get_admin_user)):
    """관리자용 모임 수정 (라운딩: 티타임·신청마감·비용·프라이빗 참가자 동기화 등 포함)"""
    try:
        from decimal import Decimal
        from models import Meeting, Club, MeetingParticipant
        from models.enums import MeetingSubtype, SettlementMethod

        # 모임 조회
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()

        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")

        previous_status = meeting.status

        update_fields = [
            'name', 'description', 'location', 'meeting_time', 'max_participants', 'status',
            'venue_name', 'social_notes',
        ]
        mt0 = meeting.meeting_type.value if hasattr(meeting.meeting_type, 'value') else str(meeting.meeting_type)
        if mt0 == 'ROUND':
            update_fields += [
                'application_deadline', 'tee_times', 'course_name', 'reservation_name', 'hole_count',
                'meeting_subtype', 'team_formation_mode', 'team_size', 'green_fee', 'caddy_fee',
                'cart_fee', 'total_cost', 'settlement_method', 'is_private', 'club_id',
            ]

        for field in update_fields:
            if field not in meeting_data:
                continue
            value = meeting_data[field]
            if field == 'meeting_time' and value and isinstance(value, str):
                try:
                    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    value = value.replace(tzinfo=None) if value.tzinfo else value
                except (ValueError, TypeError):
                    continue
            elif field == 'application_deadline' and value and isinstance(value, str):
                try:
                    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    value = value.replace(tzinfo=None) if value.tzinfo else value
                except (ValueError, TypeError):
                    continue
            elif field == 'tee_times':
                if isinstance(value, str):
                    value = [x.strip() for x in value.replace(',', ' ').split() if x.strip()]
                if not isinstance(value, list):
                    continue
                value = [str(t).strip() for t in value if str(t).strip()]
            elif field == 'is_private':
                value = bool(value)
            elif field == 'club_id':
                try:
                    cid = int(value)
                except (TypeError, ValueError):
                    continue
                club_chk = db.query(Club).filter(Club.id == cid).first()
                if not club_chk:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다.")
                value = cid
            elif field == 'meeting_subtype' and value:
                try:
                    value = MeetingSubtype(value) if isinstance(value, str) else value
                except ValueError:
                    continue
            elif field == 'settlement_method' and value:
                try:
                    value = SettlementMethod(value) if isinstance(value, str) else value
                except ValueError:
                    continue
            elif field in ('green_fee', 'caddy_fee', 'cart_fee', 'total_cost'):
                if value is None or value == '':
                    continue
                try:
                    value = Decimal(str(value))
                except Exception:
                    continue
            elif field in ('hole_count', 'team_size', 'max_participants'):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            elif field == 'status' and value is not None:
                value = str(value)

            setattr(meeting, field, value)

        mt = meeting.meeting_type.value if hasattr(meeting.meeting_type, 'value') else str(meeting.meeting_type)
        st_str = meeting.status.value if hasattr(meeting.status, 'value') else str(meeting.status)
        if mt == 'ROUND' and st_str == 'SCHEDULED' and meeting.is_private:
            if 'selected_participants' in meeting_data or 'selected_guests' in meeting_data:
                _admin_sync_private_round_participants(db, meeting, meeting.club_id, meeting_data)

        meeting.updated_at = get_kst_now()
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
            logger.error(f"라운딩 상태 변경 알림 전송 실패(관리자 수정) - meeting_id: {meeting_id}, error: {str(e)}")

        # 클럽 정보 조회
        club = db.query(Club).filter(Club.id == meeting.club_id).first()

        # 참가자 수 조회
        participant_count = db.query(MeetingParticipant).filter(
            MeetingParticipant.meeting_id == meeting.id
        ).count()

        return {
            "id":
            meeting.id,
            "name":
            meeting.name,
            "description":
            meeting.description,
            "meeting_type":
            meeting.meeting_type.value if hasattr(meeting.meeting_type, 'value') else str(meeting.meeting_type),
            "meeting_subtype":
            meeting.meeting_subtype.value
            if hasattr(meeting.meeting_subtype, 'value') else str(meeting.meeting_subtype),
            "location":
            meeting.location,
            "course_name":
            meeting.course_name,
            "venue_name":
            meeting.venue_name,
            "tee_time":
            meeting.tee_times[0] if meeting.tee_times and len(meeting.tee_times) > 0 else None,
            "meeting_time":
            meeting.meeting_time.isoformat() if meeting.meeting_time else None,
            "max_participants":
            meeting.max_participants,
            "status":
            meeting.status.value if hasattr(meeting.status, 'value') else str(meeting.status),
            "club_id":
            meeting.club_id,
            "club_name":
            club.name,
            "participant_count":
            participant_count,
            "created_at":
            meeting.created_at.isoformat() if meeting.created_at else None,
            "updated_at":
            meeting.updated_at.isoformat() if meeting.updated_at else None,
            "social_notes":
            getattr(meeting, "social_notes", None) or None,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 모임 수정 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="모임 수정 중 오류가 발생했습니다")


@router.get("/meetings/stats", response_model=MeetingStatsResponse)
async def get_meeting_statistics(
    club_id: int | None = Query(None, description="클럽 ID 필터"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """모임 통계 조회 (관리자)"""
    try:
        from models import Meeting, Club, MeetingParticipant
        from schemas import MeetingStatus, MeetingParticipantStatus

        meetings_query = db.query(Meeting)
        if club_id:
            meetings_query = meetings_query.filter(Meeting.club_id == club_id)

        total_meetings = meetings_query.count()
        active_meetings = meetings_query.filter(Meeting.status == MeetingStatus.SCHEDULED).count()
        canceled_meetings = meetings_query.filter(Meeting.status == MeetingStatus.CANCELED).count()
        completed_meetings = meetings_query.filter(Meeting.status == MeetingStatus.COMPLETED).count()

        type_stats = meetings_query.with_entities(Meeting.meeting_type, func.count(Meeting.id).label("count")).group_by(Meeting.meeting_type).all()
        meetings_by_type = {t.value: c for t, c in type_stats if t}

        twelve_months_ago = datetime.now() - timedelta(days=365)
        monthly_stats = meetings_query.filter(Meeting.created_at >= twelve_months_ago).with_entities(
            extract("year", Meeting.created_at).label("year"),
            extract("month", Meeting.created_at).label("month"),
            func.count(Meeting.id).label("count"),
        ).group_by(extract("year", Meeting.created_at), extract("month", Meeting.created_at)).order_by(
            extract("year", Meeting.created_at), extract("month", Meeting.created_at)
        ).all()
        meetings_by_month = [{"year": int(y), "month": int(m), "count": c} for y, m, c in monthly_stats]

        participants_query = db.query(MeetingParticipant)
        if club_id:
            participants_query = participants_query.join(Meeting).filter(Meeting.club_id == club_id)
        total_participants = participants_query.filter(MeetingParticipant.status == MeetingParticipantStatus.CONFIRMED).count()
        average_participants_per_meeting = round(total_participants / total_meetings, 2) if total_meetings > 0 else 0

        club_stats = meetings_query.with_entities(Meeting.club_id, Club.name.label("club_name"), func.count(Meeting.id).label("count")).join(Club).group_by(Meeting.club_id, Club.name).all()
        meetings_by_club = [{"club_id": cid, "club_name": cname, "count": c} for cid, cname, c in club_stats]

        upcoming_meetings_count = meetings_query.filter(Meeting.meeting_time > datetime.now(), Meeting.status == MeetingStatus.SCHEDULED).count()
        past_meetings_count = meetings_query.filter(Meeting.meeting_time <= datetime.now()).count()

        return MeetingStatsResponse(
            total_meetings=total_meetings, active_meetings=active_meetings,
            canceled_meetings=canceled_meetings, completed_meetings=completed_meetings,
            meetings_by_type=meetings_by_type, meetings_by_month=meetings_by_month,
            total_participants=total_participants, average_participants_per_meeting=average_participants_per_meeting,
            meetings_by_club=meetings_by_club, upcoming_meetings_count=upcoming_meetings_count,
            past_meetings_count=past_meetings_count,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"모임 통계 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.delete("/meetings/{meeting_id}")
async def delete_admin_meeting(meeting_id: int,
                               db: Session = Depends(get_db),
                               current_user: dict = Depends(get_admin_user)):
    """관리자용 모임 삭제"""
    from models import Meeting

    try:
        logger.info(f"관리자 모임 삭제 시작 - meeting_id: {meeting_id}")

        # 모임 조회
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다")

        # 모임 삭제 (실제 삭제 또는 소프트 삭제)
        # 참가자, 팀 등 관련 데이터도 함께 삭제됨 (CASCADE 설정)
        db.delete(meeting)
        db.commit()

        logger.info(f"관리자 모임 삭제 완료 - meeting_id: {meeting_id}")
        return {"message": "모임이 삭제되었습니다"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 모임 삭제 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 오류: {str(e)}")
