"""
사용자 관리 API 라우터
"""

from fastapi import APIRouter, Depends, HTTPException, status, Request
from utils.content_filter import reject_banned_content
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import logging

from database import get_db
from models import (
    User,
    UserStatus,
    Provider,
    MeetingResult,
    Meeting,
    MeetingParticipant,
    UserScoreHistory,
    Admin,
)
from schemas import (
    UserCreate,
    UserUpdate,
    UserResponse,
    MessageResponse,
    NotificationResponse,
    NotificationSettingsResponse,
    NotificationSettingsUpdate,
    PaymentResponse,
    SubscriptionResponse,
    PaginatedResponse,
    MeetingType,
)
from pydantic import BaseModel, Field, validator
from routers.auth import get_current_user, get_current_active_user

from utils.permissions import PermissionChecker, require_user_or_admin

# 로깅 설정
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


# 핸디캡 관련 스키마
class HandicapUpdate(BaseModel):
    average_score: Optional[int] = Field(None, ge=55, le=144, description="평균 타수 (자동 계산용)")
    initial_handicap: Optional[float] = Field(None, ge=0, le=72, description="초기 핸디캡 (직접 입력, 하위 호환성)")

    @validator("*", pre=True, always=True)
    def check_at_least_one(cls, v, values):
        """average_score 또는 initial_handicap 중 하나는 필수 (API 레벨에서 검증)"""
        # validator는 각 필드마다 호출되므로, 여기서는 단순히 값 반환
        # 실제 검증은 API 레벨에서 수행
        return v


class HandicapResponse(BaseModel):
    user_id: int
    nickname: str
    handicap: Optional[float]  # 하위 호환성 (기존 필드)
    average_score: Optional[int]
    initial_handicap: Optional[float]  # 가입 시 수동 입력한 핸디캡
    calculated_handicap: Optional[float]  # 자동 계산된 핸디캡 (최근 N경기 평균 기반)
    handicap_update_method: Optional[str]  # MANUAL 또는 AUTO
    handicap_calculation_count: Optional[int]  # 핸디캡 계산에 사용된 경기 수
    is_auto_calculated: (
        bool  # 자동 계산 여부 (deprecated, calculated_handicap이 있으면 True)
    )


# 사용자 생성은 /api/v1/admin/users POST 에서만 가능 (admin/users.py)


@router.put("/me", response_model=UserResponse, dependencies=[Depends(reject_banned_content)])
async def update_my_profile(
        user_data: UserUpdate,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_active_user),
):
    """내 프로필 수정"""
    try:
        logger.info(f"내 프로필 수정 시작 - user_id: {current_user.id}")

        # 사용자 조회
        user = (db.query(User).filter(User.id == current_user.id, User.deleted_at.is_(None)).first())
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="사용자를 찾을 수 없습니다",
            )

        # ---------------------------
        # 1. 입력값 유효성 검사
        # ---------------------------
        # ---------------------------
        # 1. 입력값 유효성 검사
        # ---------------------------
        try:
            # 실명 검사
            if user_data.realname is not None:
                name = user_data.realname.strip()
                if len(name) < 2:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="실명은 2자 이상이어야 합니다",
                    )

                import re
                if re.search(r"[\uAC00-\uD7A3]", name):
                    if not re.fullmatch(r"[\uAC00-\uD7A3]+", name):
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="한글 이름은 공백 없이 한글만 입력해주세요",
                        )
                else:
                    if not re.fullmatch(r"[A-Za-z ]+", name):
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="영문 이름은 알파벳과 공백만 입력해주세요",
                        )

            # 핸디캡 init (0~72)
            if user_data.handicap_init is not None:
                if not (0 <= user_data.handicap_init <= 72):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="핸디캡은 0-72 사이여야 합니다",
                    )

            # 평균 타수 init (55~144)
            if user_data.average_score_init is not None:
                if not (55 <= user_data.average_score_init <= 144):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="평균 스코어는 55-144 사이여야 합니다",
                    )

            # 생년월일
            if user_data.birthdate is not None:
                from routers.auth import validate_birthdate

                birthdate_validation = validate_birthdate(user_data.birthdate)
                if not birthdate_validation["is_valid"]:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="; ".join(birthdate_validation["errors"]),
                    )

                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="; ".join(birthdate_validation["errors"]),
                    )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"유효성 검사 실패: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="입력 값이 올바르지 않습니다",
            )

        # ---------------------------
        # 2. 중복 검사
        # ---------------------------
        if user_data.email and user_data.email != user.email:
            if db.query(User).filter(
                    User.email == user_data.email,
                    User.id != user.id,
            ).first():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="이미 존재하는 이메일입니다",
                )

        if user_data.nickname and user_data.nickname != user.nickname:
            if db.query(User).filter(
                    User.nickname == user_data.nickname,
                    User.id != user.id,
            ).first():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="이미 존재하는 닉네임입니다",
                )

        if user_data.phone_number and user_data.phone_number != user.phone_number:
            if db.query(User).filter(
                    User.phone_number == user_data.phone_number,
                    User.id != user.id,
            ).first():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="이미 사용 중인 전화번호입니다",
                )

        # ---------------------------
        # 3. 업데이트 데이터 구성 (PATCH semantics)
        # ---------------------------
        update_data = user_data.dict(exclude_unset=True)

        # average_score는 외부 입력 무시
        update_data.pop("average_score", None)

        # average_score 확정 시 init 값 무시
        if user.average_score is not None:
            update_data.pop("average_score_init", None)
            update_data.pop("handicap_init", None)

        # ---------------------------
        # 4. 필드별 반영
        # ---------------------------
        from models import Gender, HandicapUpdateMethod
        from utils.handicap_calculator import (
            calculate_handicap_from_average_score, )
        from datetime import datetime

        for field, value in update_data.items():
            if field == "gender":
                # 성별은 1회만 설정 가능
                if user.gender is None and value:
                    setattr(user, field, Gender(value))

            elif field == "birthdate" and value:
                try:
                    setattr(
                        user,
                        field,
                        datetime.strptime(value, "%Y-%m-%d"),
                    )
                except ValueError:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="생년월일 형식이 올바르지 않습니다. YYYY-MM-DD",
                    )

            elif field in ("average_score_init", "handicap_init"):
                # init 값은 아래에서 일괄 처리
                continue

            else:
                setattr(user, field, value)

        # ---------------------------
        # 5. 초기 평균 타수 → 초기 핸디캡 계산 (단 한 곳)
        # ---------------------------
        if (user.average_score is None and "average_score_init" in update_data
                and update_data["average_score_init"] is not None):
            calculated_handicap = calculate_handicap_from_average_score(update_data["average_score_init"])
            user.average_score_init = update_data["average_score_init"]
            user.handicap_init = calculated_handicap
            user.handicap_update_method = HandicapUpdateMethod.MANUAL

        db.commit()
        db.refresh(user)

        # ---------------------------
        # 6. 응답 DTO
        # ---------------------------
        user_response = UserResponse(
            id=user.id,
            email=user.email,
            realname=user.realname,
            nickname=user.nickname,
            phone_number=user.phone_number,
            birthdate=user.birthdate,
            gender=user.gender.value if user.gender else None,
            handicap=user.handicap,
            handicap_init=user.handicap_init,
            average_score=user.average_score,
            average_score_init=user.average_score_init,
            status=user.status.value if user.status else None,
            provider=user.provider.value if user.provider else None,
            email_verified=user.email_verified,
            needs_terms_agreement=user.needs_terms_agreement,
            terms_agreement=user.terms_agreement,
            privacy_policy=user.privacy_policy,
            privacy_collection=user.privacy_collection,
            marketing_consent=user.marketing_consent,
            club_count=user.club_count if hasattr(user, "club_count") else 0,
            deactivated_at=user.deactivated_at,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )

        logger.info(f"내 프로필 수정 완료 - user_id: {current_user.id}")
        return user_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"내 프로필 수정 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 오류가 발생했습니다",
        )


# 사용자 수정/삭제는 /api/v1/admin/users PUT,DELETE 에서만 가능
@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
        user_id: int,
        user_data: UserUpdate,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_current_user),
):
    """사용자 정보 수정"""
    try:
        logger.info(f"사용자 수정 시작 - user_id: {user_id}")

        # 사용자 조회
        user = (db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first())
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="사용자를 찾을 수 없습니다",
            )

        # 관리자 권한 확인
        if not PermissionChecker.check_admin_permission(current_user, db):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="관리자 권한이 필요합니다")

        # 유효성 검사: 실명/핸디캡/평균 스코어 (관리자 수정에도 동일 정책 적용)
        try:
            if user_data.realname is not None:
                name = user_data.realname.strip()
                if len(name) < 2:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="실명은 2자 이상이어야 합니다",
                    )
                import re

                if re.search(r"[\uAC00-\uD7A3]", name):
                    if not re.fullmatch(r"[\uAC00-\uD7A3]+", name):
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="한글 이름은 공백 없이 한글만 입력해주세요",
                        )
                else:
                    if not re.fullmatch(r"[A-Za-z ]+", name):
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="영문 이름은 알파벳과 공백만 입력해주세요",
                        )

            if user_data.handicap is not None:
                if user_data.handicap < 0 or user_data.handicap > 72:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="핸디캡은 0-72 사이여야 합니다",
                    )

            if user_data.average_score is not None:
                if user_data.average_score < 55 or user_data.average_score > 144:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="평균 스코어는 55-144 사이여야 합니다",
                    )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"유효성 검사 실패(update_user): {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="입력 값이 올바르지 않습니다",
            )

        # 이메일 중복 확인 (다른 사용자가 사용 중인지)
        if user_data.email and user_data.email != user.email:
            existing_user = (db.query(User).filter(User.email == user_data.email, User.id != user_id).first())
            if existing_user:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="이미 존재하는 이메일입니다",
                )

        if user_data.nickname and user_data.nickname != user.nickname:
            existing_nickname = (db.query(User).filter(User.nickname == user_data.nickname, User.id != user_id).first())
            if existing_nickname:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="이미 존재하는 닉네임입니다",
                )

        if user_data.phone_number and user_data.phone_number != user.phone_number:
            existing_phone = (db.query(User).filter(User.phone_number == user_data.phone_number, User.id
                                                    != user_id).first())
            if existing_phone:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="이미 사용 중인 전화번호입니다",
                )

        # ---------------------------
        # 3. 업데이트 데이터 구성 (PATCH semantics)
        # ---------------------------
        update_data = user_data.dict(exclude_unset=True)

        # average_score는 외부 입력 무시
        update_data.pop("average_score", None)

        # average_score 확정 시 init 값 무시
        if user.average_score is not None:
            update_data.pop("average_score_init", None)
            update_data.pop("handicap_init", None)

        # ---------------------------
        # 4. 필드별 반영
        # ---------------------------
        from models import Gender, HandicapUpdateMethod
        from utils.handicap_calculator import (
            calculate_handicap_from_average_score, )
        from datetime import datetime

        for field, value in update_data.items():
            if field == "gender":
                # 성별은 1회만 설정 가능
                if user.gender is None and value:
                    setattr(user, field, Gender(value))

            elif field == "birthdate" and value:
                # birthdate 문자열을 datetime으로 변환
                try:
                    from datetime import datetime

                    birthdate_datetime = datetime.strptime(value, "%Y-%m-%d")
                    setattr(user, field, birthdate_datetime)
                except ValueError:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="생년월일 형식이 올바르지 않습니다. YYYY-MM-DD 형식으로 입력해주세요.",
                    )
            else:
                setattr(user, field, value)

        # ---------------------------
        # 5. 초기 평균 타수 → 초기 핸디캡 계산 (단 한 곳)
        # ---------------------------
        if (user.average_score is None and "average_score_init" in update_data
                and update_data["average_score_init"] is not None):
            calculated_handicap = calculate_handicap_from_average_score(update_data["average_score_init"])
            user.average_score_init = update_data["average_score_init"]
            user.handicap_init = calculated_handicap
            user.handicap_update_method = HandicapUpdateMethod.MANUAL

        db.commit()
        db.refresh(user)

        # ---------------------------
        # 6. 응답 DTO
        # ---------------------------
        user_response = UserResponse(
            id=user.id,
            email=user.email,
            realname=user.realname,
            nickname=user.nickname,
            phone_number=user.phone_number,
            birthdate=user.birthdate,
            gender=user.gender.value if user.gender else None,
            handicap=user.handicap,
            handicap_init=user.handicap_init,
            average_score=user.average_score,
            status=user.status.value if user.status else None,
            provider=user.provider.value if user.provider else None,
            email_verified=user.email_verified,
            needs_terms_agreement=user.needs_terms_agreement,
            terms_agreement=user.terms_agreement,
            privacy_policy=user.privacy_policy,
            privacy_collection=user.privacy_collection,
            marketing_consent=user.marketing_consent,
            club_count=user.club_count if hasattr(user, "club_count") else 0,
            deactivated_at=user.deactivated_at,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )

        logger.info(f"사용자 수정 완료 - user_id: {user_id}")
        return user_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"사용자 수정 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )


# 사용자 수정/삭제는 /api/v1/admin/users PUT,DELETE 에서만 가능


# 마이페이지 관련 API들
@router.get("/profile", response_model=UserResponse)
async def get_my_profile(db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """내 프로필 조회"""
    try:
        logger.info(f"내 프로필 조회 시작 - user_id: {current_user.id}")

        user = (db.query(User).filter(User.id == current_user.id, User.deleted_at.is_(None)).first())
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="사용자를 찾을 수 없습니다",
            )
        user_response = UserResponse(
            id=user.id,
            email=user.email,
            realname=user.realname,
            nickname=user.nickname,
            phone_number=user.phone_number,
            birthdate=user.birthdate,
            gender=user.gender.value if user.gender else None,
            handicap=user.handicap,
            handicap_init=user.handicap_init,
            average_score=user.average_score,
            average_score_init=user.average_score_init,
            status=user.status.value if user.status else None,
            provider=user.provider.value if user.provider else None,
            email_verified=user.email_verified,
            needs_terms_agreement=user.needs_terms_agreement,
            terms_agreement=user.terms_agreement,
            privacy_policy=user.privacy_policy,
            privacy_collection=user.privacy_collection,
            marketing_consent=user.marketing_consent,
            club_count=user.club_count if hasattr(user, "club_count") else 0,
            deactivated_at=user.deactivated_at,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )

        logger.info(f"내 프로필 조회 완료 - user_id: {current_user.id}")
        return user_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"내 프로필 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )


@router.get("/my-clubs", response_model=List[dict])
async def get_my_clubs(db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """내 클럽 목록 조회"""
    try:
        logger.info(f"내 클럽 목록 조회 시작 - user_id: {current_user.id}")

        from models import ClubMembership, Club, ClubRegion

        # 사용자가 가입한 클럽 조회
        memberships = (db.query(ClubMembership, Club).join(Club, ClubMembership.club_id == Club.id).filter(
            ClubMembership.user_id == current_user.id, Club.deleted_at.is_(None)).all())

        clubs = []
        for membership, club in memberships:
            gungu_codes = [
                region.gungu_code for region in db.query(ClubRegion).filter(ClubRegion.club_id == club.id).all()
            ]
            club_data = {
                "id": club.id,
                "name": club.name,
                "sido_code": club.sido_code,
                "gungu_codes": gungu_codes,
                "type": club.type.value if club.type else None,
                "description": club.description,
                "member_count": club.member_count,
                "location": club.location,
                "status": club.status.value if club.status else None,
                "my_role": membership.role.value if membership.role else None,
                "joined_at": membership.created_at,
                "profile_image": club.profile_image,
                "settlement_enabled": getattr(club, "settlement_enabled", True),
            }
            clubs.append(club_data)

        logger.info(f"내 클럽 목록 조회 완료 - 총 {len(clubs)}개")
        return clubs

    except Exception as e:
        logger.error(f"내 클럽 목록 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )


@router.get("/my-meetings", response_model=PaginatedResponse)
async def get_my_meetings(
        page: int = 1,
        limit: int = 10,
        status_filter: Optional[str] = None,
        meeting_type_filter: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_active_user),
):
    """내 모임 목록 조회"""
    try:
        logger.info(
            f"내 모임 목록 조회 시작 - user_id: {current_user.id}, page: {page}, limit: {limit}, status_filter: {status_filter}, meeting_type_filter: {meeting_type_filter}, start_date: {start_date}, end_date: {end_date}"
        )

        from models import MeetingParticipant, Meeting, Club
        from datetime import datetime

        # 페이지네이션 계산
        offset = (page - 1) * limit

        # 사용자가 참가한 모임 조회 (삭제되지 않은 모임만)
        query = (db.query(MeetingParticipant, Meeting,
                          Club).join(Meeting, MeetingParticipant.meeting_id == Meeting.id).join(
                              Club, Meeting.club_id == Club.id).filter(MeetingParticipant.user_id == current_user.id))

        # 상태 필터 적용
        if status_filter:
            from models import MeetingStatus

            query = query.filter(Meeting.status == MeetingStatus(status_filter))

        # 모임 타입 필터 적용
        if meeting_type_filter:
            query = query.filter(Meeting.meeting_type == meeting_type_filter)

        # 날짜 필터 적용
        if start_date:
            try:
                start_datetime = datetime.strptime(start_date, "%Y-%m-%d")
                query = query.filter(Meeting.meeting_time >= start_datetime)
            except ValueError:
                logger.warning(f"잘못된 시작 날짜 형식: {start_date}")

        if end_date:
            try:
                # 종료일은 하루 끝까지 포함하도록 설정
                end_datetime = datetime.strptime(end_date, "%Y-%m-%d")
                end_datetime = end_datetime.replace(hour=23, minute=59, second=59, microsecond=999999)
                query = query.filter(Meeting.meeting_time <= end_datetime)
            except ValueError:
                logger.warning(f"잘못된 종료 날짜 형식: {end_date}")

        total = query.count()
        logger.info(f"내 모임 목록 조회 - 총 {total}개 모임 발견")
        results = (query.order_by(Meeting.meeting_time.desc()).offset(offset).limit(limit).all())
        logger.info(f"내 모임 목록 조회 - 페이지네이션 후 {len(results)}개 모임 반환")

        meetings = []
        for participant, meeting, club in results:
            # 참가자 수 조회
            participant_count = (db.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == meeting.id
            ).count())

            # 팀 정보 조회
            from models import Team

            teams = db.query(Team).filter(Team.meeting_id == meeting.id).all()
            teams_data = ([{"id": team.id, "name": team.name} for team in teams] if teams else [])

            meeting_data = {
                "id": meeting.id,
                "name": meeting.name,
                "description": meeting.description,
                "location": meeting.location,
                "meeting_time": meeting.meeting_time,
                "max_participants": meeting.max_participants,
                "meeting_type": meeting.meeting_type,
                "status": str(meeting.status) if meeting.status else None,
                "club_id": club.id,
                "club_name": club.name,
                "created_at": meeting.created_at,
                # getStatusBadge 함수에 필요한 필드들
                "participant_count": participant_count,
                "application_deadline": meeting.application_deadline,
                "application_closed_early": meeting.application_closed_early or False,
                "settlement_confirmed": meeting.settlement_confirmed or False,
                "rounding_completed_at": meeting.rounding_completed_at,
                "is_completed": meeting.is_completed or False,
                "teams": teams_data,
            }
            meetings.append(meeting_data)

        # PaginatedResponse 형식으로 반환
        total_pages = (total + limit - 1) // limit if limit > 0 else 1

        logger.info(f"내 모임 목록 조회 완료 - 총 {total}개, 페이지 {page}/{total_pages}")
        return {
            "data": meetings,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
        }

    except Exception as e:
        logger.error(f"내 모임 목록 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )


@router.delete("/account", response_model=MessageResponse)
async def delete_my_account(db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """회원 탈퇴"""
    try:
        logger.info(f"회원 탈퇴 시작 - user_id: {current_user.id}")

        # 사용자 조회
        user = (db.query(User).filter(User.id == current_user.id, User.deleted_at.is_(None)).first())
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="사용자를 찾을 수 없습니다",
            )

        # 소프트 삭제 (deleted_at 설정)
        from datetime import datetime

        user.deleted_at = datetime.now()
        user.status = UserStatus.DELETED

        db.commit()

        logger.info(f"회원 탈퇴 완료 - user_id: {current_user.id}")
        return MessageResponse(message="회원 탈퇴가 완료되었습니다", success=True)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"회원 탈퇴 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )


@router.get("/notifications", response_model=List[NotificationResponse])
async def get_my_notifications(
        page: int = 1,
        limit: int = 20,
        status_filter: Optional[str] = None,
        type_filter: Optional[str] = None,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_active_user),
):
    """내 알림 목록 조회"""
    try:
        logger.info(f"내 알림 목록 조회 시작 - user_id: {current_user.id}")

        from models import Notification
        from schemas import NotificationType, NotificationStatus

        # 페이지네이션 계산
        offset = (page - 1) * limit

        # 알림 조회 쿼리
        query = db.query(Notification).filter(Notification.user_id == current_user.id)

        if status_filter:
            # DB에 저장된 값은 문자열이므로 Enum.value로 비교
            status_value = (NotificationStatus(status_filter).value if hasattr(
                NotificationStatus(status_filter), "value") else str(NotificationStatus(status_filter)))
            query = query.filter(Notification.status == status_value)

        if type_filter:
            # DB에 저장된 값은 문자열이므로 Enum.value로 비교
            type_value = (NotificationType(type_filter).value
                          if hasattr(NotificationType(type_filter), "value") else str(NotificationType(type_filter)))
            query = query.filter(Notification.type == type_value)

        total = query.count()
        notifications = (query.order_by(Notification.created_at.desc()).offset(offset).limit(limit).all())

        notification_responses = []
        for notification in notifications:
            # type과 status는 String으로 저장되므로 .value 불필요
            type_value = (notification.type.value if hasattr(notification.type, "value") else
                          str(notification.type) if notification.type else None)
            status_value = (notification.status.value if hasattr(notification.status, "value") else
                            str(notification.status) if notification.status else None)

            notification_response = NotificationResponse(
                id=notification.id,
                user_id=notification.user_id,
                type=type_value,
                title=notification.title,
                content=notification.content,
                status=status_value,
                read_at=notification.read_at,
                created_at=notification.created_at,
            )
            notification_responses.append(notification_response)

        logger.info(f"내 알림 목록 조회 완료 - 총 {len(notification_responses)}개")
        return notification_responses

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"내 알림 목록 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )


@router.put("/notifications/{notification_id}/read", response_model=MessageResponse)
async def mark_notification_as_read(
        notification_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_active_user),
):
    """알림을 읽음으로 표시"""
    try:
        logger.info(f"알림 읽음 처리 시작 - notification_id: {notification_id}")

        from models import Notification
        from schemas import NotificationStatus
        from datetime import datetime

        # 알림 조회
        notification = (db.query(Notification).filter(
            Notification.id == notification_id,
            Notification.user_id == current_user.id,
        ).first())

        if not notification:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="알림을 찾을 수 없습니다")

        # 읽음 처리
        notification.status = NotificationStatus.READ.value
        notification.read_at = datetime.now()

        db.commit()

        logger.info(f"알림 읽음 처리 완료 - notification_id: {notification_id}")
        return MessageResponse(message="알림을 읽음으로 표시했습니다", success=True)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"알림 읽음 처리 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )


@router.delete("/notifications/{notification_id}", response_model=MessageResponse)
async def delete_notification(
        notification_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_active_user),
):
    """알림 삭제"""
    try:
        logger.info(f"알림 삭제 시작 - notification_id: {notification_id}")

        from models import Notification

        # 알림 조회
        notification = (db.query(Notification).filter(
            Notification.id == notification_id,
            Notification.user_id == current_user.id,
        ).first())

        if not notification:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="알림을 찾을 수 없습니다")

        # 알림 삭제
        db.delete(notification)
        db.commit()

        logger.info(f"알림 삭제 완료 - notification_id: {notification_id}")
        return MessageResponse(message="알림이 삭제되었습니다", success=True)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"알림 삭제 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )


@router.put("/notifications/read-all", response_model=MessageResponse)
async def mark_all_notifications_as_read(db: Session = Depends(get_db),
                                         current_user: User = Depends(get_current_active_user)):
    """모든 알림을 읽음으로 표시"""
    try:
        logger.info(f"모든 알림 읽음 처리 시작 - user_id: {current_user.id}")

        from models import Notification
        from schemas import NotificationStatus
        from datetime import datetime

        # 사용자의 모든 읽지 않은 알림 조회
        notifications = (db.query(Notification).filter(
            Notification.user_id == current_user.id,
            Notification.status == NotificationStatus.UNREAD.value,
        ).all())

        # 모든 알림을 읽음으로 처리
        for notification in notifications:
            notification.status = NotificationStatus.READ.value
            notification.read_at = datetime.now()

        db.commit()

        logger.info(f"모든 알림 읽음 처리 완료 - 총 {len(notifications)}개")
        return MessageResponse(
            message=f"{len(notifications)}개의 알림을 읽음으로 표시했습니다",
            success=True,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"모든 알림 읽음 처리 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )


@router.get("/notification-settings", response_model=NotificationSettingsResponse)
async def get_notification_settings(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """알림 설정 조회"""
    try:
        logger.info(f"알림 설정 조회 시작 - user_id: {current_user.get('id')}")

        from models import NotificationSettings

        # 알림 설정 조회
        settings = (db.query(NotificationSettings).filter(
            NotificationSettings.user_id == current_user.get("id")).first())

        # 설정이 없으면 기본값으로 생성
        if not settings:
            import secrets
            import string

            chars = string.ascii_lowercase + string.digits
            settings_id = "".join(secrets.choice(chars) for _ in range(20))

            settings = NotificationSettings(id=settings_id, user_id=current_user.get("id"))
            db.add(settings)
            db.commit()
            db.refresh(settings)

        settings_response = NotificationSettingsResponse(
            id=settings.id,
            user_id=settings.user_id,
            meeting_reminder=settings.meeting_reminder,
            payment_notifications=settings.payment_notifications,
            club_invitations=settings.club_invitations,
            meeting_cancellations=settings.meeting_cancellations,
            new_notices=settings.new_notices,
            system_notifications=settings.system_notifications,
            push_enabled=settings.push_enabled,
            email_enabled=settings.email_enabled,
            sms_enabled=settings.sms_enabled,
            quiet_hours_start=settings.quiet_hours_start,
            quiet_hours_end=settings.quiet_hours_end,
            created_at=settings.created_at,
            updated_at=settings.updated_at,
        )

        logger.info(f"알림 설정 조회 완료 - user_id: {current_user.get('id')}")
        return settings_response

    except Exception as e:
        logger.error(f"알림 설정 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )


@router.put("/notification-settings", response_model=NotificationSettingsResponse)
async def update_notification_settings(
        settings_data: NotificationSettingsUpdate,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_current_user),
):
    """알림 설정 수정"""
    try:
        logger.info(f"알림 설정 수정 시작 - user_id: {current_user.get('id')}")

        from models import NotificationSettings

        # 알림 설정 조회
        settings = (db.query(NotificationSettings).filter(
            NotificationSettings.user_id == current_user.get("id")).first())

        # 설정이 없으면 생성
        if not settings:
            import secrets
            import string

            chars = string.ascii_lowercase + string.digits
            settings_id = "".join(secrets.choice(chars) for _ in range(20))

            settings = NotificationSettings(id=settings_id, user_id=current_user.get("id"))
            db.add(settings)

        # 설정 업데이트
        update_data = settings_data.dict(exclude_unset=True)
        for field, value in update_data.items():
            setattr(settings, field, value)

        db.commit()
        db.refresh(settings)

        settings_response = NotificationSettingsResponse(
            id=settings.id,
            user_id=settings.user_id,
            meeting_reminder=settings.meeting_reminder,
            payment_notifications=settings.payment_notifications,
            club_invitations=settings.club_invitations,
            meeting_cancellations=settings.meeting_cancellations,
            new_notices=settings.new_notices,
            system_notifications=settings.system_notifications,
            push_enabled=settings.push_enabled,
            email_enabled=settings.email_enabled,
            sms_enabled=settings.sms_enabled,
            quiet_hours_start=settings.quiet_hours_start,
            quiet_hours_end=settings.quiet_hours_end,
            created_at=settings.created_at,
            updated_at=settings.updated_at,
        )

        logger.info(f"알림 설정 수정 완료 - user_id: {current_user.get('id')}")
        return settings_response

    except Exception as e:
        logger.error(f"알림 설정 수정 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )


@router.get("/payments", response_model=List[PaymentResponse])
async def get_my_payments(
        page: int = 1,
        limit: int = 20,
        status_filter: Optional[str] = None,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_active_user),
):
    """내 결제 내역 조회"""
    try:
        logger.info(f"내 결제 내역 조회 시작 - user_id: {current_user.id}")

        from models import Payment, PaymentStatus

        # 페이지네이션 계산
        offset = (page - 1) * limit

        # 결제 내역 조회 쿼리
        query = db.query(Payment).filter(Payment.user_id == current_user.id)

        if status_filter:
            query = query.filter(Payment.status == PaymentStatus(status_filter))

        total = query.count()
        payments = (query.order_by(Payment.created_at.desc()).offset(offset).limit(limit).all())

        payment_responses = []
        for payment in payments:
            payment_response = PaymentResponse(
                id=payment.id,
                user_id=payment.user_id,
                subscription_id=payment.subscription_id,
                amount=float(payment.amount) if payment.amount else 0.0,
                currency=payment.currency,
                status=payment.status.value if payment.status else None,
                payment_method=(payment.payment_method.value if payment.payment_method else None),
                payment_key=payment.payment_key,
                order_id=payment.order_id,
                order_name=payment.order_name,
                transaction_id=payment.transaction_id,
                paid_at=payment.paid_at,
                canceled_at=payment.canceled_at,
                cancel_reason=payment.cancel_reason,
                description=payment.description,
                created_at=payment.created_at,
                updated_at=payment.updated_at,
            )
            payment_responses.append(payment_response)

        logger.info(f"내 결제 내역 조회 완료 - 총 {len(payment_responses)}개")
        return payment_responses

    except Exception as e:
        logger.error(f"내 결제 내역 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )


@router.get("/subscriptions", response_model=List[SubscriptionResponse])
async def get_my_subscriptions(db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """내 구독/요금제 이용내역 조회"""
    try:
        logger.info(f"내 구독 내역 조회 시작 - user_id: {current_user.id}")

        from models import Subscription, Plan

        # 구독 내역 조회
        subscriptions = (db.query(Subscription, Plan).join(Plan, Subscription.plan_id == Plan.id).filter(
            Subscription.user_id == current_user.id).order_by(Subscription.created_at.desc()).all())

        subscription_responses = []
        for subscription, plan in subscriptions:
            subscription_response = SubscriptionResponse(
                id=subscription.id,
                user_id=subscription.user_id,
                plan_id=subscription.plan_id,
                plan_name=plan.name,
                status=subscription.status.value if subscription.status else None,
                start_date=subscription.start_date,
                end_date=subscription.end_date,
                next_billing_date=subscription.next_billing_date,
                canceled_at=subscription.canceled_at,
                cancel_reason=subscription.cancel_reason,
                created_at=subscription.created_at,
                updated_at=subscription.updated_at,
            )
            subscription_responses.append(subscription_response)

        logger.info(f"내 구독 내역 조회 완료 - 총 {len(subscription_responses)}개")
        return subscription_responses

    except Exception as e:
        logger.error(f"내 구독 내역 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )


@router.get("/payment-methods", response_model=List[dict])
async def get_my_payment_methods(db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """내 결제 수단 목록 조회"""
    try:
        logger.info(f"내 결제 수단 조회 시작 - user_id: {current_user.id}")

        # 현재는 간단한 구현으로, 실제로는 별도의 결제 수단 테이블이 필요할 수 있습니다
        # 여기서는 최근 결제에서 사용된 결제 수단들을 반환합니다

        from models import Payment, PaymentMethod, PaymentStatus

        # 최근 결제에서 사용된 결제 수단들 조회
        recent_payments = (db.query(Payment).filter(
            Payment.user_id == current_user.id,
            Payment.status == PaymentStatus.SUCCEEDED,
        ).order_by(Payment.created_at.desc()).limit(10).all())

        payment_methods = []
        seen_methods = set()

        for payment in recent_payments:
            if (payment.payment_method and payment.payment_method.value not in seen_methods):
                method_data = {
                    "id": f"method_{payment.id}",
                    "type": payment.payment_method.value,
                    "last_used": payment.paid_at or payment.created_at,
                    "is_default": len(payment_methods) == 0,  # 첫 번째를 기본으로 설정
                    "masked_info": _mask_payment_info(payment.payment_method.value, payment.payment_key),
                }
                payment_methods.append(method_data)
                seen_methods.add(payment.payment_method.value)

        logger.info(f"내 결제 수단 조회 완료 - 총 {len(payment_methods)}개")
        return payment_methods

    except Exception as e:
        logger.error(f"내 결제 수단 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )


def _mask_payment_info(payment_method: str, payment_key: str = None) -> str:
    """결제 수단 정보 마스킹"""
    if payment_method == "CARD":
        return "****-****-****-1234"
    elif payment_method == "BANK_TRANSFER":
        return "****-****-****-5678"
    elif payment_method == "VIRTUAL_ACCOUNT":
        return "****-****-****-9012"
    else:
        return "****-****-****-****"


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_current_user),
):
    """사용자 상세 조회"""
    try:
        logger.info(f"사용자 상세 조회 시작 - user_id: {user_id}")

        user = (db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first())
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="사용자를 찾을 수 없습니다",
            )

        user_response = UserResponse(
            id=user.id,
            email=user.email,
            realname=user.realname,
            nickname=user.nickname,
            phone_number=user.phone_number,
            birthdate=user.birthdate,
            gender=user.gender.value if user.gender else None,
            handicap=user.handicap,
            handicap_init=user.handicap_init,
            average_score=user.average_score,
            average_score_init=user.average_score_init,
            status=user.status.value if user.status else None,
            provider=user.provider.value if user.provider else None,
            email_verified=user.email_verified,
            needs_terms_agreement=user.needs_terms_agreement,
            terms_agreement=user.terms_agreement,
            privacy_policy=user.privacy_policy,
            privacy_collection=user.privacy_collection,
            marketing_consent=user.marketing_consent,
            club_count=user.club_count if hasattr(user, "club_count") else 0,
            deactivated_at=user.deactivated_at,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )

        logger.info(f"사용자 상세 조회 완료 - user_id: {user_id}")
        return user_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"사용자 상세 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}",
        )


# 핸디캡 관련 API
@router.get("/{user_id}/handicap", response_model=HandicapResponse)
async def get_user_handicap(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_current_user),
):
    """사용자 핸디캡 조회"""
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="사용자를 찾을 수 없습니다.",
            )

        # 새로운 핸디캡 시스템 필드 사용
        initial_handicap = (float(user.handicap_init) if user.handicap_init else None)
        calculated_handicap = (float(user.handicap) if user.handicap else None)
        handicap_update_method = (user.handicap_update_method.value if user.handicap_update_method else None)
        handicap_calculation_count = (user.handicap_calculation_count if user.handicap_calculation_count else 0)

        # 하위 호환성을 위한 계산
        # calculated_handicap이 있으면 자동 계산된 것으로 간주
        is_auto_calculated = calculated_handicap is not None

        return HandicapResponse(
            user_id=user.id,
            nickname=user.nickname,
            handicap=float(user.handicap) if user.handicap else None,  # 하위 호환성
            average_score=user.average_score,
            initial_handicap=initial_handicap,
            calculated_handicap=calculated_handicap,
            handicap_update_method=handicap_update_method,
            handicap_calculation_count=handicap_calculation_count,
            is_auto_calculated=is_auto_calculated,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"핸디캡 조회 실패: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="핸디캡 정보를 조회하는 중 오류가 발생했습니다.",
        )


@router.put("/{user_id}/handicap", response_model=MessageResponse)
async def update_user_handicap(
        user_id: int,
        handicap_data: HandicapUpdate,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user),
):
    """사용자 핸디캡 수정 (본인 또는 관리자만 가능)"""
    logger.info(
        f"핸디캡 수정 요청 시작 - user_id: {user_id}, current_user_id: {current_user.id}, average_score: {handicap_data.average_score}, initial_handicap: {handicap_data.initial_handicap}"
    )
    try:
        # 권한 확인
        current_user_id = current_user.id

        logger.info(f"권한 확인 - current_user_id: {current_user_id}, user_id: {user_id}")

        # 관리자 확인
        from models import Admin

        admin = (db.query(Admin).filter(Admin.id == current_user_id, Admin.deleted_at.is_(None)).first())

        if current_user_id != user_id and not admin:
            logger.warning(
                f"권한 없음 - current_user_id: {current_user_id}, user_id: {user_id}, is_admin: {admin is not None}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="핸디캡을 수정할 권한이 없습니다.",
            )

        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="사용자를 찾을 수 없습니다.",
            )

        from decimal import Decimal
        from utils.handicap_calculator import calculate_handicap_from_average_score
        from models import HandicapUpdateMethod

        # average_score가 있으면 자동 계산 (우선순위 1)
        if handicap_data.average_score is not None:
            # 평균 타수 유효성 검사
            if handicap_data.average_score < 55 or handicap_data.average_score > 144:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="평균 타수는 55 이상 144 이하여야 합니다.",
                )

            calculated_handicap = calculate_handicap_from_average_score(handicap_data.average_score)
            logger.info(
                f"평균 타수로부터 핸디캡 자동 계산 - user_id: {user_id}, average_score: {handicap_data.average_score}, calculated_handicap: {calculated_handicap}"
            )
            user.handicap_init = calculated_handicap
            user.average_score = handicap_data.average_score
            user.handicap_update_method = HandicapUpdateMethod.MANUAL
        # initial_handicap만 있으면 직접 입력 (하위 호환성)
        elif handicap_data.initial_handicap is not None:
            # 핸디캡 값을 float로 변환
            try:
                handicap_float = float(handicap_data.initial_handicap)
            except (ValueError, TypeError) as e:
                logger.error(f"핸디캡 값 변환 실패: {handicap_data.initial_handicap}, 에러: {e}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"핸디캡 값이 유효하지 않습니다: {handicap_data.initial_handicap}",
                )

            # 핸디캡 유효성 검사 (0 이상 72 이하)
            if handicap_float < 0 or handicap_float > 72:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="핸디캡은 0 이상 72 이하여야 합니다.",
                )

            # 핸디캡 값을 Decimal로 안전하게 변환 (소수점 1자리로 반올림)
            rounded_value = round(handicap_float, 1)
            handicap_decimal = Decimal(str(rounded_value))

            logger.info(f"핸디캡 직접 입력 - user_id: {user_id}, initial_handicap: {handicap_decimal}")
            user.handicap_init = handicap_decimal
            user.handicap_update_method = HandicapUpdateMethod.MANUAL
        else:
            # 둘 다 없으면 에러
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="average_score 또는 initial_handicap 중 하나는 필수입니다.",
            )

        try:
            db.commit()
            db.refresh(user)
            logger.info(
                f"핸디캡 수정 성공 - user_id: {user_id}, initial_handicap: {user.handicap_init}, average_score: {user.average_score}"
            )
        except Exception as db_error:
            db.rollback()
            logger.error(
                f"핸디캡 데이터베이스 업데이트 실패 - user_id: {user_id}, 에러: {db_error}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"핸디캡을 수정하는 중 데이터베이스 오류가 발생했습니다: {str(db_error)}",
            )

        return MessageResponse(message="초기 핸디캡이 성공적으로 업데이트되었습니다.", success=True)

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"핸디캡 수정 실패: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="핸디캡을 수정하는 중 오류가 발생했습니다.",
        )


@router.get("/handicap/calculate/{user_id}", response_model=HandicapResponse)
async def calculate_handicap(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_current_user),
):
    """사용자 핸디캡 자동 계산 (평균타수 기반)"""
    try:
        from utils.handicap_calculator import calculate_handicap_from_average_score as calc_handicap_from_average

        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="사용자를 찾을 수 없습니다.",
            )

        if not user.average_score:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="평균타수가 설정되지 않아 핸디캡을 계산할 수 없습니다.",
            )

        # 핸디캡 계산 (공통 계산 유틸 사용)
        calculated_handicap = float(calc_handicap_from_average(user.average_score))

        return HandicapResponse(
            user_id=user.id,
            nickname=user.nickname,
            handicap=float(user.handicap) if user.handicap else None,
            average_score=user.average_score,
            calculated_handicap=calculated_handicap,
            is_auto_calculated=True,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"핸디캡 계산 실패: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="핸디캡을 계산하는 중 오류가 발생했습니다.",
        )


# 스코어 히스토리 관련 스키마
class ScoreHistoryResponse(BaseModel):
    id: int
    meeting_id: int
    meeting_name: Optional[str] = None
    gross_score: int
    net_score: Optional[float] = None
    handicap_used: float
    played_at: datetime
    created_at: datetime


@router.get("/{user_id}/score-history", response_model=List[ScoreHistoryResponse])
async def get_user_score_history(
        user_id: int,
        limit: int = 10,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user),
):
    """최근 경기 스코어 조회"""
    try:
        # 사용자 존재 확인
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="사용자를 찾을 수 없습니다.",
            )

        # 권한 확인 (본인 또는 관리자)
        # Admin 모델 존재 여부로 관리자 확인
        is_admin = (db.query(Admin).filter(Admin.id == current_user.id, Admin.deleted_at.is_(None)).first() is not None)

        if current_user.id != user_id and not is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="스코어 히스토리 조회 권한이 없습니다.",
            )

        # 최근 스코어 조회
        from utils.handicap_calculator import get_recent_scores
        from decimal import Decimal

        score_history_list = get_recent_scores(db, user_id, count=limit)

        # Meeting 정보를 함께 조회하기 위해 join
        results = []
        for score_history in score_history_list:
            try:
                meeting = (db.query(Meeting).filter(Meeting.id == score_history.meeting_id).first())
                meeting_name = meeting.name if meeting else None

                # Decimal 타입을 float로 안전하게 변환
                net_score_value = None
                if score_history.net_score is not None:
                    if isinstance(score_history.net_score, Decimal):
                        net_score_value = float(score_history.net_score)
                    else:
                        net_score_value = float(score_history.net_score)

                handicap_used_value = 0.0
                if score_history.handicap_used is not None:
                    if isinstance(score_history.handicap_used, Decimal):
                        handicap_used_value = float(score_history.handicap_used)
                    else:
                        handicap_used_value = float(score_history.handicap_used)

                results.append(
                    ScoreHistoryResponse(
                        id=score_history.id,
                        meeting_id=score_history.meeting_id,
                        meeting_name=meeting_name,
                        gross_score=score_history.gross_score,
                        net_score=net_score_value,
                        handicap_used=handicap_used_value,
                        played_at=score_history.played_at,
                        created_at=score_history.created_at,
                    ))
            except Exception as item_error:
                logger.error(f"스코어 히스토리 항목 처리 실패 (id: {score_history.id}): {item_error}")
                continue

        return results

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"스코어 히스토리 조회 실패: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"스코어 히스토리를 조회하는 중 오류가 발생했습니다: {str(e)}",
        )


# 직전 대회 성적 조회 API
class MeetingResultResponse(BaseModel):
    id: int
    meeting_id: int
    meeting_name: Optional[str] = None
    user_id: int
    gross_score: int
    net_score: float
    rank: Optional[int] = None
    handicap_used: float
    completed_at: datetime
    created_at: datetime


@router.get("/{user_id}/last-meeting-result", response_model=Optional[MeetingResultResponse])
async def get_last_meeting_result(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user),
):
    """직전 대회 성적 조회"""
    try:
        # 사용자 존재 확인
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="사용자를 찾을 수 없습니다.",
            )

        # 권한 확인 (본인 또는 관리자)
        from models import Admin

        admin = (db.query(Admin).filter(Admin.id == current_user.id, Admin.deleted_at.is_(None)).first())

        if current_user.id != user_id and not admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="직전 대회 성적 조회 권한이 없습니다.",
            )

        # 가장 최근 MeetingResult와 UserScoreHistory 조인 조회
        from models import UserScoreHistory
        from sqlalchemy import desc

        result_query = (db.query(MeetingResult, UserScoreHistory).join(
            UserScoreHistory,
            (UserScoreHistory.meeting_id == MeetingResult.meeting_id)
            & (UserScoreHistory.user_id == MeetingResult.user_id),
        ).filter(MeetingResult.user_id == user_id).order_by(desc(MeetingResult.completed_at)).first())

        if not result_query:
            return None

        last_result, score_history = result_query

        # 모임 정보 조회
        from decimal import Decimal

        meeting = db.query(Meeting).filter(Meeting.id == last_result.meeting_id).first()
        meeting_name = meeting.name if meeting else None

        # Decimal 타입을 float로 안전하게 변환
        net_score_value = 0.0
        if score_history.net_score is not None:
            if isinstance(score_history.net_score, Decimal):
                net_score_value = float(score_history.net_score)
            else:
                net_score_value = float(score_history.net_score)

        handicap_used_value = 0.0
        if score_history.handicap_used is not None:
            if isinstance(score_history.handicap_used, Decimal):
                handicap_used_value = float(score_history.handicap_used)
            else:
                handicap_used_value = float(score_history.handicap_used)

        return MeetingResultResponse(
            id=last_result.id,
            meeting_id=last_result.meeting_id,
            meeting_name=meeting_name,
            user_id=last_result.user_id,
            gross_score=score_history.gross_score,
            net_score=net_score_value,
            rank=last_result.rank,
            handicap_used=handicap_used_value,
            completed_at=last_result.completed_at,
            created_at=last_result.created_at,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"직전 대회 성적 조회 실패: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"직전 대회 성적을 조회하는 중 오류가 발생했습니다: {str(e)}",
        )


# 라운딩 종료된 모임 목록 조회
class RoundingMeetingItem(BaseModel):
    meeting_id: int
    meeting_name: str
    meeting_time: datetime
    club_name: str
    rounding_completed_at: datetime
    has_score: bool
    has_hole_scores: bool
    gross_score: Optional[int] = None
    net_score: Optional[float] = None
    handicap_used: Optional[float] = None  # 해당 경기 넷 계산에 쓴 핸디캡 (경기 전 기준)
    handicap_after_round: Optional[float] = None  # 이 경기 반영 후 자동 재계산 핸디캡 (기록 표시 권장)


class RoundingMeetingsResponse(BaseModel):
    data: List[RoundingMeetingItem]
    total: int
    page: int
    limit: int
    total_pages: int


# 라운딩 통계 응답 스키마
class ScheduleMeetingItem(BaseModel):
    """일정표 모임 아이템"""

    id: int
    name: str
    description: Optional[str] = None
    location: Optional[str] = None
    meeting_time: Optional[datetime] = None
    meeting_type: str
    status: str
    club_id: int
    club_name: str


class DateScheduleItem(BaseModel):
    """날짜별 일정 아이템"""

    date: str  # YYYY-MM-DD 형식
    meetings: List[ScheduleMeetingItem]


class UserScheduleResponse(BaseModel):
    """유저 일정표 응답"""

    total_meetings: int
    date_range: Optional[dict] = None  # {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
    schedules: List[DateScheduleItem]


@router.get("/me/schedule", response_model=UserScheduleResponse)
async def get_my_schedule(
        start_date: Optional[str] = None,  # YYYY-MM-DD 형식
        end_date: Optional[str] = None,  # YYYY-MM-DD 형식
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_active_user),
):
    """내 일정표 조회 (참가한 모임만, 날짜별로 그룹화)"""
    try:
        from models import MeetingParticipant, Meeting, Club
        from datetime import datetime, timedelta
        from collections import defaultdict

        # 사용자가 참가한 모임 조회
        query = (
            db.query(MeetingParticipant, Meeting, Club).join(Meeting, MeetingParticipant.meeting_id == Meeting.id).join(
                Club, Meeting.club_id == Club.id).filter(
                    MeetingParticipant.user_id == current_user.id,
                    Meeting.meeting_time.isnot(None),  # 일정 시간이 있는 모임만
                ))

        # 날짜 필터 적용
        if start_date:
            try:
                start_datetime = datetime.strptime(start_date, "%Y-%m-%d")
                query = query.filter(Meeting.meeting_time >= start_datetime)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="잘못된 시작 날짜 형식입니다. YYYY-MM-DD 형식으로 입력해주세요.",
                )

        if end_date:
            try:
                end_datetime = datetime.strptime(end_date, "%Y-%m-%d")
                # 종료일은 하루 끝까지 포함
                end_datetime = end_datetime.replace(hour=23, minute=59, second=59, microsecond=999999)
                query = query.filter(Meeting.meeting_time <= end_datetime)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="잘못된 종료 날짜 형식입니다. YYYY-MM-DD 형식으로 입력해주세요.",
                )

        # 결과 조회 (날짜순으로 정렬)
        results = query.order_by(Meeting.meeting_time.asc()).all()

        # 날짜별로 그룹화
        schedules_by_date = defaultdict(list)
        date_range_start = None
        date_range_end = None

        for participant, meeting, club in results:
            if meeting.meeting_time:
                # 날짜 키 생성 (YYYY-MM-DD 형식)
                date_key = meeting.meeting_time.strftime("%Y-%m-%d")

                # 날짜 범위 업데이트
                if date_range_start is None or meeting.meeting_time < date_range_start:
                    date_range_start = meeting.meeting_time
                if date_range_end is None or meeting.meeting_time > date_range_end:
                    date_range_end = meeting.meeting_time

                schedules_by_date[date_key].append(
                    ScheduleMeetingItem(
                        id=meeting.id,
                        name=meeting.name,
                        description=meeting.description,
                        location=meeting.location,
                        meeting_time=meeting.meeting_time,
                        meeting_type=meeting.meeting_type.value if meeting.meeting_type else None,
                        status=str(meeting.status) if meeting.status else None,
                        club_id=club.id,
                        club_name=club.name,
                    ))

        # 날짜별로 정렬된 리스트 생성
        sorted_dates = sorted(schedules_by_date.keys())
        schedules = [DateScheduleItem(date=date_key, meetings=schedules_by_date[date_key]) for date_key in sorted_dates]

        # 날짜 범위 정보 구성
        date_range = None
        if date_range_start and date_range_end:
            date_range = {
                "start": date_range_start.strftime("%Y-%m-%d"),
                "end": date_range_end.strftime("%Y-%m-%d"),
            }

        return UserScheduleResponse(total_meetings=len(results), date_range=date_range, schedules=schedules)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"일정표 조회 실패: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"일정표를 조회하는 중 오류가 발생했습니다: {str(e)}",
        )


class RoundingStatsResponse(BaseModel):
    total_games: int
    average_score: Optional[float] = None
    recent_5_avg: Optional[float] = None
    best_score: Optional[int] = None
    worst_score: Optional[int] = None
    current_handicap: Optional[float] = None
    initial_handicap: Optional[float] = None


@router.get("/me/rounding-meetings", response_model=RoundingMeetingsResponse)
async def get_my_rounding_meetings(
        score_status: Optional[str] = None,  # all, missing, completed
        page: int = 1,
        limit: int = 10,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_active_user),
):
    """라운딩 종료된 모임 목록 조회 (점수 입력 상태 필터링 가능)"""
    try:
        from models import Club

        # 페이지네이션 계산
        offset = (page - 1) * limit

        # 기본 쿼리: 라운딩 종료된 모임만, 본인 참가 모임만
        query = (db.query(MeetingParticipant, Meeting, Club).join(Meeting,
                                                                  MeetingParticipant.meeting_id == Meeting.id).join(
                                                                      Club, Meeting.club_id == Club.id).filter(
                                                                          MeetingParticipant.user_id == current_user.id,
                                                                          Meeting.meeting_type == MeetingType.ROUND,
                                                                          Meeting.rounding_completed_at.isnot(None),
                                                                      ))

        # 점수 입력 상태 필터링
        if score_status == "missing":
            # 점수가 없는 모임만
            query = query.outerjoin(
                UserScoreHistory,
                (UserScoreHistory.meeting_id == Meeting.id)
                & (UserScoreHistory.user_id == current_user.id),
            ).filter(UserScoreHistory.id.is_(None))
        elif score_status == "completed":
            # 점수가 있는 모임만
            query = query.join(
                UserScoreHistory,
                (UserScoreHistory.meeting_id == Meeting.id)
                & (UserScoreHistory.user_id == current_user.id),
            )

        # 총 개수 계산
        total = query.count()

        # 결과 조회 (최신순)
        results = (query.order_by(Meeting.rounding_completed_at.desc()).offset(offset).limit(limit).all())

        # 점수 정보 조회
        meeting_ids = [meeting.id for _, meeting, _ in results]
        score_histories = (db.query(UserScoreHistory).filter(
            UserScoreHistory.user_id == current_user.id,
            UserScoreHistory.meeting_id.in_(meeting_ids),
        ).all()) if meeting_ids else []

        score_map = {sh.meeting_id: sh for sh in score_histories}

        # 응답 데이터 구성
        meetings_data = []
        for participant, meeting, club in results:
            score_history = score_map.get(meeting.id)
            handicap_used_val = None
            handicap_after_val = None
            if score_history and score_history.handicap_used is not None:
                handicap_used_val = float(score_history.handicap_used)
            if score_history and getattr(score_history, "handicap_after_round", None) is not None:
                handicap_after_val = float(score_history.handicap_after_round)
            meetings_data.append(
                RoundingMeetingItem(
                    meeting_id=meeting.id,
                    meeting_name=meeting.name,
                    meeting_time=meeting.meeting_time,
                    club_name=club.name,
                    rounding_completed_at=meeting.rounding_completed_at,
                    has_score=score_history is not None,
                    has_hole_scores=bool(participant.has_hole_scores),
                    gross_score=score_history.gross_score if score_history else None,
                    net_score=(float(score_history.net_score) if score_history and score_history.net_score else None),
                    handicap_used=handicap_used_val,
                    handicap_after_round=handicap_after_val,
                ))

        # 총 페이지 수 계산
        total_pages = (total + limit - 1) // limit

        return RoundingMeetingsResponse(
            data=meetings_data,
            total=total,
            page=page,
            limit=limit,
            total_pages=total_pages,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"라운딩 종료된 모임 목록 조회 실패: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="라운딩 종료된 모임 목록을 조회하는 중 오류가 발생했습니다.",
        )


@router.get("/me/rounding-stats", response_model=RoundingStatsResponse)
async def get_my_rounding_stats(db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """라운딩 기록 통계 조회"""
    try:
        from utils.handicap_calculator import (
            get_recent_scores,
            calculate_moving_average,
        )
        from sqlalchemy import func

        # 사용자의 모든 라운딩 점수 기록 조회 (점수가 입력된 것만)
        all_scores = (db.query(UserScoreHistory).filter(UserScoreHistory.user_id == current_user.id).order_by(
            UserScoreHistory.played_at.desc()).all())

        # 통계 계산
        total_games = len(all_scores)

        if total_games == 0:
            # 점수 기록이 없는 경우
            # 초기 핸디캡 조회
            initial_handicap = None
            if current_user.handicap_init is not None:
                initial_handicap = float(current_user.handicap_init)

            return RoundingStatsResponse(
                total_games=0,
                average_score=None,
                recent_5_avg=None,
                best_score=None,
                worst_score=None,
                current_handicap=None,
                initial_handicap=initial_handicap,
            )

        # 전체 평균 스코어
        gross_scores = [score.gross_score for score in all_scores]
        average_score = sum(gross_scores) / len(gross_scores) if gross_scores else None

        # 최고/최저 스코어
        best_score = min(gross_scores) if gross_scores else None
        worst_score = max(gross_scores) if gross_scores else None

        # 최근 5경기 평균
        recent_5_scores = all_scores[:5]
        recent_5_avg = None
        if len(recent_5_scores) > 0:
            recent_5_gross = [score.gross_score for score in recent_5_scores]
            recent_5_avg = sum(recent_5_gross) / len(recent_5_gross)

        # 현재 핸디캡 조회
        current_handicap = None
        if current_user.handicap is not None:
            current_handicap = float(current_user.handicap)
        elif current_user.handicap_init is not None:
            current_handicap = float(current_user.handicap_init)

        # 초기 핸디캡 조회
        initial_handicap = None
        if current_user.handicap_init is not None:
            initial_handicap = float(current_user.handicap_init)

        return RoundingStatsResponse(
            total_games=total_games,
            average_score=round(average_score, 1) if average_score else None,
            recent_5_avg=round(recent_5_avg, 1) if recent_5_avg else None,
            best_score=best_score,
            worst_score=worst_score,
            current_handicap=round(current_handicap, 1) if current_handicap else None,
            initial_handicap=round(initial_handicap, 1) if initial_handicap else None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"라운딩 통계 조회 실패: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="라운딩 통계를 조회하는 중 오류가 발생했습니다.",
        )


# 사용자 통계는 /api/v1/admin/users/stats 에서 조회 가능
