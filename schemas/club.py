# 클럽 관련 스키마
from pydantic import BaseModel, Field
from datetime import datetime
from .enums import ClubType, ClubStatus, ClubRole, BillingCycle, RegulationStatus


# 클럽 관련 스키마
class ClubCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="클럽 이름")
    sido_code: str = Field(..., min_length=2, max_length=2, description="시도 코드")
    gungu_codes: list[str] = Field(..., min_length=1, max_length=4, description="군구 코드 목록 (1~4개)")
    type: ClubType = Field(default=ClubType.REGULAR, description="클럽 타입")
    description: str | None = Field(None, description="클럽 설명")
    member_count: int = Field(..., ge=1, description="예상 멤버 수")
    contact_info: str | None = Field(None, max_length=255, description="연락처")
    representative_name: str | None = Field(None, max_length=255, description="대표자 이름")
    additional_info: str | None = Field(None, description="추가 정보")


class ClubUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255, description="클럽 이름")
    sido_code: str | None = Field(None, min_length=2, max_length=2, description="시도 코드")
    gungu_codes: list[str] | None = Field(None, min_length=1, max_length=4, description="군구 코드 목록 (1~4개)")
    type: ClubType | None = Field(None, description="클럽 타입")
    description: str | None = Field(None, description="클럽 설명")
    member_count: int | None = Field(None, ge=1, description="예상 멤버 수")
    contact_info: str | None = Field(None, max_length=255, description="연락처")
    representative_name: str | None = Field(None, max_length=255, description="대표자 이름")
    additional_info: str | None = Field(None, description="추가 정보")
    application_deadline: datetime | None = Field(None, description="신청 마감일")
    settlement_enabled: bool | None = Field(None, description="모임 정산 기능 사용 여부")


class ClubFeeSummary(BaseModel):
    """클럽 상세용 회비 요약 (비회원도 조회 가능)"""
    has_regular_fee: bool = False
    amount: float | None = None
    cycle: str | None = None  # MONTHLY, QUARTERLY, YEARLY
    cycle_label: str | None = None  # 월 1회, 분기 1회, 연 1회


class ClubResponse(BaseModel):
    id: int
    display_id: str | None
    name: str
    sido_code: str | None = None
    gungu_codes: list[str] | None = None
    type: ClubType
    description: str | None
    member_count: int | None  # 예상 멤버 수 (레거시)
    current_member_count: int | None = None  # 실제 멤버 수 (ACTIVE/APPROVED 상태)
    contact_info: str | None
    representative_name: str | None
    additional_info: str | None
    status: ClubStatus
    settlement_enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
    membership_status: str | None = None  # 현재 사용자의 멤버십 상태
    membership_role: str | None = None  # 현재 사용자의 멤버십 역할
    fee_summary: ClubFeeSummary | None = None  # 회비 요약 (비회원 포함 조회용)

    class Config:
        from_attributes = True


class ClubMembersResponse(BaseModel):
    id: int
    user_id: int
    user_name: str
    user_realname: str | None = None
    user_nickname: str
    user_email: str
    user_phone_number: str | None = None
    user_birthdate: str | None = None
    user_gender: str | None = None
    user_handicap: float | None = None
    user_average_score: float | None = None
    role: ClubRole
    status: str
    joined_at: datetime | None = None

    model_config = {"from_attributes": True}


class ClubMemberSearchItem(BaseModel):
    id: int = Field(..., description="구성원 사용자 ID")
    name: str = Field(..., description="구성원 이름 (realname 우선, 없으면 nickname)")
    gender: str | None = Field(None, description="성별")
    handicap: float | None = Field(
        None,
        description="핸디캡 (handicap 우선, 없으면 handicap_init 사용)",
    )


class ClubMemberSearchClub(BaseModel):
    club_id: int = Field(..., description="클럽 ID")
    club_display_id: str | None = Field(None, description="클럽 display_id")
    club_name: str = Field(..., description="클럽 이름")
    members: list[ClubMemberSearchItem] = Field(..., description="검색된 구성원 목록")


class ClubMemberSearchResponse(BaseModel):
    keyword: str = Field(..., description="검색어")
    data: list[ClubMemberSearchClub] = Field(..., description="클럽별 검색 결과")
    total_clubs: int = Field(..., description="검색 결과에 포함된 클럽 수")
    total_members: int = Field(..., description="검색된 전체 구성원 수")


class ClubMembershipResponse(BaseModel):
    id: int
    club_id: int
    user_id: int
    role: ClubRole
    status: str
    joined_at: datetime | None = None

    model_config = {"from_attributes": True}


class ClubMemberRoundingHistoryItem(BaseModel):
    """해당 클럽 소속·공개 라운딩만 (타 클럽·프라이빗 제외) 멤버의 라운드별 스코어"""

    meeting_id: int
    meeting_name: str
    meeting_time: datetime | None = None
    course_name: str | None = None
    gross_score: int
    net_score: float | None = None
    handicap_used: float | None = None
    played_at: datetime


class ClubMemberRecordSummaryResponse(BaseModel):
    """클럽 구성원이 보는 멤버 기록 요약. 통계·내역은 요청한 클럽 모임만 (타 클럽·프라이빗 제외)."""

    user_id: int
    club_id: int
    handicap: float | None = Field(None, description="회원 프로필 핸디(전역)")
    average_score: float | None = Field(
        None, description="클럽 공개 라운딩 gross 평균 (타 클럽·프라이빗 제외)"
    )
    recent_rounds_count: int = Field(
        0, description="클럽 공개 라운딩 기록 건수 (타 클럽·프라이빗 제외)"
    )
    rounding_history: list[ClubMemberRoundingHistoryItem] = Field(
        default_factory=list,
        description="클럽 소속 공개 라운딩만 (타 클럽·프라이빗 제외), 최근 순",
    )


class ClubMemberAddRequest(BaseModel):
    user_id: int
    role: ClubRole = ClubRole.MEMBER


class ClubMemberRoleUpdateRequest(BaseModel):
    role: ClubRole


class MemberNoteUpdate(BaseModel):
    note: str | None = None


class MemberNoteResponse(BaseModel):
    note: str | None = None
    updated_at: datetime | None = None


class RegularFeeUpdate(BaseModel):
    has_regular_fee: bool = Field(..., description="정기 회비 사용 여부")
    regular_fee_amount: float | None = Field(None, ge=0, description="정기 회비 금액")
    regular_fee_cycle: BillingCycle | None = Field(None, description="정기 회비 주기")
    regular_fee_description: str | None = Field(None, description="정기 회비 설명")


class RegularFeeResponse(BaseModel):
    has_regular_fee: bool
    regular_fee_amount: float | None = None
    regular_fee_cycle: BillingCycle | None = None
    regular_fee_description: str | None = None
    updated_at: datetime | None = None


# 회비 항목 관련 스키마
class ClubFeeCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="회비 항목명")
    amount: float = Field(..., ge=0, description="회비 금액")
    cycle: BillingCycle | None = Field(None, description="회비 주기")
    description: str | None = Field(None, description="회비 설명")
    is_active: bool = Field(default=True, description="활성 여부")


class ClubFeeUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255, description="회비 항목명")
    amount: float | None = Field(None, ge=0, description="회비 금액")
    cycle: BillingCycle | None = Field(None, description="회비 주기")
    description: str | None = Field(None, description="회비 설명")
    is_active: bool | None = Field(None, description="활성 여부")


class ClubFeeResponse(BaseModel):
    id: int
    club_id: int
    name: str
    amount: float
    cycle: str | None = None
    description: str | None = None
    is_active: bool
    created_by: int
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


# 클럽 공지사항 관련 스키마
class ClubNoticeCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="공지사항 제목")
    content: str = Field(..., min_length=1, description="공지사항 내용")
    is_important: bool = Field(default=False, description="중요 공지 여부")
    is_private: bool = Field(default=False, description="비공개 공지 여부")


class ClubNoticeUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255, description="공지사항 제목")
    content: str | None = Field(None, min_length=1, description="공지사항 내용")
    is_important: bool | None = Field(None, description="중요 공지 여부")
    is_private: bool | None = Field(None, description="비공개 공지 여부")


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
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


# 규정 관련 스키마
class RegulationCreate(BaseModel):
    category_id: int = Field(..., description="카테고리 ID")
    title: str = Field(..., min_length=1, max_length=255, description="규정 제목")
    content: str = Field(..., min_length=1, description="규정 내용")
    status: str = Field(default="ACTIVE", description="상태")


class RegulationUpdate(BaseModel):
    category_id: int | None = Field(None, description="카테고리 ID")
    title: str | None = Field(None, min_length=1, max_length=255, description="규정 제목")
    content: str | None = Field(None, min_length=1, description="규정 내용")
    status: str | None = Field(None, description="상태")


class RegulationResponse(BaseModel):
    id: int
    club_id: int
    category_id: int
    title: str
    content: str
    status: str
    created_by: int
    created_by_name: str | None = None
    published_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class RegulationListResponse(BaseModel):
    regulations: list[RegulationResponse]
    total: int
    page: int
    limit: int
    total_pages: int


# 규정 카테고리 관련 스키마
class RegulationCategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="카테고리 이름")
    description: str | None = Field(None, description="카테고리 설명")
    order: int = Field(default=0, description="정렬 순서")


class RegulationCategoryUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255, description="카테고리 이름")
    description: str | None = Field(None, description="카테고리 설명")


class RegulationCategoryResponse(BaseModel):
    id: int
    club_id: int
    name: str
    order: int
    description: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


# 클럽 규정 관련 스키마
class RegulationCategoryWithRegulations(RegulationCategoryResponse):
    regulations: list[RegulationResponse] = []


class ClubRegulationsResponse(BaseModel):
    categories: list[RegulationCategoryWithRegulations]
    total_categories: int = 0


class ClubRegulationResponse(BaseModel):
    id: int
    club_id: int
    category_id: int
    title: str
    content: str
    status: str
    created_by: int | None = None
    created_by_name: str | None = None
    published_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # 추가 필드 (프론트엔드에서 사용)
    category_name: str | None = None

    model_config = {"from_attributes": True}


class ClubRegulationCreate(BaseModel):
    category_id: int = Field(..., description="카테고리 ID")
    title: str = Field(..., min_length=1, max_length=255, description="규정 제목")
    content: str = Field(..., min_length=1, description="규정 내용")
    status: str = Field(default="ACTIVE", description="상태")


class ClubRegulationUpdate(BaseModel):
    category_id: int | None = Field(None, description="카테고리 ID")
    title: str | None = Field(None, min_length=1, max_length=255, description="규정 제목")
    content: str | None = Field(None, min_length=1, description="규정 내용")
    status: str | None = Field(None, description="상태")
