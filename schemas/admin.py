# 백오피스 사용자 상세페이지용 스키마
from pydantic import BaseModel, Field
from datetime import datetime


# 관리자(Admin) CRUD용 스키마
class AdminResponse(BaseModel):
    """관리자 조회 응답 (비밀번호 제외)"""
    id: int
    email: str
    name: str
    profile_image: str | None = None
    phone_number: str | None = None
    provider: str | None = None
    status: str
    role: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class AdminCreate(BaseModel):
    """관리자 생성"""
    email: str = Field(..., description="이메일")
    password: str = Field(..., description="비밀번호 (8자 이상, 영문·숫자·특수문자)")
    name: str = Field(..., description="관리자 이름")
    phone_number: str | None = Field(None, description="전화번호")
    profile_image: str | None = Field(None, description="프로필 이미지 URL")
    role: str | None = Field(None, description="역할 (SUPER_ADMIN, CLUB_ADMIN, MEETING_ADMIN, USER_ADMIN, CONTENT_ADMIN, SUPPORT_ADMIN). 필수")


class AdminUpdate(BaseModel):
    """관리자 수정"""
    name: str | None = Field(None, description="관리자 이름")
    phone_number: str | None = Field(None, description="전화번호")
    profile_image: str | None = Field(None, description="프로필 이미지 URL")
    status: str | None = Field(None, description="상태 (ACTIVE, DEACTIVATED)")
    role: str | None = Field(None, description="역할")


class AdminPasswordUpdate(BaseModel):
    """관리자 비밀번호 변경 (다른 관리자가 변경)"""
    new_password: str = Field(..., description="새 비밀번호 (8자 이상, 영문·숫자·특수문자)")
# 백오피스 사용자 상세페이지용 스키마
class UserMeetingItem(BaseModel):
    """사용자 참가 모임 정보"""
    meeting_id: int
    meeting_name: str
    club_name: str
    meeting_time: datetime | None = None
    status: str
    joined_at: datetime | None = None
    rounding_completed_at: datetime | None = None
    has_score: bool = False
    gross_score: int | None = None
    net_score: float | None = None

    model_config = {"from_attributes": True}

class UserMeetingsResponse(BaseModel):
    """사용자별 모임 참가 목록 응답"""
    rounding_meetings: list[UserMeetingItem] = []
    social_meetings: list[UserMeetingItem] = []
    total_rounding: int = 0
    total_social: int = 0

    model_config = {"from_attributes": True}

class HandicapHistoryItem(BaseModel):
    """핸디캡 업데이트 이력 항목"""
    id: int
    meeting_id: int
    meeting_name: str | None = None
    club_name: str | None = None
    gross_score: int
    net_score: float | None = None
    handicap_used: float
    played_at: datetime  # user_score_history.played_at 은 DB 가 NOT NULL
    created_at: datetime | None = None

    model_config = {"from_attributes": True}

class HandicapInfo(BaseModel):
    """현재 핸디캡 정보"""
    initial_handicap: float | None = None
    calculated_handicap: float | None = None
    handicap_update_method: str | None = None
    handicap_calculation_count: int | None = None
    last_updated_at: datetime | None = None

    model_config = {"from_attributes": True}

class UserHandicapHistoryResponse(BaseModel):
    """사용자별 핸디캡 업데이트 이력 응답"""
    handicap_info: HandicapInfo
    score_history: list[HandicapHistoryItem] = []
    total: int = 0
    page: int = 1
    limit: int = 10
    total_pages: int = 0

    model_config = {"from_attributes": True}
