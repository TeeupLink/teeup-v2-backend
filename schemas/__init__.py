# Pydantic 스키마 정의 패키지
# 모든 스키마를 한 곳에서 export하여 기존 import 경로와 호환성 유지

# Enum 정의
from .enums import (
    ClubType, ClubStatus, ClubRole, BillingCycle,
    MeetingType, MeetingSubtype, SocialType, SettlementMethod,
    MeetingStatus, MeetingParticipantStatus, MeetingParticipantRole, ParticipantType,
    RegulationStatus, MembershipStatus,
    TeamFormationMode, TeamStatus,
    NotificationType, NotificationStatus,
    PlanType, PaymentStatus, PaymentMethod, PaymentMethodStatus, SubscriptionStatus,
    TermsType, UserStatus,
    NoticeType, InquiryType, InquiryStatus, Provider,
    ReportTargetType, ReportReason, ReportStatus)

# 공통 스키마
from .common import MessageResponse, PaginatedResponse

# 클럽 관련 스키마
from .club import (ClubCreate, ClubUpdate, ClubResponse, ClubMembersResponse, ClubMembershipResponse,
                   ClubMemberAddRequest, ClubMemberRoleUpdateRequest, ClubMemberSearchItem, ClubMemberSearchClub,
                   ClubMemberSearchResponse, MemberNoteUpdate, MemberNoteResponse, RegularFeeUpdate, RegularFeeResponse,
                   ClubFeeCreate, ClubFeeUpdate, ClubFeeResponse, ClubNoticeCreate, ClubNoticeUpdate,
                   ClubNoticeResponse, RegulationCreate, RegulationUpdate, RegulationResponse, RegulationListResponse,
                   RegulationCategoryCreate, RegulationCategoryUpdate, RegulationCategoryResponse,
                   RegulationCategoryWithRegulations, ClubRegulationsResponse, ClubRegulationResponse,
                   ClubRegulationCreate, ClubRegulationUpdate, ClubMemberRecordSummaryResponse,
                   ClubMemberRoundingHistoryItem)

# 모임 관련 스키마
from .meeting import (RoundingMeetingCreate, MeetingUpdate, MeetingResponse, MeetingParticipantResponse,
                      SocialMeetingCreate)

# 팀 관련 스키마
from .team import (TeamCreate, TeamUpdate, TeamResponse, TeamMemberResponse, GuestCreate, GuestUpdate, GuestResponse,
                   TeamFormationRequest, TeamFormationResponse, TeamMemberAddRequest, TeamBulkItem, TeamBulkUpdateRequest)

# 결제 관련 스키마 (플랜, 결제 수단, 구독, 결제 포함)
from .payment import (PlanCreate, PlanUpdate, PlanResponse, PaymentMethodCreate, PaymentMethodUpdate,
                      PaymentMethodResponse, SubscriptionCreate, SubscriptionUpdate, SubscriptionResponse,
                      PaymentResponse)

# 사용자 관련 스키마
from .user import (UserCreate, UserUpdate, UserResponse, NotificationResponse, NotificationSettingsResponse,
                   NotificationSettingsUpdate)

# 약관 관련 스키마
from .terms import (TermsCreate, TermsUpdate, TermsResponse, TermsListResponse, TermsAgreementCreate,
                    TermsAgreementResponse, TermsAgreementBulkCreate, TermsAgreementBulkResponse)

# 공지사항 관련 스키마
from .notice import (NoticeCreate, NoticeUpdate, NoticeResponse, NoticeListResponse)

# 문의 관련 스키마
from .moderation import (ContentReportCreate, ContentReportResponse, BlockedUserItem, BlockedUsersResponse)
from .inquiry import (InquiryCreate, InquiryUpdate, InquiryResponse, InquiryListResponse, InquiryResponseCreate,
                      InquiryResponseUpdate, InquiryResponseResponse, InquiryDetailResponse)

# 비용 정산 관련 스키마
from .expense import (ExpenseCreate, ExpenseUpdate, ExpenseResponse, ExpenseListResponse, ExpenseParticipantResponse,
                      ExpenseParticipantUpdate)

# 스코어 관련 스키마
from .score import (
    ScoreCreate,
    MeetingParticipantScoreCreate,
    ScoreUpdate,
    ScoreResponse,
    SimpleScoreCreate,
    SimpleScoreResponse,
    ScoreListResponse,
    ScoreStats,
)

# OAuth 관련 스키마
from .oauth import (OAuthLoginRequest, OAuthCallbackRequest, OAuthUserInfo, GoogleOAuthBody, AppleOAuthBody)

# FAQ 관련 스키마
from .faq import (FAQCategoryBase, FAQCategoryCreate, FAQCategoryUpdate, FAQCategoryResponse, FAQBase, FAQCreate,
                  FAQUpdate, FAQResponse, FAQPageResponse)

# 백오피스 관련 스키마
from .admin import (UserMeetingItem, UserMeetingsResponse, HandicapHistoryItem, HandicapInfo,
                    UserHandicapHistoryResponse, AdminResponse, AdminCreate, AdminUpdate, AdminPasswordUpdate)

# 기존 import 경로와의 호환성을 위해 모든 스키마를 export
__all__ = [
    # Enums
    "ClubType", "ClubStatus", "ClubRole", "BillingCycle",
    "MeetingType", "MeetingSubtype", "SocialType", "SettlementMethod",
    "MeetingStatus", "MeetingParticipantStatus", "MeetingParticipantRole",
    "RegulationStatus", "MembershipStatus",
    "TeamFormationMode", "TeamStatus",
    "NotificationType", "NotificationStatus",
    "PlanType", "PaymentStatus", "PaymentMethod", "PaymentMethodStatus", "SubscriptionStatus",
    "TermsType", "UserStatus",
    "NoticeType", "InquiryType", "InquiryStatus", "Provider",
    # Common
    "MessageResponse",
    "PaginatedResponse",
    # Club
    "ClubCreate",
    "ClubUpdate",
    "ClubResponse",
    "ClubMembersResponse",
    "ClubMembershipResponse",
    "ClubMemberRecordSummaryResponse",
    "ClubMemberRoundingHistoryItem",
    "ClubMemberAddRequest",
    "ClubMemberRoleUpdateRequest",
    "ClubMemberSearchItem",
    "ClubMemberSearchClub",
    "ClubMemberSearchResponse",
    "MemberNoteUpdate",
    "MemberNoteResponse",
    "RegularFeeUpdate",
    "RegularFeeResponse",
    "ClubFeeCreate",
    "ClubFeeUpdate",
    "ClubFeeResponse",
    "ClubNoticeCreate",
    "ClubNoticeUpdate",
    "ClubNoticeResponse",
    "RegulationCreate",
    "RegulationUpdate",
    "RegulationResponse",
    "RegulationListResponse",
    "RegulationCategoryCreate",
    "RegulationCategoryUpdate",
    "RegulationCategoryResponse",
    "RegulationCategoryWithRegulations",
    "ClubRegulationsResponse",
    "ClubRegulationResponse",
    "ClubRegulationCreate",
    "ClubRegulationUpdate",
    # Meeting
    "RoundingMeetingCreate",
    "MeetingUpdate",
    "MeetingResponse",
    "MeetingParticipantResponse",
    "SocialMeetingCreate",
    # Team
    "TeamCreate",
    "TeamUpdate",
    "TeamResponse",
    "TeamMemberResponse",
    "GuestCreate",
    "GuestUpdate",
    "GuestResponse",
    "TeamFormationRequest",
    "TeamFormationResponse",
    "TeamMemberAddRequest",
    "TeamBulkItem",
    "TeamBulkUpdateRequest",
    # Payment
    "PlanCreate",
    "PlanUpdate",
    "PlanResponse",
    "PaymentMethodCreate",
    "PaymentMethodUpdate",
    "PaymentMethodResponse",
    "SubscriptionCreate",
    "SubscriptionUpdate",
    "SubscriptionResponse",
    "PaymentResponse",
    # User
    "UserCreate",
    "UserUpdate",
    "UserResponse",
    "NotificationResponse",
    "NotificationSettingsResponse",
    "NotificationSettingsUpdate",
    # Terms
    "TermsCreate",
    "TermsUpdate",
    "TermsResponse",
    "TermsListResponse",
    "TermsAgreementCreate",
    "TermsAgreementResponse",
    "TermsAgreementBulkCreate",
    "TermsAgreementBulkResponse",
    # Notice
    "NoticeCreate",
    "NoticeUpdate",
    "NoticeResponse",
    "NoticeListResponse",
    # Inquiry
    "InquiryCreate",
    "InquiryUpdate",
    "InquiryResponse",
    "InquiryListResponse",
    "InquiryResponseCreate",
    "InquiryResponseUpdate",
    "InquiryResponseResponse",
    "InquiryDetailResponse",
    # Expense
    "ExpenseCreate",
    "ExpenseUpdate",
    "ExpenseResponse",
    "ExpenseListResponse",
    "ExpenseParticipantResponse",
    "ExpenseParticipantUpdate",
    # Score
    "ScoreCreate",
    "MeetingParticipantScoreCreate",
    "ScoreUpdate",
    "ScoreResponse",
    "SimpleScoreCreate",
    "SimpleScoreResponse",
    "ScoreListResponse",
    "ScoreStats",
    # OAuth
    "OAuthLoginRequest",
    "OAuthCallbackRequest",
    "OAuthUserInfo",
    "GoogleOAuthBody",
    "AppleOAuthBody",
    # FAQ
    "FAQCategoryBase",
    "FAQCategoryCreate",
    "FAQCategoryUpdate",
    "FAQCategoryResponse",
    "FAQBase",
    "FAQCreate",
    "FAQUpdate",
    "FAQResponse",
    "FAQPageResponse",
    # Admin
    "UserMeetingItem",
    "UserMeetingsResponse",
    "HandicapHistoryItem",
    "HandicapInfo",
    "UserHandicapHistoryResponse",
    "AdminResponse",
    "AdminCreate",
    "AdminUpdate",
    "AdminPasswordUpdate",
]

# Forward reference 해결을 위해 model_rebuild 호출
# 모든 모듈이 로드된 후 실행

TeamFormationResponse.model_rebuild()
