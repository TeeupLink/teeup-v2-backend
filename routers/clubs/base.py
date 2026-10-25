# 클럽 관리 API들
from fastapi import APIRouter, Depends, HTTPException, status, Query, Body
from utils.content_filter import reject_banned_content
from sqlalchemy.orm import Session
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

from database import get_db
from models import User, Club, ClubMembership, ClubRegion
from schemas import (ClubCreate, ClubUpdate, ClubResponse, ClubMembersResponse, ClubMembershipResponse,
                     PaginatedResponse, MessageResponse, ClubRole, ClubStatus, MembershipStatus, MeetingStatus,
                     MeetingType)
from routers.auth import get_current_user, get_current_active_user
from utils.datetime_utils import get_kst_now
from utils.region import validate_gungu_codes

router = APIRouter(prefix="/clubs", tags=["클럽 관리"])


@router.get("/{club_id}/meetings", response_model=PaginatedResponse)
async def get_club_meetings_legacy(
    club_id: str,
    page: int = 1,
    limit: int = 10,
    status_filter: MeetingStatus | None = None,
    meeting_type_filter: MeetingType | None = None,
    search: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """클럽별 모임 목록 조회 (레거시 경로 호환)"""
    from routers.meetings.base import get_club_meetings

    return await get_club_meetings(
        club_id=club_id,
        page=page,
        limit=limit,
        status_filter=status_filter,
        meeting_type_filter=meeting_type_filter,
        search=search,
        db=db,
        current_user=current_user,
    )


@router.post("/register", response_model=ClubResponse, dependencies=[Depends(reject_banned_content)])
async def register_club(
    club_data: ClubCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽 등록 (바로 활성화)"""
    return await create_club(club_data, db, current_user)

@router.post("/", response_model=ClubResponse, dependencies=[Depends(reject_banned_content)])
async def create_club(club_data: ClubCreate,
                      db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_active_user)):
    """클럽 생성 (바로 활성화)"""
    try:
        from utils.display_id_generator import generate_club_display_id

        # display_id 생성
        display_id = generate_club_display_id(db)

        if not club_data.sido_code:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="시도를 선택하셔야 합니다.")

        # 클럽 이름 중복 체크 (삭제되지 않은 클럽만)
        existing_club = db.query(Club).filter(
            Club.name == club_data.name,
            Club.deleted_at.is_(None)
        ).first()
        
        if existing_club:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"이미 사용 중인 클럽 이름입니다: '{club_data.name}'"
            )

        # 시도/군구 유효성 검사 (등록 전)
        unique_gungu_codes = validate_gungu_codes(db, club_data.sido_code, club_data.gungu_codes)

        # 클럽 생성 (바로 활성 상태로 생성) - location은 sido_code/gungu_codes로 대체
        club = Club(display_id=display_id,
                    name=club_data.name,
                    sido_code=club_data.sido_code,
                    type=club_data.type,
                    description=club_data.description,
                    contact_info=club_data.contact_info,
                    representative_name=club_data.representative_name,
                    additional_info=club_data.additional_info,
                    member_count=0,
                    status=ClubStatus.ACTIVE)

        db.add(club)
        db.commit()
        db.refresh(club)

        # 디버깅: current_user 타입 확인
        print(f"current_user 타입: {type(current_user)}")
        print(f"current_user 속성들: {dir(current_user)}")

        # current_user가 딕셔너리인 경우 처리
        if isinstance(current_user, dict):
            user_id = current_user.get('id')
        else:
            user_id = current_user.id

        # 선택한 군구 저장
        for gungu_code in unique_gungu_codes:
            db.add(ClubRegion(club_id=club.id, gungu_code=gungu_code))

        # 클럽 생성자를 리더로 추가
        membership = ClubMembership(club_id=club.id, user_id=user_id, role=ClubRole.LEADER)

        db.add(membership)
        db.commit()

        # 멤버 수 업데이트
        club.member_count = 1
        db.commit()
        db.refresh(club)

        # 디버깅: club 객체 확인
        print(f"클럽 생성 완료 - ID: {club.id}, 타입: {type(club)}")
        print(f"클럽 속성들: {dir(club)}")

        club.gungu_codes = [
            region.gungu_code for region in db.query(ClubRegion).filter(ClubRegion.club_id == club.id).all()
        ]
        return club

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"클럽 생성 에러: {str(e)}")
        print(f"에러 타입: {type(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 내부 오류가 발생했습니다: {str(e)}")


@router.get("/", response_model=dict)
async def get_clubs(page: int = 1,
                    limit: int = 10,
                    status_filter: ClubStatus | None = None,
                    sido_code: str | None = Query(None, description="시도 코드"),
                    gungu_codes: list[str] | None = Query(None, description="군구 코드 목록"),
                    db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_active_user)):
    """클럽 목록 조회"""
    try:
        offset = (page - 1) * limit
        from sqlalchemy import func, or_

        clubs_query = db.query(Club).filter(Club.deleted_at.is_(None))

        # 지역 필터 적용 (gungu_codes가 있으면 sido_code는 무시)
        filtered_gungu_codes = [code for code in (gungu_codes or []) if code]
        if filtered_gungu_codes:
            like_filters = [
                ClubRegion.gungu_code.like(f"{code}%") for code in filtered_gungu_codes
            ]
            matching_club_ids_query = db.query(ClubRegion.club_id).filter(
                or_(*like_filters)
            )
            clubs_query = clubs_query.filter(Club.id.in_(matching_club_ids_query))
        elif sido_code:
            clubs_query = clubs_query.filter(Club.sido_code == sido_code)

        # status_filter가 있으면 해당 상태만 필터링
        if status_filter:
            clubs_query = clubs_query.filter(Club.status == status_filter)
        total = clubs_query.order_by(None).with_entities(func.count(Club.id)).scalar() or 0

        clubs = clubs_query.order_by(Club.created_at.desc()).offset(offset).limit(limit).all()
        total_pages = (total + limit - 1) // limit

        if not clubs:
            return {"data": [], "total": total, "page": page, "limit": limit, "total_pages": total_pages}

        from schemas import MembershipStatus

        club_ids = [club.id for club in clubs]
        membership_rows = db.query(
            ClubMembership.club_id,
            ClubMembership.user_id,
            ClubMembership.status,
            ClubMembership.role,
            User.realname,
            User.nickname,
            User.email,
        ).join(
            User,
            User.id == ClubMembership.user_id,
        ).filter(
            ClubMembership.club_id.in_(club_ids),
        ).all()

        member_count_map: dict[int, int] = {}
        leader_name_map: dict[int, str] = {}
        my_membership_map: dict[int, tuple] = {}
        for club_id, user_id, membership_status, membership_role, realname, nickname, email in membership_rows:
            if membership_status == MembershipStatus.ACTIVE or membership_status == "APPROVED":
                member_count_map[club_id] = member_count_map.get(club_id, 0) + 1
                if membership_role == ClubRole.LEADER and club_id not in leader_name_map:
                    leader_name_map[club_id] = realname or nickname or email

            if user_id == current_user.id and club_id not in my_membership_map:
                my_membership_map[club_id] = (membership_status, membership_role)

        region_rows = db.query(
            ClubRegion.club_id,
            ClubRegion.gungu_code,
        ).filter(
            ClubRegion.club_id.in_(club_ids)
        ).all()
        gungu_codes_map: dict[int, list[str]] = {}
        for club_id, gungu_code in region_rows:
            gungu_codes_map.setdefault(club_id, []).append(gungu_code)

        club_responses = []
        for club in clubs:
            membership_status = None
            membership_role = None
            if club.id in my_membership_map:
                raw_status, raw_role = my_membership_map[club.id]
                membership_status = raw_status.value if hasattr(raw_status, "value") else raw_status
                membership_role = raw_role.value if hasattr(raw_role, "value") else raw_role

            club_responses.append({
                "id": club.id,
                "display_id": club.display_id,
                "name": club.name,
                "sido_code": club.sido_code,
                "gungu_codes": gungu_codes_map.get(club.id, []),
                "type": club.type,
                "description": club.description,
                "member_count": member_count_map.get(club.id, 0),
                "contact_info": club.contact_info,
                "representative_name": leader_name_map.get(club.id, club.representative_name),
                "additional_info": club.additional_info,
                "status": club.status,
                "membership_status": membership_status,
                "membership_role": membership_role,
                "created_at": club.created_at,
                "updated_at": club.updated_at,
            })

        return {"data": club_responses, "total": total, "page": page, "limit": limit, "total_pages": total_pages}

    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.get("/my", response_model=dict)
async def get_my_clubs(page: int = 1,
                       limit: int = 10,
                       status_filter: str | None = None,
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_active_user)):
    """내 클럽 목록 조회"""
    try:
        from models import ClubMembership, Club

        # 페이지네이션 계산
        offset = (page - 1) * limit

        # 사용자가 가입한 클럽 조회
        from sqlalchemy import or_
        from schemas import MembershipStatus

        # status_filter가 PENDING이면 가입 신청 내역 조회 (멤버십 상태 PENDING 포함)
        # 그 외에는 ACTIVE 또는 APPROVED 상태의 멤버십만 조회
        if status_filter == 'PENDING':
            # 가입 신청 내역: 멤버십 상태가 PENDING인 것만 조회
            query = db.query(ClubMembership, Club).join(Club, ClubMembership.club_id == Club.id).filter(
                ClubMembership.user_id == current_user.id, Club.deleted_at.is_(None),
                ClubMembership.status == MembershipStatus.PENDING)
        else:
            # 일반 내 클럽 목록: ACTIVE 또는 APPROVED 상태의 멤버십만
            query = db.query(ClubMembership, Club).join(Club, ClubMembership.club_id == Club.id).filter(
                ClubMembership.user_id == current_user.id,
                Club.deleted_at.is_(None),
                or_(
                    ClubMembership.status == MembershipStatus.ACTIVE,
                    ClubMembership.status == "APPROVED"  # 이전에 APPROVED로 저장된 멤버도 포함
                ))

            # 클럽 상태 필터 적용 (PENDING이 아닐 때만)
            if status_filter:
                from models import ClubStatus
                query = query.filter(Club.status == ClubStatus(status_filter))

        total = query.count()
        results = query.order_by(ClubMembership.created_at.desc()).offset(offset).limit(limit).all()

        clubs = []
        for membership, club in results:
            # 실제 멤버 수 계산 (승인된 멤버만)
            from sqlalchemy import or_
            from schemas import MembershipStatus
            actual_member_count = db.query(ClubMembership).filter(
                ClubMembership.club_id == club.id,
                or_(
                    ClubMembership.status == MembershipStatus.ACTIVE,
                    ClubMembership.status == "APPROVED"  # 이전에 APPROVED로 저장된 멤버도 포함
                )).count()

            # 클럽 리더(개설자) 찾기
            from models import ClubRole
            leader_membership = db.query(ClubMembership).filter(ClubMembership.club_id == club.id,
                                                                ClubMembership.role == ClubRole.LEADER).first()
            created_by = leader_membership.user_id if leader_membership else None

            gungu_codes = [
                region.gungu_code for region in db.query(ClubRegion).filter(ClubRegion.club_id == club.id).all()
            ]
            club_data = {
                "id": club.id,
                "display_id": club.display_id,
                "name": club.name,
                "sido_code": club.sido_code,
                "gungu_codes": gungu_codes,
                "type": club.type.value if club.type else None,
                "description": club.description,
                "member_count": actual_member_count,  # 실제 멤버 수 사용
                "status": club.status.value if club.status else None,
                "my_role": membership.role.value if hasattr(membership.role, "value") else membership.role,
                "membership_role": membership.role.value if hasattr(membership.role, "value") else membership.role,
                "membership_status":
                membership.status.value if hasattr(membership.status, "value") else membership.status,
                "joined_at": membership.created_at,
                "created_at": club.created_at,  # 클럽 개설일 추가
                "profile_image": club.profile_image,
                "created_by": created_by,  # 클럽 리더(개설자) ID 추가
                "settlement_enabled": getattr(club, "settlement_enabled", True),
            }
            clubs.append(club_data)

        total_pages = (total + limit - 1) // limit

        return {"data": clubs, "total": total, "page": page, "limit": limit, "total_pages": total_pages}

    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 오류: {str(e)}")


@router.get("/{club_id}", response_model=ClubResponse)
async def get_club(club_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """클럽 상세 조회 (display_id 또는 id로 조회)"""
    try:
        from models import ClubMembership

        # display_id로 먼저 조회 시도
        club = db.query(Club).filter(Club.display_id == club_id, Club.deleted_at.is_(None)).first()

        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(Club.id == club_id_int, Club.deleted_at.is_(None)).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우 (예: "club-81198" 형식이지만 DB에 없는 경우)
                club = None

        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다.")

        # 현재 사용자의 멤버십 상태 확인
        membership = db.query(ClubMembership).filter(ClubMembership.club_id == club.id,
                                                     ClubMembership.user_id == current_user.id).first()

        # INACTIVE 클럽은 모든 사용자가 조회 가능 (프론트엔드에서 모달로 처리)

        # 멤버십 상태를 club 객체에 추가 (문자열로 변환)
        if membership:
            club.membership_status = membership.status.value if hasattr(membership.status, "value") else str(
                membership.status)
            club.membership_role = membership.role.value if hasattr(membership.role, "value") else str(membership.role)
        else:
            club.membership_status = None
            club.membership_role = None

        # 실제 멤버 수 계산 (승인된 멤버만)
        from sqlalchemy import or_
        from schemas import MembershipStatus
        current_member_count = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            or_(
                ClubMembership.status == MembershipStatus.ACTIVE,
                ClubMembership.status == "APPROVED"  # 이전에 APPROVED로 저장된 멤버도 포함
            )).count()

        # 실제 멤버 수를 club 객체에 추가
        club.current_member_count = current_member_count

        club.gungu_codes = [
            region.gungu_code for region in db.query(ClubRegion).filter(ClubRegion.club_id == club.id).all()
        ]

        # 회비 요약 (비회원 포함 조회용)
        from models import ClubFee
        from models.enums import BillingCycle
        regular_fee = db.query(ClubFee).filter(
            ClubFee.club_id == club.id,
            ClubFee.is_active == True,
            ClubFee.cycle.in_([BillingCycle.MONTHLY, BillingCycle.QUARTERLY, BillingCycle.YEARLY])
        ).first()
        cycle_labels = {"MONTHLY": "월 1회", "QUARTERLY": "분기 1회", "YEARLY": "연 1회"}
        if regular_fee:
            club.fee_summary = {
                "has_regular_fee": True,
                "amount": float(regular_fee.amount) if regular_fee.amount else None,
                "cycle": regular_fee.cycle.value if regular_fee.cycle else None,
                "cycle_label": cycle_labels.get(regular_fee.cycle.value, regular_fee.cycle.value) if regular_fee.cycle else None,
            }
        else:
            club.fee_summary = {"has_regular_fee": False, "amount": None, "cycle": None, "cycle_label": None}

        # 대표자명 계산: 항상 현재 리더의 실명으로 설정
        original_representative_name = club.representative_name
        try:
            from models import ClubRole
            leader_membership = db.query(ClubMembership).filter(
                ClubMembership.club_id == club.id, ClubMembership.role == ClubRole.LEADER,
                or_(ClubMembership.status == MembershipStatus.ACTIVE, ClubMembership.status == "APPROVED")).first()

            if leader_membership:
                leader_user = db.query(User).filter(User.id == leader_membership.user_id).first()
                if leader_user:
                    # 항상 현재 리더의 실명으로 설정
                    new_representative_name = leader_user.realname or leader_user.nickname or leader_user.email
                    club.representative_name = new_representative_name
                    logger.info(
                        f"클럽 {club.id} ({club.name}) 대표자명 업데이트: '{original_representative_name}' -> '{new_representative_name}' (리더: user_id={leader_user.id}, email={leader_user.email})"
                    )
                else:
                    logger.warning(
                        f"클럽 {club.id} ({club.name}) 리더 멤버십은 있지만 사용자를 찾을 수 없음: user_id={leader_membership.user_id}")
            else:
                # 리더를 찾지 못한 경우 기존 값 유지
                logger.warning(f"클럽 {club.id} ({club.name}) 리더를 찾을 수 없음. 기존 대표자명 유지: '{original_representative_name}'")
        except Exception as e:
            logger.error(f"클럽 {club.id} ({club.name}) 리더 조회 중 오류: {str(e)}", exc_info=True)

        # 디버깅 로그
        logger.debug(f"클럽 {club.id} ({club.name}) 상세 조회:")
        logger.debug(f"  - 예상 멤버 수 (member_count): {club.member_count}")
        logger.debug(f"  - 실제 멤버 수 (current_member_count): {current_member_count}")
        logger.debug(f"  - 대표자명 (representative_name): {club.representative_name}")

        return club

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.put("/{club_id}", response_model=ClubResponse, dependencies=[Depends(reject_banned_content)])
async def update_club(club_id: str,
                      club_data: ClubUpdate,
                      db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_active_user)):
    """클럽 정보 수정 (리더/매니저만 가능)"""
    try:
        # display_id로 먼저 조회 시도
        club = db.query(Club).filter(Club.display_id == club_id, Club.deleted_at.is_(None)).first()

        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(Club.id == club_id_int, Club.deleted_at.is_(None)).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우 (예: "club-81198" 형식이지만 DB에 없는 경우)
                club = None

        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다.")

        # 권한 확인 (리더/매니저 또는 관리자)
        # 관리자 확인 (Admin 모델 조회)
        from models import Admin
        admin = db.query(Admin).filter(Admin.id == current_user.id, Admin.deleted_at.is_(None)).first()

        if not admin:
            # 관리자가 아니면 리더/매니저 확인
            membership = db.query(ClubMembership).filter(
                ClubMembership.club_id == club.id,  # 실제 클럽 ID 사용
                ClubMembership.user_id == current_user.id,
                ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER])).first()

            if not membership:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="클럽 리더나 매니저만 클럽 정보를 수정할 수 있습니다.")

        # 클럽 정보 수정
        if club_data.name is not None:
            # 클럽 이름 중복 체크 (현재 클럽 제외, 삭제되지 않은 클럽만)
            existing_club = db.query(Club).filter(
                Club.name == club_data.name,
                Club.id != club.id,
                Club.deleted_at.is_(None)
            ).first()
            
            if existing_club:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"이미 사용 중인 클럽 이름입니다: '{club_data.name}'"
                )
            
            club.name = club_data.name
        if club_data.type is not None:
            club.type = club_data.type
        if club_data.sido_code is not None and not club_data.sido_code:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="시도를 선택하셔야 합니다.")
        if club_data.sido_code is not None and club_data.gungu_codes is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="sido_code 변경 시 gungu_codes는 필수입니다.")
        if club_data.sido_code is not None:
            club.sido_code = club_data.sido_code
        if club_data.description is not None:
            club.description = club_data.description
        if club_data.member_count is not None:
            club.member_count = club_data.member_count
        if club_data.contact_info is not None:
            club.contact_info = club_data.contact_info
        # 대표자명은 개인정보이므로 수정 불가 (제거)
        # if club_data.representative_name is not None:
        #     club.representative_name = club_data.representative_name
        if club_data.additional_info is not None:
            club.additional_info = club_data.additional_info
        if club_data.application_deadline is not None:
            club.application_deadline = club_data.application_deadline
        if club_data.settlement_enabled is not None:
            club.settlement_enabled = bool(club_data.settlement_enabled)

        if club_data.gungu_codes is not None:
            target_sido_code = club_data.sido_code if club_data.sido_code is not None else club.sido_code
            if not target_sido_code:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="시도를 선택하셔야 합니다.")
            unique_gungu_codes = validate_gungu_codes(db, target_sido_code, club_data.gungu_codes)
            db.query(ClubRegion).filter(ClubRegion.club_id == club.id).delete(synchronize_session=False)
            for gungu_code in unique_gungu_codes:
                db.add(ClubRegion(club_id=club.id, gungu_code=gungu_code))

        club.updated_at = get_kst_now()
        db.commit()
        db.refresh(club)

        club.gungu_codes = [
            region.gungu_code for region in db.query(ClubRegion).filter(ClubRegion.club_id == club.id).all()
        ]
        return club

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.get("/{club_id}/active-meetings-check")
async def check_active_meetings(club_id: str,
                                db: Session = Depends(get_db),
                                current_user: User = Depends(get_current_active_user)):
    """활성 모임 체크 (리더/매니저만 가능)"""
    try:
        from models import Meeting, ClubMembership
        from schemas import MeetingStatus, ClubRole
        from sqlalchemy import or_

        # display_id로 먼저 조회 시도
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

        # 권한 확인 (리더/매니저만 가능)
        # NOTE: ClubMembership에는 deleted_at이 없으므로 status로 체크
        membership = db.query(ClubMembership).filter(ClubMembership.club_id == club.id,
                                                     ClubMembership.user_id == current_user.id,
                                                     ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER]),
                                                     ClubMembership.status.in_(["ACTIVE", "APPROVED"])).first()

        if not membership:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="클럽 리더나 매니저만 활성 모임을 확인할 수 있습니다.")

        # 활성 모임 체크 (ROUND 타입만, SCHEDULED 또는 IN_PROGRESS 상태)
        # Meeting.status는 문자열로 저장되므로 문자열 비교
        # NOTE: Meeting 모델에 deleted_at이 없으므로 상태만 체크
        active_meetings = db.query(Meeting).filter(Meeting.club_id == club.id, Meeting.meeting_type == "ROUND",
                                                   or_(Meeting.status == "SCHEDULED",
                                                       Meeting.status == "IN_PROGRESS")).all()

        active_meetings_count = len(active_meetings)

        # 활성 이벤트 체크 (SOCIAL 타입만, SCHEDULED 또는 IN_PROGRESS 상태)
        # 이벤트는 meeting_type이 "SOCIAL"이고, 상태가 SCHEDULED 또는 IN_PROGRESS인 경우
        # NOTE: Meeting 모델에 deleted_at이 없으므로 상태만 체크
        active_events = db.query(Meeting).filter(Meeting.club_id == club.id, Meeting.meeting_type == "SOCIAL",
                                                 or_(Meeting.status == "SCHEDULED",
                                                     Meeting.status == "IN_PROGRESS")).all()

        active_events_count = len(active_events)

        # 삭제 가능 여부 판단
        can_delete = active_meetings_count == 0 and active_events_count == 0

        # 메시지 생성
        if can_delete:
            message = "클럽을 삭제할 수 있습니다."
        else:
            parts = []
            if active_meetings_count > 0:
                parts.append(f"예정된 모임 {active_meetings_count}개")
            if active_events_count > 0:
                parts.append(f"진행 중인 이벤트 {active_events_count}개")
            message = f"{', '.join(parts)}가 있어 삭제할 수 없습니다."

        return {
            "can_delete": can_delete,
            "active_meetings_count": active_meetings_count,
            "active_events_count": active_events_count,
            "message": message
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"활성 모임 체크 에러: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")


@router.delete("/{club_id}", response_model=MessageResponse)
async def delete_club(club_id: str,
                      db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_active_user)):
    """클럽 삭제 (리더만 가능)"""
    try:
        from models import Meeting, ClubMembership
        from schemas import MeetingStatus, ClubRole, MembershipStatus
        from sqlalchemy import or_
        from datetime import datetime
        from utils.datetime_utils import get_kst_now

        # display_id로 먼저 조회 시도
        club = db.query(Club).filter(Club.display_id == club_id, Club.deleted_at.is_(None)).first()

        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(Club.id == club_id_int, Club.deleted_at.is_(None)).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우 (예: "club-81198" 형식이지만 DB에 없는 경우)
                club = None

        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다.")

        # 권한 확인 (리더만 가능)
        # NOTE: ClubMembership에는 deleted_at이 없으므로 status로 체크
        membership = db.query(ClubMembership).filter(ClubMembership.club_id == club.id,
                                                     ClubMembership.user_id == current_user.id,
                                                     ClubMembership.role == ClubRole.LEADER,
                                                     ClubMembership.status.in_(["ACTIVE", "APPROVED"])).first()

        if not membership:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="클럽 리더만 클럽을 삭제할 수 있습니다.")

        # 활성 모임 체크 (ROUND 타입만)
        # Meeting.status는 문자열로 저장되므로 문자열 비교
        # NOTE: Meeting 모델에 deleted_at이 없으므로 상태만 체크
        active_meetings = db.query(Meeting).filter(Meeting.club_id == club.id, Meeting.meeting_type == "ROUND",
                                                   or_(Meeting.status == "SCHEDULED",
                                                       Meeting.status == "IN_PROGRESS")).count()

        if active_meetings > 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"활성 모임이 {active_meetings}개 있어 삭제할 수 없습니다. 모든 모임을 종료한 후 삭제해주세요.")

        # 활성 이벤트 체크 (SOCIAL 타입만)
        # NOTE: Meeting 모델에 deleted_at이 없으므로 상태만 체크
        active_events = db.query(Meeting).filter(Meeting.club_id == club.id, Meeting.meeting_type == "SOCIAL",
                                                 or_(Meeting.status == "SCHEDULED",
                                                     Meeting.status == "IN_PROGRESS")).count()

        if active_events > 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"진행 중인 이벤트가 {active_events}개 있어 삭제할 수 없습니다.")

        # 멤버 수 체크 (리더만 남아있어야 함)
        # NOTE: ClubMembership에는 deleted_at이 없으므로 status로만 체크
        active_members = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            or_(ClubMembership.status == MembershipStatus.ACTIVE, ClubMembership.status == "APPROVED")).count()

        if active_members > 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"승인된 멤버가 {active_members-1}명 있습니다. 모든 멤버를 제거한 후 클럽을 삭제해주세요.")

        # 소프트 삭제 수행
        club.deleted_at = get_kst_now()
        club.updated_at = get_kst_now()

        # 관련 멤버십도 소프트 삭제 (status를 INACTIVE로 변경)
        # NOTE: ClubMembership에는 deleted_at이 없으므로 status로 처리
        memberships = db.query(ClubMembership).filter(ClubMembership.club_id == club.id,
                                                      ClubMembership.status.in_(["ACTIVE", "APPROVED"])).all()

        for m in memberships:
            m.status = MembershipStatus.INACTIVE
            m.updated_at = get_kst_now()

        db.commit()

        return {"success": True, "message": f"클럽 '{club.name}'이 성공적으로 삭제되었습니다."}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"클럽 삭제 에러: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="서버 내부 오류가 발생했습니다.")
