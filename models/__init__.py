# 모델 패키지 초기화 파일
# 모든 모델을 한 곳에서 import할 수 있도록 함

# Enum 클래스들
from .enums import (UserStatus, Provider, Gender, ClubType, ClubStatus, ClubRole, MembershipStatus, MeetingType, MeetingSubtype, SocialType, MeetingStatus,
                    SettlementMethod, ParticipantStatus, ParticipantRole, ParticipantType, PaymentStatus, SubscriptionStatus,
                    BillingCycle, TermsType, InquiryType, InquiryStatus, FeeType, ExpenseStatus, ExpenseItemType, NoticeType, TokenRevokeReason,
                    HandicapUpdateMethod, NotificationType, NotificationStatus, AdminRole,
                    ReportTargetType, ReportReason, ReportStatus)

# User 관련 모델
from .user import User, RefreshTokenBlacklist

# Admin 관련 모델
from .admin import Admin

# Club 관련 모델
from .club import (Club, ClubMembership, ClubNotice, ClubFee, RegulationCategory, Regulation, ClubRegion)

# Meeting 관련 모델
from .meeting import (Guest, Meeting, MeetingParticipant, Team, TeamMember, Expense, ExpenseItem,
                      ExpenseItemParticipant, Score, UserScoreHistory, MeetingResult)

# Payment 관련 모델
from .payment import Plan, Payment, UserPaymentMethod, Subscription

# Terms 관련 모델
from .terms import Terms, TermsAgreement

# Inquiry 관련 모델
from .inquiry import Inquiry, InquiryResponse

# 신고 / 차단 모델
from .moderation import ContentReport, UserBlock

# 감사 로그 (backend #49)
from .audit import AdminAuditLog, AuditMonthlyReview

# 회원 활동 로그 (backend #50)
from .activity import UserActivityLog

# 기타 모델
from .notification import Notification
from .device_token import UserDeviceToken
from .notice import Notice
from .faq import FAQCategory, FAQ

from .region import Sido, Gungu

# 모든 모델을 __all__에 추가
__all__ = [
    # Enums
    "UserStatus",
    "Provider",
    "Gender",
    "ClubType",
    "ClubStatus",
    "ClubRole",
    "MembershipStatus",
    "MeetingType",
    "MeetingSubtype",
    "SocialType",
    "MeetingStatus",
    "SettlementMethod",
    "ParticipantStatus",
    "ParticipantRole",
    "ParticipantType",
    "PaymentStatus",
    "SubscriptionStatus",
    "BillingCycle",
    "TermsType",
    "InquiryStatus",
    "FeeType",
    "ExpenseStatus",
    "NoticeType",
    "TokenRevokeReason",
    "HandicapUpdateMethod",
    "NotificationType",
    "NotificationStatus",
    "AdminRole",
    # User
    "User",
    "RefreshTokenBlacklist",
    # Admin
    "Admin",
    "AdminAuditLog",
    "AuditMonthlyReview",
    "UserActivityLog",
    # Club
    "Club",
    "ClubMembership",
    "ClubNotice",
    "ClubFee",
    "RegulationCategory",
    "Regulation",
    "ClubRegion",
    # Meeting
    "Guest",
    "Meeting",
    "MeetingParticipant",
    "Team",
    "TeamMember",
    "Expense",
    "ExpenseItem",
    "ExpenseItemParticipant",
    "Score",
    "UserScoreHistory",
    "MeetingResult",
    # Payment
    "Plan",
    "Payment",
    "UserPaymentMethod",
    "Subscription",
    # Terms
    "Terms",
    "TermsAgreement",
    # Inquiry
    "Inquiry",
    "InquiryResponse",
    # Others
    "Notification",
    "UserDeviceToken",
    "Notice",
    "FAQCategory",
    "FAQ"
]
