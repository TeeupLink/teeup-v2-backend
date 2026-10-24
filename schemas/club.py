# 클럽 관련 스키마
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from .enums import ClubType, ClubStatus, ClubRole, BillingCycle, RegulationStatus


# 클럽 관련 스키마
class ClubCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="클럽 이름")
    sido_code: str = Field(..., min_length=2, max_length=2, description="시도 코드")
    gungu_codes: List[str] = Field(..., min_length=1, max_length=4, description="군구 코드 목록 (1~4개)")
    type: ClubType = Field(default=ClubType.REGULAR, description="클럽 타입")
    description: Optional[str] = Field(None, description="클럽 설명")
    member_count: int = Field(..., ge=1, description="예상 멤버 수")
    contact_info: Optional[str] = Field(None, max_length=255, description="연락처")
    representative_name: Optional[str] = Field(None, max_length=255, description="대표자 이름")
    additional_info: Optional[str] = Field(None, description="추가 정보")


class ClubUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255, description="클럽 이름")
    sido_code: Optional[str] = Field(None, min_length=2, max_length=2, description="시도 코드")
    gungu_codes: Optional[List[str]] = Field(None, min_length=1, max_length=4, description="군구 코드 목록 (1~4개)")
    type: Optional[ClubType] = Field(None, description="클럽 타입")
    description: Optional[str] = Field(None, description="클럽 설명")
    member_count: Optional[int] = Field(None, ge=1, description="예상 멤버 수")
    contact_info: Optional[str] = Field(None, max_length=255, description="연락처")
    representative_name: Optional[str] = Field(None, max_length=255, description="대표자 이름")
    additional_info: Optional[str] = Field(None, description="추가 정보")
    application_deadline: Optional[datetime] = Field(None, description="신청 마감일")
    settlement_enabled: Optional[bool] = Field(None, description="모임 정산 기능 사용 여부")


class ClubFeeSummary(BaseModel):
    """클럽 상세용 회비 요약 (비회원도 조회 가능)"""
    has_regular_fee: bool = False
    amount: Optional[float] = None
    cycle: Optional[str] = None  # MONTHLY, QUARTERLY, YEARLY
    cycle_label: Optional[str] = None  # 월 1회, 분기 1회, 연 1회


class ClubResponse(BaseModel):
    id: int
    display_id: Optional[str]
    name: str
    sido_code: Optional[str] = None
    gungu_codes: Optional[List[str]] = None
    type: ClubType
    description: Optional[str]
    member_count: Optional[int]  # 예상 멤버 수 (레거시)
    current_member_count: Optional[int] = None  # 실제 멤버 수 (ACTIVE/APPROVED 상태)
    contact_info: Optional[str]
    representative_name: Optional[str]
    additional_info: Optional[str]
    status: ClubStatus
    settlement_enabled: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    membership_status: Optional[str] = None  # 현재 사용자의 멤버십 상태
    membership_role: Optional[str] = None  # 현재 사용자의 멤버십 역할
    fee_summary: Optional[ClubFeeSummary] = None  # 회비 요약 (비회원 포함 조회용)

    class Config:
        from_attributes = True


class ClubMembersResponse(BaseModel):
    id: int
    user_id: int
    user_name: str
    user_realname: Optional[str] = None
    user_nickname: str
    user_email: str
    user_phone_number: Optional[str] = None
    user_birthdate: Optional[str] = None
    user_gender: Optional[str] = None
    user_handicap: Optional[float] = None
    user_average_score: Optional[float] = None
    role: ClubRole
    status: str
    joined_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ClubMemberSearchItem(BaseModel):
    id: int = Field(..., description="구성원 사용자 ID")
    name: str = Field(..., description="구성원 이름 (realname 우선, 없으면 nickname)")
    gender: Optional[str] = Field(None, description="성별")
    handicap: Optional[float] = Field(
        None,
        description="핸디캡 (handicap 우선, 없으면 handicap_init 사용)",
    )


class ClubMemberSearchClub(BaseModel):
    club_id: int = Field(..., description="클럽 ID")
    club_display_id: Optional[str] = Field(None, description="클럽 display_id")
    club_name: str = Field(..., description="클럽 이름")
    members: List[ClubMemberSearchItem] = Field(..., description="검색된 구성원 목록")


class ClubMemberSearchResponse(BaseModel):
    keyword: str = Field(..., description="검색어")
    data: List[ClubMemberSearchClub] = Field(..., description="클럽별 검색 결과")
    total_clubs: int = Field(..., description="검색 결과에 포함된 클럽 수")
    total_members: int = Field(..., description="검색된 전체 구성원 수")


class ClubMembershipResponse(BaseModel):
    id: int
    club_id: int
    user_id: int
    role: ClubRole
    status: str
    joined_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ClubMemberRoundingHistoryItem(BaseModel):
    """해당 클럽 소속·공개 라운딩만 (타 클럽·프라이빗 제외) 멤버의 라운드별 스코어"""

    meeting_id: int
    meeting_name: str
    meeting_time: Optional[datetime] = None
    course_name: Optional[str] = None
    gross_score: int
    net_score: Optional[float] = None
    handicap_used: Optional[float] = None
    played_at: datetime


class ClubMemberRecordSummaryResponse(BaseModel):
    """클럽 구성원이 보는 멤버 기록 요약. 통계·내역은 요청한 클럽 모임만 (타 클럽·프라이빗 제외)."""

    user_id: int
    club_id: int
    handicap: Optional[float] = Field(None, description="회원 프로필 핸디(전역)")
    average_score: Optional[float] = Field(
        None, description="클럽 공개 라운딩 gross 평균 (타 클럽·프라이빗 제외)"
    )
    recent_rounds_count: int = Field(
        0, description="클럽 공개 라운딩 기록 건수 (타 클럽·프라이빗 제외)"
    )
    rounding_history: List[ClubMemberRoundingHistoryItem] = Field(
        default_factory=list,
        description="클럽 소속 공개 라운딩만 (타 클럽·프라이빗 제외), 최근 순",
    )


class ClubMemberAddRequest(BaseModel):
    user_id: int
    role: ClubRole = ClubRole.MEMBER


class ClubMemberRoleUpdateRequest(BaseModel):
    role: ClubRole


class MemberNoteUpdate(BaseModel):
    note: Optional[str] = None


class MemberNoteResponse(BaseModel):
    note: Optional[str] = None
    updated_at: Optional[datetime] = None


class RegularFeeUpdate(BaseModel):
    has_regular_fee: bool = Field(..., description="정기 회비 사용 여부")
    regular_fee_amount: Optional[float] = Field(None, ge=0, description="정기 회비 금액")
    regular_fee_cycle: Optional[BillingCycle] = Field(None, description="정기 회비 주기")
    regular_fee_description: Optional[str] = Field(None, description="정기 회비 설명")


class RegularFeeResponse(BaseModel):
    has_regular_fee: bool
    regular_fee_amount: Optional[float] = None
    regular_fee_cycle: Optional[BillingCycle] = None
    regular_fee_description: Optional[str] = None
    updated_at: Optional[datetime] = None


# 회비 항목 관련 스키마
class ClubFeeCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="회비 항목명")
    amount: float = Field(..., ge=0, description="회비 금액")
    cycle: Optional[BillingCycle] = Field(None, description="회비 주기")
    description: Optional[str] = Field(None, description="회비 설명")
    is_active: bool = Field(default=True, description="활성 여부")


class ClubFeeUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255, description="회비 항목명")
    amount: Optional[float] = Field(None, ge=0, description="회비 금액")
    cycle: Optional[BillingCycle] = Field(None, description="회비 주기")
    description: Optional[str] = Field(None, description="회비 설명")
    is_active: Optional[bool] = Field(None, description="활성 여부")


class ClubFeeResponse(BaseModel):
    id: int
    club_id: int
    name: str
    amount: float
    cycle: Optional[str] = None
    description: Optional[str] = None
    is_active: bool
    created_by: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# 클럽 공지사항 관련 스키마
class ClubNoticeCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="공지사항 제목")
    content: str = Field(..., min_length=1, description="공지사항 내용")
    is_important: bool = Field(default=False, description="중요 공지 여부")
    is_private: bool = Field(default=False, description="비공개 공지 여부")


class ClubNoticeUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255, description="공지사항 제목")
    content: Optional[str] = Field(None, min_length=1, description="공지사항 내용")
    is_important: Optional[bool] = Field(None, description="중요 공지 여부")
    is_private: Optional[bool] = Field(None, description="비공개 공지 여부")


class ClubNoticeResponse(BaseModel):
    id: int
    club_id: int
    title: str
    content: str
    is_important: bool
    is_private: bool
    author_id: int
    author_name: str
    view_count: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# 규정 관련 스키마
class RegulationCreate(BaseModel):
    category_id: int = Field(..., description="카테고리 ID")
    title: str = Field(..., min_length=1, max_length=255, description="규정 제목")
    content: str = Field(..., min_length=1, description="규정 내용")
    status: str = Field(default="ACTIVE", description="상태")


class RegulationUpdate(BaseModel):
    category_id: Optional[int] = Field(None, description="카테고리 ID")
    title: Optional[str] = Field(None, min_length=1, max_length=255, description="규정 제목")
    content: Optional[str] = Field(None, min_length=1, description="규정 내용")
    status: Optional[str] = Field(None, description="상태")


class RegulationResponse(BaseModel):
    id: int
    club_id: int
    category_id: int
    title: str
    content: str
    status: str
    created_by: int
    created_by_name: Optional[str] = None
    published_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class RegulationListResponse(BaseModel):
    regulations: List[RegulationResponse]
    total: int
    page: int
    limit: int
    total_pages: int


# 규정 카테고리 관련 스키마
class RegulationCategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="카테고리 이름")
    description: Optional[str] = Field(None, description="카테고리 설명")
    order: int = Field(default=0, description="정렬 순서")


class RegulationCategoryUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255, description="카테고리 이름")
    description: Optional[str] = Field(None, description="카테고리 설명")


class RegulationCategoryResponse(BaseModel):
    id: int
    club_id: int
    name: str
    order: int
    description: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# 클럽 규정 관련 스키마
class RegulationCategoryWithRegulations(RegulationCategoryResponse):
    regulations: List[RegulationResponse] = []


class ClubRegulationsResponse(BaseModel):
    categories: List[RegulationCategoryWithRegulations]
    total_categories: int = 0


class ClubRegulationResponse(BaseModel):
    id: int
    club_id: int
    category_id: int
    title: str
    content: str
    status: str
    created_by: Optional[int] = None
    created_by_name: Optional[str] = None
    published_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # 추가 필드 (프론트엔드에서 사용)
    category_name: Optional[str] = None

    model_config = {"from_attributes": True}


class ClubRegulationCreate(BaseModel):
    category_id: int = Field(..., description="카테고리 ID")
    title: str = Field(..., min_length=1, max_length=255, description="규정 제목")
    content: str = Field(..., min_length=1, description="규정 내용")
    status: str = Field(default="ACTIVE", description="상태")


class ClubRegulationUpdate(BaseModel):
    category_id: Optional[int] = Field(None, description="카테고리 ID")
    title: Optional[str] = Field(None, min_length=1, max_length=255, description="규정 제목")
    content: Optional[str] = Field(None, min_length=1, description="규정 내용")
    status: Optional[str] = Field(None, description="상태")
