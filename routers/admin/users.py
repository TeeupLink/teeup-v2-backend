"""
백오피스 사용자 관리 API
"""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
import logging

from database import get_db
from models import User, Club, ClubMembership, Terms, TermsAgreement, UserActivityLog
from models import UserStatus
from schemas import UserResponse, PaginatedResponse, MembershipStatus
import schemas
from services import admin_user_service as user_svc
from utils import activity, audit
from utils.datetime_utils import get_kst_now
from .deps import get_admin_user
from .pagination import BulkRequest, bulk_run

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-users"])


def _last_logins(db: Session, user_ids: list[int]) -> dict[int, UserActivityLog]:
    """회원별 마지막 LOGIN 활동 (활동 로그 1년 보관 범위 안)"""
    if not user_ids:
        return {}
    latest = (db.query(UserActivityLog.user_id, func.max(UserActivityLog.id).label("id"))
              .filter(UserActivityLog.user_id.in_(user_ids), UserActivityLog.event_type == "LOGIN")
              .group_by(UserActivityLog.user_id).subquery())
    rows = db.query(UserActivityLog).join(latest, UserActivityLog.id == latest.c.id).all()
    return {r.user_id: r for r in rows}


def _detail(db: Session, user: User) -> dict:
    """상세 응답 (SCR-032 헤더·기본정보·골프). UserResponse 필드 + 초기값·최근 로그인"""
    last = _last_logins(db, [user.id]).get(user.id)
    club_count = db.query(ClubMembership).filter(ClubMembership.user_id == user.id,
                                                 ClubMembership.status == MembershipStatus.ACTIVE).count()
    return {
        **user_svc.snapshot(user, ("id", "email", "realname", "nickname", "phone_number", "gender", "provider", "status",
                                   "average_score", "average_score_init", "handicap", "handicap_init",
                                   "handicap_update_method", "handicap_calculation_count", "needs_terms_agreement",
                                   "terms_agreement", "privacy_policy", "privacy_collection", "marketing_consent")),
        "profile_image": user.profile_image,
        "birthdate": user.birthdate.date().isoformat() if user.birthdate else None,
        "email_verified": user.email_verified,
        "deactivated_at": user.deactivated_at,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "club_count": club_count,
        "last_login_at": last.created_at if last else None,
        "last_login_device": (last.meta_json or {}).get("device") if last else None,
    }


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
        logins = _last_logins(db, ids)
        items = [{**u.model_dump(), "last_login_at": logins[u.id].created_at if u.id in logins else None} for u in user_responses]
        return {
            "data": items,
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
        # 이메일·전화번호·실명이 담긴 상세 조회는 개인정보 열람으로 남긴다
        audit.record("VIEW_PII", "USER", user_id)
        return _detail(db, user)
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
            "note": membership.note,
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


class MemberNoteBody(BaseModel):
    note: str | None = Field(None, max_length=2000)


@router.put("/users/{user_id}/clubs/{club_id}/note")
async def update_member_note(
        user_id: int,
        club_id: int,
        body: MemberNoteBody,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """클럽별 회원 메모 (F-034). 앱의 구성원 메모(club_memberships.note)와 같은 칸이라 클럽 리더·매니저도 앱에서 본다"""
    m = db.query(ClubMembership).filter(ClubMembership.user_id == user_id, ClubMembership.club_id == club_id).first()
    if not m:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="이 클럽의 회원이 아닙니다")
    note = (body.note or "").strip() or None
    before = m.note
    if note == before:
        return {"club_id": club_id, "user_id": user_id, "note": note}
    m.note = note
    db.commit()
    audit.record("UPDATE", "CLUB_MEMBER", m.id, before={"note": before}, after={"note": note},
                 meta={"club_id": club_id, "user_id": user_id})
    return {"club_id": club_id, "user_id": user_id, "note": note}


@router.get("/users/{user_id}/terms-agreements")
async def get_user_terms_agreements(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """회원별 약관 동의 이력 (F-111). 최근 동의 먼저 + 게시중 필수 약관 중 아직 동의하지 않은 것"""
    from .terms import _rows

    if not db.query(User.id).filter(User.id == user_id).first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다")
    agreements = (db.query(TermsAgreement).filter(TermsAgreement.user_id == user_id)
                  .order_by(TermsAgreement.agreed_at.desc(), TermsAgreement.id.desc()).all())
    versions = {r["id"]: r for r in _rows(db, db.query(Terms).filter(Terms.id.in_({a.terms_id for a in agreements})).all())} \
        if agreements else {}
    items = []
    for a in agreements:
        v = versions.get(a.terms_id, {})
        items.append({
            "id": a.id,
            "terms_id": a.terms_id,
            "type": v.get("type"),
            "version": v.get("version"),
            "title": v.get("title"),
            "status": v.get("status"),
            "is_required": v.get("is_required"),
            "agreed_at": a.agreed_at.isoformat() if a.agreed_at else None,
            "ip_address": a.ip_address,
        })
    agreed_ids = {a.terms_id for a in agreements}
    active_required = db.query(Terms).filter(Terms.is_active.is_(True), Terms.is_required.is_(True)).all()
    missing = _rows(db, [t for t in active_required if t.id not in agreed_ids])
    return {"items": items, "missing_required": [{k: r[k] for k in ("id", "type", "version", "title")} for r in missing]}


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


@router.post("/users/bulk")
async def bulk_admin_users(body: BulkRequest, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """회원 일괄 활성화·비활성화 (API-061). 건별 독립 처리"""
    target = {"activate": UserStatus.ACTIVE, "deactivate": UserStatus.DEACTIVATED}.get(body.action)
    if target is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="action 은 activate 또는 deactivate 입니다")

    def one(user_id: int) -> None:
        user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="회원을 찾을 수 없습니다")
        if user.status == UserStatus.DELETED:
            raise ValueError("탈퇴한 회원은 상태를 바꿀 수 없습니다")
        before = {"status": user.status.value if user.status else None}
        user.status = target
        user.deactivated_at = get_kst_now() if target == UserStatus.DEACTIVATED else None
        db.commit()
        audit.record("STATUS_CHANGE", "USER", user_id, before=before, after={"status": target.value}, reason=body.reason, meta={"bulk": True})

    return bulk_run(body.ids, one, audit_as=("STATUS_CHANGE", "USER"))


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_admin_user(
        body: user_svc.AdminUserCreate,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자 회원 등록 (F-031). 일반(LOCAL) 로그인 계정으로 만든다"""
    user = user_svc.create_user(db, body)
    audit.record("CREATE", "USER", user.id, after=user_svc.snapshot(user), reason=body.reason)
    activity.log(user, "SIGNUP", "관리자 등록", meta={"provider": "LOCAL", "by_admin": True})
    return _detail(db, user)


@router.put("/users/{user_id}")
async def update_admin_user(
        user_id: int,
        body: user_svc.AdminUserUpdate,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자 회원 수정 (F-031). 보낸 항목만 바꾼다"""
    user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다")
    before, after = user_svc.update_user(db, user, body)
    action = "STATUS_CHANGE" if set(after) == {"status"} else "UPDATE"
    audit.record(action, "USER", user_id, before=before, after=after, reason=body.reason)
    return _detail(db, user)


@router.delete("/users/{user_id}")
async def delete_admin_user(
        user_id: int,
        reason: str | None = Query(None, max_length=500),
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 사용자 삭제"""
    try:
        user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다")
        before = {"nickname": user.nickname, "status": user.status.value if user.status else None}
        user.original_nickname = user.nickname
        user.nickname = f"탈퇴회원_{int(get_kst_now().timestamp())}"
        user.nickname_locked_until = datetime.now() + timedelta(days=7)
        user.deleted_at = datetime.now()
        user.status = UserStatus.DELETED
        db.commit()
        audit.record("DELETE", "USER", user_id, before=before, after={"status": UserStatus.DELETED.value}, reason=reason)
        return {"message": "사용자가 삭제되었습니다"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 사용자 삭제 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )
