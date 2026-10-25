# 백오피스 사용자 상세페이지용 스키마
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime


# 관리자(Admin) CRUD용 스키마
class AdminResponse(BaseModel):
    """관리자 조회 응답 (비밀번호 제외)"""
    id: int
    email: str
    name: str
    profile_image: Optional[str] = None
    phone_number: Optional[str] = None
    provider: Optional[str] = None
    status: str
    role: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class AdminCreate(BaseModel):
    """관리자 생성"""
    email: str = Field(..., description="이메일")
    password: str = Field(..., min_length=6, description="비밀번호")
    name: str = Field(..., description="관리자 이름")
    phone_number: Optional[str] = Field(None, description="전화번호")
    profile_image: Optional[str] = Field(None, description="프로필 이미지 URL")
    role: Optional[str] = Field("SUPER_ADMIN", description="역할 (SUPER_ADMIN, CLUB_ADMIN, MEETING_ADMIN, USER_ADMIN, CONTENT_ADMIN, SUPPORT_ADMIN)")


class AdminUpdate(BaseModel):
    """관리자 수정"""
    name: Optional[str] = Field(None, description="관리자 이름")
    phone_number: Optional[str] = Field(None, description="전화번호")
    profile_image: Optional[str] = Field(None, description="프로필 이미지 URL")
    status: Optional[str] = Field(None, description="상태 (ACTIVE, DEACTIVATED)")
    role: Optional[str] = Field(None, description="역할")


class AdminPasswordUpdate(BaseModel):
    """관리자 비밀번호 변경 (다른 관리자가 변경)"""
    new_password: str = Field(..., min_length=6, description="새 비밀번호")
# 백오피스 사용자 상세페이지용 스키마
class UserMeetingItem(BaseModel):
    """사용자 참가 모임 정보"""
    meeting_id: int
    meeting_name: str
    club_name: str
    meeting_time: Optional[datetime] = None
    status: str
    joined_at: Optional[datetime] = None
    rounding_completed_at: Optional[datetime] = None
    has_score: bool = False
    gross_score: Optional[int] = None
    net_score: Optional[float] = None

    model_config = {"from_attributes": True}

class UserMeetingsResponse(BaseModel):
    """사용자별 모임 참가 목록 응답"""
    rounding_meetings: List[UserMeetingItem] = []
    social_meetings: List[UserMeetingItem] = []
    total_rounding: int = 0
    total_social: int = 0

    model_config = {"from_attributes": True}

class HandicapHistoryItem(BaseModel):
    """핸디캡 업데이트 이력 항목"""
    id: int
    meeting_id: int
    meeting_name: Optional[str] = None
    club_name: Optional[str] = None
    gross_score: int
    net_score: Optional[float] = None
    handicap_used: float
    played_at: datetime  # user_score_history.played_at 은 DB 가 NOT NULL
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}

class HandicapInfo(BaseModel):
    """현재 핸디캡 정보"""
    initial_handicap: Optional[float] = None
    calculated_handicap: Optional[float] = None
    handicap_update_method: Optional[str] = None
    handicap_calculation_count: Optional[int] = None
    last_updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}

class UserHandicapHistoryResponse(BaseModel):
    """사용자별 핸디캡 업데이트 이력 응답"""
    handicap_info: HandicapInfo
    score_history: List[HandicapHistoryItem] = []
    total: int = 0
    page: int = 1
    limit: int = 10
    total_pages: int = 0

    model_config = {"from_attributes": True}
