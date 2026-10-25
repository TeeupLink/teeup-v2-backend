"""
백오피스 사용자 관리 API
"""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
import logging

from database import get_db
from models import User, Club, ClubMembership
from models import UserStatus
from schemas import UserResponse, PaginatedResponse, MembershipStatus
import schemas
from utils.datetime_utils import get_kst_now
from .deps import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-users"])


@router.get("/users", response_model=PaginatedResponse)
async def get_admin_users(
        page: int = 1,
        limit: int = 10,
        search: str | None = None,
        role_filter: str | None = None,
        status_filter: str | None = None,
        sort_order: str | None = "oldest",
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 사용자 목록 조회"""
    try:
        offset = (page - 1) * limit
        query = db.query(User).filter(
            User.deleted_at.is_(None),
            ~User.email.like("%@guest.local"),
            ~User.nickname.like("guest_%"),
        )
        if search:
            search_filter = f"%{search}%"
            query = query.filter((User.email.ilike(search_filter))
                                 | (User.nickname.ilike(search_filter))
                                 | (User.realname.ilike(search_filter))
                                 | (User.phone_number.ilike(search_filter)))
        if status_filter:
            query = query.filter(User.status == status_filter)
        total_count = query.count()
        # id만으로 정렬 (유일값이라 페이지네이션 안정적, id 오름차순=오래된순/내림차순=최신순)
        if sort_order == "oldest":
            query = query.order_by(User.id.asc())
        else:
            query = query.order_by(User.id.desc())
        users = query.offset(offset).limit(limit).all()

        user_responses = []
        for user in users:
            club_count = db.query(ClubMembership).filter(
                ClubMembership.user_id == user.id,
                ClubMembership.status == MembershipStatus.ACTIVE,
            ).count()
            user_responses.append(
                UserResponse(
                    id=user.id,
                    email=user.email,
                    realname=getattr(user, "realname", None),
                    nickname=user.nickname,
                    phone_number=getattr(user, "phone_number", None),
                    birthdate=getattr(user, "birthdate", None),
                    gender=getattr(user, "gender", None),
                    handicap=user.handicap,
                    average_score=user.average_score,
                    provider=getattr(user, "provider", None),
                    email_verified=getattr(user, "email_verified", None),
                    status=user.status.value if user.status else None,
                    deactivated_at=getattr(user, "deactivated_at", None),
                    needs_terms_agreement=getattr(user, "needs_terms_agreement", None),
                    terms_agreement=getattr(user, "terms_agreement", None),
                    privacy_policy=getattr(user, "privacy_policy", None),
                    privacy_collection=getattr(user, "privacy_collection", None),
                    marketing_consent=getattr(user, "marketing_consent", None),
                    club_count=club_count,
                    created_at=user.created_at,
                    updated_at=user.updated_at,
                ))
        # 응답 내 유저 ID 중복 검증 (백오피스 중복 출력 원인 파악용)
        ids = [u.id for u in user_responses]
        if len(ids) != len(set(ids)):
            dup = [x for x in ids if ids.count(x) > 1]
            logger.error("get_admin_users 중복 user id 감지: count=%s ids=%s 중복=%s", len(ids), ids, list(set(dup)))
            assert False, f"duplicate user ids in response: {list(set(dup))}"
        logger.info("get_admin_users 검증 OK: count=%s page=%s ids_sample=%s", len(ids), page, ids[:20])
        return {
            "data": user_responses,
            "total": total_count,
            "page": page,
            "limit": limit,
            "total_pages": (total_count + limit - 1) // limit,
        }
    except Exception as e:
        logger.error(f"관리자 사용자 목록 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="사용자 목록 조회 중 오류가 발생했습니다",
        )


@router.get("/users/search")
async def search_users_for_club(
        q: str | None = None,
        limit: int = 20,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """클럽 대표자 선택을 위한 사용자 검색"""
    try:
        query = db.query(User).filter(
            User.deleted_at.is_(None),
            User.status == UserStatus.ACTIVE,
        )
        if q:
            search_filter = f"%{q}%"
            query = query.filter((User.email.ilike(search_filter))
                                 | (User.nickname.ilike(search_filter))
                                 | (User.realname.ilike(search_filter)))
        users = query.limit(limit).all()
        user_list = [{
            "id": u.id,
            "email": u.email,
            "nickname": u.nickname,
            "realname": getattr(u, "realname", None),
            "phone_number": getattr(u, "phone_number", None),
            "status": u.status.value if u.status else None,
        } for u in users]
        return {"success": True, "users": user_list, "total": len(user_list)}
    except Exception as e:
        logger.error(f"사용자 검색 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="사용자 검색 중 오류가 발생했습니다",
        )


@router.get("/users/create")
async def get_user_create_form_data(
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """사용자 생성 페이지에 필요한 데이터 조회"""
    return {
        "success": True,
        "data": {
            "roles": ["USER", "ADMIN"],
            "statuses": ["ACTIVE", "INACTIVE", "SUSPENDED"],
            "providers": ["LOCAL", "GOOGLE", "KAKAO", "NAVER"],
        },
    }


@router.get("/users/create/clubs")
async def get_user_create_clubs_data(
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """사용자 생성 시 클럽 목록 조회 (대표자 선택용)"""
    try:
        from models import ClubStatus

        clubs = (db.query(Club).filter(Club.deleted_at.is_(None),
                                       Club.status == ClubStatus.ACTIVE).order_by(Club.created_at.desc()).all())
        club_list = [{
            "id": c.id,
            "display_id": c.display_id,
            "name": c.name,
            "status": c.status.value if hasattr(c.status, "value") else str(c.status),
        } for c in clubs]
        return {"success": True, "clubs": club_list, "total": len(club_list)}
    except Exception as e:
        logger.error(f"클럽 목록 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="클럽 목록 조회 중 오류가 발생했습니다",
        )


@router.get("/users/{user_id}")
async def get_admin_user_detail(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 사용자 상세 조회"""
    try:
        user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다")
        gender_value = None
        if hasattr(user, "gender") and user.gender:
            gender_value = user.gender.value if hasattr(user.gender, "value") else str(user.gender)
        return UserResponse(
            id=user.id,
            email=user.email,
            realname=getattr(user, "realname", None),
            nickname=user.nickname,
            phone_number=getattr(user, "phone_number", None),
            birthdate=getattr(user, "birthdate", None),
            gender=gender_value,
            handicap=float(user.handicap) if user.handicap else None,
            average_score=user.average_score,
            provider=user.provider.value if user.provider else None,
            email_verified=getattr(user, "email_verified", None),
            status=user.status.value if user.status else None,
            deactivated_at=getattr(user, "deactivated_at", None),
            needs_terms_agreement=getattr(user, "needs_terms_agreement", None),
            created_at=user.created_at,
            updated_at=user.updated_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 사용자 상세 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="사용자 상세 조회 중 오류가 발생했습니다",
        )


@router.get("/users/{user_id}/clubs")
async def get_user_clubs(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """사용자별 소속 클럽 목록 조회"""
    try:
        from sqlalchemy import or_

        user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다")
        memberships = (db.query(ClubMembership, Club).join(Club, ClubMembership.club_id == Club.id).filter(
            ClubMembership.user_id == user_id,
            Club.deleted_at.is_(None),
            or_(
                ClubMembership.status == MembershipStatus.ACTIVE,
                ClubMembership.status == "APPROVED",
            ),
        ).all())
        club_data = [{
            "id": club.id,
            "name": club.name,
            "type": club.type.value if hasattr(club.type, "value") else str(club.type),
            "description": club.description,
            "location": club.location,
            "status": club.status.value if hasattr(club.status, "value") else str(club.status),
            "member_role": membership.role.value if hasattr(membership.role, "value") else str(membership.role),
            "joined_at": membership.created_at.isoformat() if membership.created_at else None,
            "club_created_at": club.created_at.isoformat() if club.created_at else None,
        } for membership, club in memberships]
        return {"user_id": user_id, "user_name": user.nickname, "total_clubs": len(club_data), "clubs": club_data}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"사용자 클럽 목록 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="사용자 클럽 목록 조회 중 오류가 발생했습니다",
        )


@router.get("/users/{user_id}/meetings", response_model=schemas.UserMeetingsResponse)
async def get_user_meetings(
        user_id: int,
        page: int = Query(1, ge=1),
        limit: int = Query(10, ge=1, le=100),
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """사용자별 라운딩/소셜 참가 목록 조회"""
    try:
        from models import Meeting, MeetingParticipant, Club, UserScoreHistory
        from schemas import MeetingType
        from sqlalchemy import func, desc

        user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다")
        offset = (page - 1) * limit

        rounding_query = (db.query(MeetingParticipant, Meeting,
                                   Club).join(Meeting, MeetingParticipant.meeting_id == Meeting.id).join(
                                       Club, Meeting.club_id == Club.id).filter(
                                           MeetingParticipant.user_id == user_id,
                                           Meeting.meeting_type == MeetingType.ROUND,
                                           Club.deleted_at.is_(None),
                                       ))
        total_rounding = rounding_query.count()
        rounding_results = (rounding_query.order_by(desc(func.coalesce(
            Meeting.meeting_time, Meeting.created_at))).offset(offset).limit(limit).all())

        social_query = (db.query(MeetingParticipant, Meeting,
                                 Club).join(Meeting, MeetingParticipant.meeting_id == Meeting.id).join(
                                     Club, Meeting.club_id == Club.id).filter(
                                         MeetingParticipant.user_id == user_id,
                                         Meeting.meeting_type == MeetingType.SOCIAL,
                                         Club.deleted_at.is_(None),
                                     ))
        total_social = social_query.count()
        social_results = (social_query.order_by(desc(func.coalesce(
            Meeting.meeting_time, Meeting.created_at))).offset(offset).limit(limit).all())

        rounding_meeting_ids = [m.id for _, m, _ in rounding_results]
        score_histories = {}
        if rounding_meeting_ids:
            scores = db.query(UserScoreHistory).filter(
                UserScoreHistory.user_id == user_id,
                UserScoreHistory.meeting_id.in_(rounding_meeting_ids),
            ).all()
            for s in scores:
                score_histories[s.meeting_id] = s

        rounding_meetings = []
        for participant, meeting, club in rounding_results:
            sh = score_histories.get(meeting.id)
            rounding_meetings.append(
                schemas.UserMeetingItem(
                    meeting_id=meeting.id,
                    meeting_name=meeting.name,
                    club_name=club.name,
                    meeting_time=meeting.meeting_time,
                    status=meeting.status,
                    joined_at=participant.created_at,
                    rounding_completed_at=meeting.rounding_completed_at,
                    has_score=sh is not None,
                    gross_score=sh.gross_score if sh else None,
                    net_score=float(sh.net_score) if sh and sh.net_score else None,
                ))

        social_meetings = []
        for participant, meeting, club in social_results:
            social_meetings.append(
                schemas.UserMeetingItem(
                    meeting_id=meeting.id,
                    meeting_name=meeting.name,
                    club_name=club.name,
                    meeting_time=meeting.meeting_time,
                    status=meeting.status,
                    joined_at=participant.created_at,
                    rounding_completed_at=None,
                    has_score=False,
                    gross_score=None,
                    net_score=None,
                ))

        return schemas.UserMeetingsResponse(
            rounding_meetings=rounding_meetings,
            social_meetings=social_meetings,
            total_rounding=total_rounding,
            total_social=total_social,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"사용자 모임 목록 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="사용자 모임 목록 조회 중 오류가 발생했습니다",
        )


@router.get("/users/{user_id}/handicap-history", response_model=schemas.UserHandicapHistoryResponse)
async def get_user_handicap_history(
        user_id: int,
        page: int = Query(1, ge=1),
        limit: int = Query(10, ge=1, le=100),
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """사용자별 핸디캡 업데이트 이력 조회"""
    try:
        from models import UserScoreHistory, Meeting, Club
        from sqlalchemy import desc

        user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다")
        handicap_info = schemas.HandicapInfo(
            initial_handicap=float(user.handicap_init) if user.handicap_init is not None else None,
            calculated_handicap=float(user.handicap) if user.handicap is not None else None,
            handicap_update_method=user.handicap_update_method.value if user.handicap_update_method else None,
            handicap_calculation_count=user.handicap_calculation_count,
            last_updated_at=user.updated_at,
        )
        offset = (page - 1) * limit
        score_history_query = (db.query(UserScoreHistory, Meeting,
                                        Club).join(Meeting, UserScoreHistory.meeting_id == Meeting.id).join(
                                            Club,
                                            Meeting.club_id == Club.id).filter(UserScoreHistory.user_id == user_id,
                                                                               Club.deleted_at.is_(None)))
        total = score_history_query.count()
        results = (score_history_query.order_by(desc(UserScoreHistory.played_at)).offset(offset).limit(limit).all())
        score_history = [
            schemas.HandicapHistoryItem(
                id=sh.id,
                meeting_id=sh.meeting_id,
                meeting_name=m.name if m else None,
                club_name=c.name if c else None,
                gross_score=sh.gross_score,
                net_score=float(sh.net_score) if sh.net_score else None,
                handicap_used=float(sh.handicap_used),
                played_at=sh.played_at,
                created_at=sh.created_at,
            ) for sh, m, c in results
        ]
        total_pages = (total + limit - 1) // limit if total > 0 else 0
        return schemas.UserHandicapHistoryResponse(
            handicap_info=handicap_info,
            score_history=score_history,
            total=total,
            page=page,
            limit=limit,
            total_pages=total_pages,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"사용자 핸디캡 이력 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="사용자 핸디캡 이력 조회 중 오류가 발생했습니다",
        )


@router.post("/users", response_model=UserResponse)
async def create_admin_user(
        user_data: dict,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 사용자 생성"""
    from routers.users import create_user
    from schemas import UserCreate

    return await create_user(UserCreate(**user_data), db=db, current_user=current_user)


@router.put("/users/{user_id}", response_model=UserResponse)
async def update_admin_user(
        user_id: int,
        user_data: dict,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 사용자 수정"""
    from routers.users import update_user
    from schemas import UserUpdate

    return await update_user(user_id, UserUpdate(**user_data), db=db, current_user=current_user)


@router.delete("/users/{user_id}")
async def delete_admin_user(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 사용자 삭제"""
    try:
        user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다")
        user.original_nickname = user.nickname
        user.nickname = f"탈퇴회원_{int(get_kst_now().timestamp())}"
        user.nickname_locked_until = datetime.now() + timedelta(days=7)
        user.deleted_at = datetime.now()
        user.status = UserStatus.DELETED
        db.commit()
        return {"message": "사용자가 삭제되었습니다"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 사용자 삭제 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )
