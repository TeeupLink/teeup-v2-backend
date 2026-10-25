# 클럽 멤버 관리 API들
from fastapi import APIRouter, Depends, HTTPException, status, Query, Body
from utils.content_filter import reject_banned_content
from sqlalchemy.orm import Session
from sqlalchemy import or_
import logging

logger = logging.getLogger(__name__)

from database import get_db
from models import User, Club, ClubMembership, Meeting, UserScoreHistory
from schemas import (
    MessageResponse, ClubRole, ClubStatus, MembershipStatus,
    ClubMemberAddRequest, ClubMemberRoleUpdateRequest,
    MemberNoteUpdate, MemberNoteResponse,
    ClubMemberSearchResponse, ClubMemberRecordSummaryResponse, ClubMemberRoundingHistoryItem,
    ClubMembershipResponse,
)
from routers.auth import get_current_active_user
from utils.datetime_utils import get_kst_now

router = APIRouter(prefix="/clubs", tags=["클럽 멤버 관리"])

ACTIVE_MEMBERSHIP_STATUSES = [MembershipStatus.ACTIVE, "APPROVED"]


def _is_active_membership(membership: ClubMembership | None) -> bool:
    if not membership or not membership.status:
        return False
    status_value = membership.status.value if hasattr(membership.status, "value") else str(membership.status)
    return status_value in {"ACTIVE", "APPROVED"}


def _raise_profile_not_completed(message: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "PROFILE_NOT_COMPLETED",
            "message": message,
            "redirect": "/mypage/edit",
        },
    )


def _resolve_club_by_id_or_display_id(db: Session, club_id: str) -> Club:
    """club_id(display_id or numeric id)를 Club 레코드로 해석"""
    club = db.query(Club).filter(
        Club.display_id == club_id,
        Club.deleted_at.is_(None),
    ).first()

    if club:
        return club

    try:
        club_id_int = int(club_id)
    except ValueError:
        club_id_int = None

    if club_id_int is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="클럽을 찾을 수 없습니다.",
        )

    club = db.query(Club).filter(
        Club.id == club_id_int,
        Club.deleted_at.is_(None),
    ).first()

    if not club:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="클럽을 찾을 수 없습니다.",
        )

    return club


@router.get("/{club_id}/membership", response_model=ClubMembershipResponse)
async def get_club_membership(
    club_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """현재 사용자의 클럽 멤버십 조회 (display_id 또는 id 지원)"""
    club = _resolve_club_by_id_or_display_id(db, club_id)

    membership = db.query(ClubMembership).filter(
        ClubMembership.club_id == club.id,
        ClubMembership.user_id == current_user.id,
    ).first()

    if not membership:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 클럽의 멤버가 아닙니다.",
        )

    role_value = membership.role.value if hasattr(membership.role, "value") else str(membership.role)
    status_value = membership.status.value if hasattr(membership.status, "value") else str(membership.status)

    return ClubMembershipResponse(
        id=membership.id,
        club_id=club.id,
        user_id=current_user.id,
        role=role_value,
        status=status_value,
        joined_at=membership.created_at,
    )


@router.get("/{club_id}/members/{user_id}/record-summary", response_model=ClubMemberRecordSummaryResponse)
async def get_club_member_record_summary(
    club_id: str,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """클럽 구성원이 보는 특정 멤버의 기록 요약 (가입자만 접근 가능).

    스코어·통계·내역은 모두 **클럽에 소속된 라운딩 모임**만 포함합니다.
    다른 클럽에서 친 라운딩은 제외되며, 프라이빗 라운딩도 제외됩니다.
    """
    try:
        club = _resolve_club_by_id_or_display_id(db, club_id)

        # 요청자 권한: 해당 클럽 ACTIVE/APPROVED 멤버여야 함
        viewer_membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == current_user.id,
        ).first()
        if not _is_active_membership(viewer_membership):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 구성원만 멤버 기록을 조회할 수 있습니다.",
            )

        # 대상자도 해당 클럽 멤버인지 확인 (ACTIVE/APPROVED 기준)
        target_membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == user_id,
            ClubMembership.status.in_(ACTIVE_MEMBERSHIP_STATUSES),
        ).first()
        if not target_membership:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="해당 사용자는 클럽 구성원이 아닙니다.",
            )

        target_user = db.query(User).filter(
            User.id == user_id,
            User.deleted_at.is_(None),
        ).first()
        if not target_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="사용자를 찾을 수 없습니다.",
            )

        # 클럽(club.id) 소속 ROUND만 집계 — 타 클럽 모임·프라이빗 라운딩 제외
        from sqlalchemy import func
        from models.enums import MeetingType

        non_private_round_filter = (
            UserScoreHistory.user_id == user_id,
            Meeting.club_id == club.id,
            Meeting.meeting_type == MeetingType.ROUND,
            Meeting.is_private.is_(False),
        )

        score_q = (
            db.query(UserScoreHistory)
            .join(Meeting, Meeting.id == UserScoreHistory.meeting_id)
            .filter(*non_private_round_filter)
        )

        recent_rounds_count = int(score_q.count())
        avg_score_val = (
            db.query(func.avg(UserScoreHistory.gross_score))
            .join(Meeting, Meeting.id == UserScoreHistory.meeting_id)
            .filter(*non_private_round_filter)
            .scalar()
        )

        history_rows = (
            db.query(UserScoreHistory, Meeting)
            .join(Meeting, Meeting.id == UserScoreHistory.meeting_id)
            .filter(*non_private_round_filter)
            .order_by(UserScoreHistory.played_at.desc())
            .limit(50)
            .all()
        )

        rounding_history: list[ClubMemberRoundingHistoryItem] = []
        for ush, mtg in history_rows:
            net_val = float(ush.net_score) if ush.net_score is not None else None
            hc_val = float(ush.handicap_used) if ush.handicap_used is not None else None
            course = (mtg.course_name or mtg.venue_name or mtg.location or None)
            if isinstance(course, str) and not course.strip():
                course = None
            rounding_history.append(
                ClubMemberRoundingHistoryItem(
                    meeting_id=mtg.id,
                    meeting_name=mtg.name,
                    meeting_time=mtg.meeting_time,
                    course_name=course,
                    gross_score=ush.gross_score,
                    net_score=net_val,
                    handicap_used=hc_val,
                    played_at=ush.played_at,
                )
            )

        handicap_source = (
            float(target_user.handicap)
            if target_user.handicap is not None
            else (float(target_user.handicap_init) if target_user.handicap_init is not None else None)
        )

        average_score = float(avg_score_val) if avg_score_val is not None else None
        if average_score is not None:
            average_score = round(average_score, 1)

        return ClubMemberRecordSummaryResponse(
            user_id=user_id,
            club_id=club.id,
            handicap=handicap_source,
            average_score=average_score,
            recent_rounds_count=recent_rounds_count,
            rounding_history=rounding_history,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"멤버 기록 요약 조회 실패: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다.",
        )


@router.get("/members/search", response_model=ClubMemberSearchResponse)
async def search_members_in_my_clubs(
    name: str | None = Query(None, description="이름 검색어 (실명/닉네임 부분 일치, 비우면 전체)"),
    limit: int = Query(100, ge=1, le=500, description="최대 반환 인원 수"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """내가 속한 클럽의 구성원 중 이름 검색"""
    try:
        keyword = name.strip() if name else ""

        # 현재 사용자가 속한 클럽(승인된 멤버십) 조회
        my_memberships = db.query(ClubMembership.club_id).filter(
            ClubMembership.user_id == current_user.id,
            ClubMembership.status.in_([MembershipStatus.ACTIVE, "APPROVED"])
        ).all()
        my_club_ids = [club_id for (club_id,) in my_memberships]

        if not my_club_ids:
            response_payload = {
                "keyword": keyword,
                "data": [],
                "total_clubs": 0,
                "total_members": 0
            }
            print(f"[DEBUG] /clubs/members/search response: {response_payload}")
            return response_payload

        query = (
            db.query(
                Club.id.label("club_id"),
                Club.display_id.label("club_display_id"),
                Club.name.label("club_name"),
                User.id.label("user_id"),
                User.realname.label("realname"),
                User.nickname.label("nickname"),
                User.gender.label("gender"),
                User.handicap.label("handicap"),
                User.handicap_init.label("handicap_init"),
            )
            .join(ClubMembership, ClubMembership.club_id == Club.id)
            .join(User, User.id == ClubMembership.user_id)
            .filter(
                Club.id.in_(my_club_ids),
                Club.deleted_at.is_(None),
                ClubMembership.status.in_([MembershipStatus.ACTIVE, "APPROVED"]),
                User.deleted_at.is_(None),
                ~User.email.like("%@guest.local"),
                ~User.nickname.like("guest_%"),
            )
        )

        if keyword:
            search_term = f"%{keyword}%"
            # name 파라미터로 realname/nickname 모두 부분 검색
            query = query.filter(or_(User.realname.ilike(search_term), User.nickname.ilike(search_term)))

        rows = query.order_by(Club.name.asc(), User.realname.asc(), User.nickname.asc()).limit(limit).all()

        club_map = {}
        for row in rows:
            if row.club_id not in club_map:
                club_map[row.club_id] = {
                    "club_id": row.club_id,
                    "club_display_id": row.club_display_id,
                    "club_name": row.club_name,
                    "members": []
                }

            gender_value = row.gender.value if hasattr(row.gender, "value") else row.gender
            handicap_source = row.handicap if row.handicap is not None else row.handicap_init
            handicap_value = float(handicap_source) if handicap_source is not None else None

            club_map[row.club_id]["members"].append({
                "id": row.user_id,
                "name": row.realname or row.nickname or "Unknown",
                "gender": gender_value,
                "handicap": handicap_value,
            })

        data = list(club_map.values())
        total_members = sum(len(club["members"]) for club in data)

        response_payload = {
            "keyword": keyword,
            "data": data,
            "total_clubs": len(data),
            "total_members": total_members
        }
        print(f"[DEBUG] /clubs/members/search response: {response_payload}")
        return response_payload

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"클럽 구성원 이름 검색 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.get("/{club_id}/members", response_model=dict)
async def get_club_members(
    club_id: str,
    page: int = 1,
    limit: int = 10,
    all_members: bool = Query(False, description="모든 멤버 조회 (대기중 포함)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽 멤버 목록 조회 (display_id 또는 id로 조회, 페이지네이션 지원)
    
    all_members=False: 승인된 회원만 표시 (기본값, 일반 멤버 목록용)
    all_members=True: 모든 멤버 표시 (멤버 관리 페이지용)
    """
    try:
        # display_id로 먼저 클럽 조회 시도
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()
        
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우 (예: "club-81198" 형식이지만 DB에 없는 경우)
                club = None
        
        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )

        # 조회 권한 확인
        current_membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == current_user.id
        ).first()

        is_active_member = _is_active_membership(current_membership)
        current_role = None
        if current_membership and current_membership.role:
            current_role = (
                current_membership.role.value
                if hasattr(current_membership.role, "value")
                else str(current_membership.role)
            )
        is_manager_or_leader = is_active_member and current_role in {"LEADER", "MANAGER"}

        if all_members and not is_manager_or_leader:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="리더 또는 매니저만 멤버 관리 목록을 조회할 수 있습니다."
            )

        if not all_members and not is_active_member:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 구성원만 회원 목록을 조회할 수 있습니다."
            )

        # 페이지네이션 계산
        offset = (page - 1) * limit

        # 멤버 목록 조회 쿼리 (실제 club.id 사용)
        if all_members:
            # 멤버 관리 페이지: 승인 멤버 + 가입 신청자
            memberships_query = db.query(ClubMembership).filter(
                ClubMembership.club_id == club.id,
                ClubMembership.status.in_([
                    MembershipStatus.ACTIVE,
                    MembershipStatus.PENDING,
                    "APPROVED",
                ])
            )
        else:
            # 승인된 회원만 표시 (일반 멤버 목록용)
            memberships_query = db.query(ClubMembership).filter(
                ClubMembership.club_id == club.id,
                ClubMembership.status.in_(ACTIVE_MEMBERSHIP_STATUSES)
            )
        
        # 전체 개수 조회
        total = memberships_query.count()
        
        # 페이지네이션 적용
        # all_members=True일 때는 모든 멤버를 반환 (페이지네이션 제한 없음)
        if all_members:
            memberships = memberships_query.order_by(ClubMembership.created_at.desc()).all()
        else:
            memberships = memberships_query.order_by(ClubMembership.created_at.desc()).offset(offset).limit(limit).all()
        
        # 디버깅: 리더 정보 로깅
        leader_count = 0
        for m in memberships:
            role_val = m.role.value if hasattr(m.role, 'value') else str(m.role)
            if role_val == "LEADER":
                leader_count += 1
        print(f"[DEBUG] 클럽 {club.id} ({club.display_id}) 멤버 조회:")
        print(f"  - 전체 멤버 수: {total}")
        print(f"  - 반환된 멤버 수: {len(memberships)}")
        print(f"  - 리더 수: {leader_count}")
        print(f"  - all_members: {all_members}")
        
        # 사용자 정보와 함께 구성
        member_list = []
        for membership in memberships:
            user = db.query(User).filter(
                User.id == membership.user_id,
                User.deleted_at.is_(None)
            ).first()
            
            if not user:
                continue  # 삭제된 사용자는 제외
            
            # 게스트는 클럽 회원 목록에서 제외 (게스트는 일회성이므로)
            if user.email and user.email.endswith('@guest.local'):
                continue
            if user.nickname and user.nickname.startswith('guest_'):
                continue
            
            # status 처리 (Enum일 수 있음)
            status_value = membership.status
            if hasattr(status_value, 'value'):
                status_value = status_value.value
            elif not status_value:
                status_value = "ACTIVE"
            else:
                status_value = str(status_value)
            
            # role 처리 (Enum일 수 있음)
            role_value = membership.role
            if hasattr(role_value, 'value'):
                role_value = role_value.value
            else:
                role_value = str(role_value)
            
            # 리더는 항상 ACTIVE 상태로 강제 설정
            if role_value == "LEADER":
                status_value = "ACTIVE"
            
            member_data = {
                "id": membership.id,
                "user_id": membership.user_id,
                "user_name": user.realname or user.nickname or "Unknown",  # 실명 우선
                "user_realname": user.realname if user.realname else None,
                "user_nickname": user.nickname if user.nickname else None,
                "user_email": user.email if user.email else None,
                "user_phone_number": user.phone_number if user.phone_number else None,
                "user_birthdate": user.birthdate.isoformat() if user.birthdate else None,
                "user_gender": user.gender if user.gender else None,
                "user_handicap": user.handicap if user.handicap is not None else None,
                "user_average_score": user.average_score if user.average_score is not None else None,
                "role": role_value,
                "status": status_value,
                "joined_at": membership.created_at
            }
            
            # 리더 정보 디버깅 로그
            if role_value == "LEADER":
                print(f"[DEBUG] 리더 발견 - User ID: {user.id}, Email: {user.email}, Role: {role_value}, Status: {status_value} (원본: {membership.status})")
            
            # 디버그 로그
            if user.nickname == "사용자1" or (user.realname and "동동" in user.realname):
                print(f"[DEBUG] 멤버 데이터: {member_data}")
                print(f"[DEBUG] 사용자 정보 - realname: {user.realname}, phone: {user.phone_number}, birthdate: {user.birthdate}, gender: {user.gender}, handicap: {user.handicap}, avg_score: {user.average_score}")
            
            member_list.append(member_data)
        
        # all_members=True일 때는 페이지네이션 정보를 전체 기준으로 계산
        if all_members:
            total_pages = 1
        else:
            total_pages = (total + limit - 1) // limit
        
        # 디버깅: 최종 응답 로깅
        leaders_in_response = [m for m in member_list if m.get("role") == "LEADER"]
        print(f"[DEBUG] API 응답 - 리더 수: {len(leaders_in_response)}, 전체 멤버 수: {len(member_list)}")
        if leaders_in_response:
            for leader in leaders_in_response:
                print(f"[DEBUG]   - 리더: {leader.get('user_name')} (ID: {leader.get('user_id')}, Role: {leader.get('role')}, Status: {leader.get('status')})")
        
        return {
            "data": member_list,
            "total": total,
            "page": page,
            "limit": limit if not all_members else total,
            "total_pages": total_pages
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"클럽 멤버 조회 에러: {str(e)}")
        print(f"에러 타입: {type(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )

@router.post("/{club_id}/join", response_model=MessageResponse)
async def join_club(
    club_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽 가입"""
    try:
        # 프로필 완성도 검증: 실명, 전화번호, 생년월일, 성별 필수
        if not current_user.realname:
            _raise_profile_not_completed("클럽 가입을 위해서는 실명이 필요합니다. 프로필을 먼저 완성해주세요.")
        
        if not current_user.phone_number:
            _raise_profile_not_completed("클럽 가입을 위해서는 전화번호가 필요합니다. 프로필을 먼저 완성해주세요.")
        
        if not current_user.birthdate:
            _raise_profile_not_completed("클럽 가입을 위해서는 생년월일이 필요합니다. 프로필을 먼저 완성해주세요.")
        
        if not current_user.gender:
            _raise_profile_not_completed("클럽 가입을 위해서는 성별 정보가 필요합니다. 프로필을 먼저 완성해주세요.")
        
        # 클럽 조회 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None),
            Club.status == ClubStatus.ACTIVE
        ).first()
        
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None),
                    Club.status == ClubStatus.ACTIVE
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None
        
        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="승인된 클럽을 찾을 수 없습니다."
            )
        
        # 이미 가입했는지 확인
        existing_membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == current_user.id
        ).first()
        
        if existing_membership:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="이미 가입된 클럽입니다."
            )
        
        # 클럽 가입 신청 (PENDING 상태로 생성)
        membership = ClubMembership(
            club_id=club.id,
            user_id=current_user.id,
            role=ClubRole.MEMBER,
            status=MembershipStatus.PENDING
        )
        
        db.add(membership)
        
        # 멤버 수는 증가시키지 않음 (승인 후에만 증가)
        db.commit()
        
        # 클럽 리더/매니저에게 새 회원 가입신청 알림 전송
        try:
            from utils.notification_service import create_club_membership_request_notification
            
            # 클럽의 리더/매니저 조회
            leaders_managers = db.query(ClubMembership).filter(
                ClubMembership.club_id == club.id,
                ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER]),
                ClubMembership.status.in_(ACTIVE_MEMBERSHIP_STATUSES)
            ).all()
            
            if leaders_managers:
                leader_manager_ids = [lm.user_id for lm in leaders_managers]
                create_club_membership_request_notification(
                    db=db,
                    leader_manager_ids=leader_manager_ids,
                    club_name=club.name,
                    applicant_name=current_user.nickname or current_user.realname or "사용자",
                    club_id=club.id
                )
        except Exception as e:
            logger.error(f"클럽 가입 신청 알림 전송 실패: {str(e)}")
        
        return {
            "message": "클럽 가입 신청이 완료되었습니다. 승인 대기 중입니다.",
            "success": True
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.post("/{club_id}/members", response_model=MessageResponse)
async def add_club_member(
    club_id: str,
    member_data: ClubMemberAddRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽 멤버 추가 (리더/매니저만 가능)"""
    try:
        # 클럽 조회 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()
        
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None
        
        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )
        
        # 권한 확인 (리더/매니저만 가능)
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == current_user.id
        ).first()
        
        if not membership or membership.role not in [ClubRole.LEADER, ClubRole.MANAGER]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="리더 또는 매니저만 멤버를 추가할 수 있습니다."
            )
        
        # 대상 사용자 조회
        target_user = db.query(User).filter(User.id == member_data.user_id).first()
        if not target_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="사용자를 찾을 수 없습니다."
            )
        
        # 이미 멤버인지 확인
        existing_membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == member_data.user_id
        ).first()
        
        if existing_membership:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="이미 클럽 멤버입니다."
            )
        
        # 멤버 추가
        new_membership = ClubMembership(
            club_id=club.id,
            user_id=member_data.user_id,
            role=member_data.role
        )
        
        db.add(new_membership)
        
        # 클럽 멤버 수 증가
        club.member_count = (club.member_count or 0) + 1
        db.commit()
        
        return {"message": "멤버가 성공적으로 추가되었습니다.", "success": True}
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"멤버 추가 에러: {str(e)}")
        print(f"에러 타입: {type(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )

@router.post("/{club_id}/members/{user_id}/approve", response_model=MessageResponse)
async def approve_membership(
    club_id: str,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽 가입 승인 (리더/매니저만 가능)"""
    try:
        # 클럽 조회 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()
        
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None
        
        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )
        
        # 실제 클럽 ID 사용
        actual_club_id = club.id
        
        # 권한 확인 (리더/매니저만 가능)
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == actual_club_id,
            ClubMembership.user_id == current_user.id,
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER]),
            ClubMembership.status.in_(ACTIVE_MEMBERSHIP_STATUSES)
        ).first()

        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="리더 또는 매니저만 가입을 승인할 수 있습니다."
            )
        
        # 가입 신청 조회
        pending_membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == actual_club_id,
            ClubMembership.user_id == user_id,
            ClubMembership.status == MembershipStatus.PENDING
        ).first()
        
        if not pending_membership:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="가입 신청을 찾을 수 없습니다."
            )
        
        # 가입 승인
        pending_membership.status = MembershipStatus.ACTIVE
        pending_membership.updated_at = get_kst_now()
        
        # 클럽 멤버 수 증가 (PENDING -> ACTIVE로 변경된 경우)
        if club.member_count is None:
            club.member_count = 0
        club.member_count += 1
        club.updated_at = get_kst_now()
        
        db.commit()
        
        # 클럽 가입 승인 알림 전송 (커밋 후 별도 처리, 실패해도 메인 트랜잭션에 영향 없음)
        try:
            from utils.notification_service import create_club_membership_notification
            # 별도 세션으로 알림 생성 (메인 트랜잭션과 분리)
            from database import SessionLocal
            notification_db = SessionLocal()
            try:
                create_club_membership_notification(
                    db=notification_db,
                    user_id=user_id,
                    club_name=club.name,
                    status="ACTIVE",
                    club_id=actual_club_id
                )
                notification_db.commit()
            except Exception as e:
                notification_db.rollback()
                logger.error(f"클럽 가입 승인 알림 전송 실패: {str(e)}")
            finally:
                notification_db.close()
        except Exception as e:
            logger.error(f"클럽 가입 승인 알림 전송 중 오류: {str(e)}")
        
        return {
            "message": "멤버십이 승인되었습니다.",
            "success": True
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )

@router.post("/{club_id}/members/{user_id}/reject", response_model=MessageResponse)
async def reject_membership(
    club_id: str,
    user_id: int,
    reject_data: dict | None = Body(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽 가입 거절 (리더/매니저만 가능)"""
    try:
        # 클럽 조회 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()
        
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None
        
        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )
        
        # 실제 클럽 ID 사용
        actual_club_id = club.id
        
        # 권한 확인 (리더/매니저만 가능)
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == actual_club_id,
            ClubMembership.user_id == current_user.id
        ).first()
        
        if not membership or membership.role not in [ClubRole.LEADER, ClubRole.MANAGER]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="리더 또는 매니저만 가입을 거절할 수 있습니다."
            )
        
        # 가입 신청 조회
        pending_membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == actual_club_id,
            ClubMembership.user_id == user_id,
            ClubMembership.status == MembershipStatus.PENDING
        ).first()
        
        if not pending_membership:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="가입 신청을 찾을 수 없습니다."
            )
        
        # 가입 거절 (멤버십 삭제)
        reason = reject_data.get("reason", "") if reject_data else ""
        
        db.delete(pending_membership)
        
        club.updated_at = get_kst_now()
        db.commit()
        
        # 클럽 가입 거절 알림 전송
        try:
            from utils.notification_service import create_club_membership_notification
            create_club_membership_notification(
                db=db,
                user_id=user_id,
                club_name=club.name,
                status="REJECTED",
                rejection_reason=reason,
                club_id=actual_club_id
            )
        except Exception as e:
            logger.error(f"클럽 가입 거절 알림 전송 실패: {str(e)}")
        
        message = "멤버십이 거절되었습니다."
        if reason:
            message += f" 사유: {reason}"
        
        return {
            "message": message,
            "success": True
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )

@router.put("/{club_id}/members/{user_id}/role", response_model=MessageResponse)
async def update_member_role(
    club_id: str,
    user_id: int,
    role_data: ClubMemberRoleUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """멤버 역할 변경 (리더/매니저만 가능)"""
    try:
        # 클럽 조회 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()
        
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None
        
        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )
        
        # 실제 클럽 ID 사용
        actual_club_id = club.id
        
        # 클럽 멤버 권한 확인 (리더/매니저만)
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == actual_club_id,
            ClubMembership.user_id == current_user.id,
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER])
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 리더나 매니저만 멤버 역할을 변경할 수 있습니다."
            )
        
        # 대상 멤버 조회
        target_membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == actual_club_id,
            ClubMembership.user_id == user_id
        ).first()
        
        if not target_membership:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="해당 사용자가 클럽 멤버가 아닙니다."
            )
        
        # 역할 변경
        target_membership.role = role_data.role
        target_membership.updated_at = get_kst_now()
        db.commit()
        
        return {
            "message": f"멤버 역할이 {role_data.role}로 변경되었습니다.",
            "success": True
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )


@router.delete("/{club_id}/members/{user_id}", response_model=MessageResponse)
async def remove_club_member(
    club_id: str,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽 멤버 내보내기 (리더/매니저가 일반 회원을 강제로 쫒아내기)"""
    try:
        # 클럽 조회 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()
        
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None
        
        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )
        
        # 실제 클럽 ID 사용
        actual_club_id = club.id
        
        # 현재 사용자의 클럽 멤버십 확인
        current_membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == actual_club_id,
            ClubMembership.user_id == current_user.id
        ).first()
        
        if not current_membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="해당 클럽의 멤버가 아닙니다."
            )
        
        # 권한 확인 (리더 또는 매니저만 가능)
        if current_membership.role not in [ClubRole.LEADER, ClubRole.MANAGER]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 리더나 매니저만 멤버를 내보낼 수 있습니다."
            )
        
        # 대상 사용자의 멤버십 확인
        target_membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == actual_club_id,
            ClubMembership.user_id == user_id
        ).first()
        
        if not target_membership:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="해당 사용자가 클럽 멤버가 아닙니다."
            )
        
        # 자기 자신을 내보내려는 경우 방지
        if user_id == current_user.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="자기 자신을 내보낼 수 없습니다. 클럽을 탈퇴하려면 다른 방법을 사용하세요."
            )
        
        # 매니저는 리더를 내보낼 수 없음
        if (current_membership.role == ClubRole.MANAGER and 
            target_membership.role == ClubRole.LEADER):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="매니저는 리더를 내보낼 수 없습니다."
            )
        
        # 매니저는 다른 매니저를 내보낼 수 없음
        if (current_membership.role == ClubRole.MANAGER and 
            target_membership.role == ClubRole.MANAGER):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="매니저는 다른 매니저를 내보낼 수 없습니다."
            )
        
        # 일반 회원만 내보낼 수 있음
        if target_membership.role != ClubRole.MEMBER:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="일반 회원만 내보낼 수 있습니다."
            )
        
        # 멤버십 삭제
        db.delete(target_membership)
        
        # 클럽 멤버 수 감소
        club.member_count = max(0, club.member_count - 1)
        db.add(club)  # 클럽 객체를 세션에 다시 추가
        
        db.commit()
        
        return {"message": "멤버가 성공적으로 내보내졌습니다.", "success": True}
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.delete("/{club_id}/membership", response_model=MessageResponse)
async def cancel_membership(
    club_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽 가입 신청 취소 (PENDING 상태만)"""
    try:
        # 클럽 조회 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()
        
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None
        
        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )
        
        # PENDING 상태의 멤버십 조회
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == current_user.id,
            ClubMembership.status == MembershipStatus.PENDING
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="가입 신청 내역을 찾을 수 없습니다. 이미 처리되었거나 존재하지 않습니다."
            )
        
        # 멤버십 삭제
        db.delete(membership)
        db.commit()
        
        return {
            "message": "가입 신청이 취소되었습니다.",
            "success": True
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.delete("/{club_id}/leave", response_model=MessageResponse)
async def leave_club(
    club_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽 탈퇴 (일반 회원만 가능, 매니저/리더는 탈퇴 불가)"""
    try:
        # 클럽 조회 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()
        
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None
        
        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )
        
        # 현재 사용자의 클럽 멤버십 확인 (실제 club.id 사용)
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == current_user.id
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="해당 클럽의 멤버가 아닙니다."
            )
        
        # 매니저/리더는 탈퇴 불가
        if membership.role in [ClubRole.MANAGER, ClubRole.LEADER]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="매니저나 리더는 직접 탈퇴할 수 없습니다. 리더십을 양도한 후 탈퇴하세요."
            )
        
        # 멤버십 삭제
        db.delete(membership)
        
        # 클럽 멤버 수 감소
        club.member_count = max(0, club.member_count - 1)
        db.add(club)  # 클럽 객체를 세션에 다시 추가
        
        db.commit()
        
        return {"message": "클럽에서 성공적으로 탈퇴했습니다.", "success": True}
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"클럽 탈퇴 에러: {str(e)}")
        print(f"에러 타입: {type(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )

@router.get("/{club_id}/members/{user_id}/note", response_model=MemberNoteResponse)
async def get_member_note(
    club_id: str,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """멤버 메모 조회 (리더/매니저만 가능)"""
    try:
        # 클럽 조회 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()
        
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None
        
        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )
        
        # 실제 클럽 ID 사용
        actual_club_id = club.id
        
        # 권한 확인 (리더/매니저만 가능)
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == actual_club_id,
            ClubMembership.user_id == current_user.id
        ).first()
        
        if not membership or membership.role not in [ClubRole.LEADER, ClubRole.MANAGER]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="리더 또는 매니저만 메모를 조회할 수 있습니다."
            )
        
        # 대상 멤버십 조회
        target_membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == actual_club_id,
            ClubMembership.user_id == user_id
        ).first()
        
        if not target_membership:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="멤버를 찾을 수 없습니다."
            )
        
        return {
            "note": target_membership.note or None,
            "updated_at": target_membership.updated_at
        }
        
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )

@router.put("/{club_id}/members/{user_id}/note", response_model=MessageResponse, dependencies=[Depends(reject_banned_content)])
async def update_member_note(
    club_id: str,
    user_id: int,
    note_data: MemberNoteUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """멤버 메모 작성/수정 (리더/매니저만 가능)"""
    try:
        # 클럽 조회 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()
        
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None
        
        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )
        
        # 실제 클럽 ID 사용
        actual_club_id = club.id
        
        # 권한 확인 (리더/매니저만 가능)
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == actual_club_id,
            ClubMembership.user_id == current_user.id
        ).first()
        
        if not membership or membership.role not in [ClubRole.LEADER, ClubRole.MANAGER]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="리더 또는 매니저만 메모를 작성할 수 있습니다."
            )
        
        # 대상 멤버십 조회
        target_membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == actual_club_id,
            ClubMembership.user_id == user_id
        ).first()
        
        if not target_membership:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="멤버를 찾을 수 없습니다."
            )
        
        # 메모 업데이트
        target_membership.note = note_data.note
        target_membership.updated_at = get_kst_now()
        
        db.commit()
        
        return {
            "message": "메모가 저장되었습니다.",
            "success": True
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )
