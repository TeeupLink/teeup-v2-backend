# Enum 클래스 정의
import enum

class UserRole(str, enum.Enum):
    USER = "USER"
    ADMIN = "ADMIN"


class AdminRole(str, enum.Enum):
    """백오피스 관리자 역할"""
    SUPER_ADMIN = "SUPER_ADMIN"       # 전체 권한 (어드민 CRUD 포함)
    CLUB_ADMIN = "CLUB_ADMIN"         # 클럽 관리
    MEETING_ADMIN = "MEETING_ADMIN"   # 모임/라운딩 관리
    USER_ADMIN = "USER_ADMIN"         # 사용자 관리
    CONTENT_ADMIN = "CONTENT_ADMIN"   # 공지/FAQ/약관 관리
    SUPPORT_ADMIN = "SUPPORT_ADMIN"   # 문의 관리


class UserStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DEACTIVATED = "DEACTIVATED"
    DELETED = "DELETED"

class Provider(str, enum.Enum):
    LOCAL = "LOCAL"
    GOOGLE = "GOOGLE"
    APPLE = "APPLE"

class Gender(str, enum.Enum):
    MALE = "MALE"
    FEMALE = "FEMALE"
    OTHER = "OTHER"

class ClubType(str, enum.Enum):
    REGULAR = "REGULAR"
    IRREGULAR = "IRREGULAR"

class ClubStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"

class ClubRole(str, enum.Enum):
    LEADER = "LEADER"
    MANAGER = "MANAGER"
    MEMBER = "MEMBER"

class MembershipStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    PENDING = "PENDING"
    INACTIVE = "INACTIVE"
    SUSPENDED = "SUSPENDED"
    CANCELED = "CANCELED"

class MeetingType(str, enum.Enum):
    ROUND = "ROUND"
    SOCIAL = "SOCIAL"

class MeetingSubtype(str, enum.Enum):
    REGULAR = "REGULAR"
    IRREGULAR = "IRREGULAR"
    ONE_TIME = "ONE_TIME"

class SocialType(str, enum.Enum):
    """소셜 모임 유형"""
    CASUAL = "CASUAL"
    DINNER = "DINNER"
    EVENT = "EVENT"

class MeetingStatus(str, enum.Enum):
    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"

class SettlementMethod(str, enum.Enum):
    """정산 방법 (라운딩/소셜 공통)"""
    EQUAL_SPLIT = "EQUAL_SPLIT"
    INDIVIDUAL = "INDIVIDUAL"
    TREASURER_PREPAID = "TREASURER_PREPAID"
    CLUB_FUND = "CLUB_FUND"

class ParticipantStatus(str, enum.Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELED = "CANCELED"
    WAITING_LIST = "WAITING_LIST"

class ParticipantRole(str, enum.Enum):
    PARTICIPANT = "PARTICIPANT"
    ORGANIZER = "ORGANIZER"
    CO_ORGANIZER = "CO_ORGANIZER"

class ParticipantType(str, enum.Enum):
    USER = "USER"
    GUEST = "GUEST"

class PaymentStatus(str, enum.Enum):
    SUCCEEDED = "SUCCEEDED"
    PENDING = "PENDING"
    FAILED = "FAILED"
    CANCELED = "CANCELED"   # 결제 취소. Payment.canceled_at/cancel_reason 과 짝

class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    CANCELED = "CANCELED"
    PAST_DUE = "PAST_DUE"

class BillingCycle(str, enum.Enum):
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    YEARLY = "YEARLY"
    ONE_TIME = "ONE_TIME"

class TermsType(str, enum.Enum):
    SERVICE = "SERVICE"
    PRIVACY = "PRIVACY"
    PRIVACY_COLLECTION = "PRIVACY_COLLECTION"
    MARKETING = "MARKETING"

class InquiryType(str, enum.Enum):
    GENERAL = "GENERAL"
    TECHNICAL = "TECHNICAL"
    BILLING = "BILLING"
    FEATURE_REQUEST = "FEATURE_REQUEST"
    BUG_REPORT = "BUG_REPORT"
    ACCOUNT = "ACCOUNT"
    PAYMENT = "PAYMENT"


class InquiryStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"

class FeeType(str, enum.Enum):
    MONTHLY = "MONTHLY"
    SPECIAL = "SPECIAL"

class ExpenseStatus(str, enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    CANCELED = "CANCELED"


class ExpenseItemType(str, enum.Enum):
    """비용 항목 타입 (그린피/캐디피/카트비/기타 = 행으로 관리)"""
    GREEN_FEE = "GREEN_FEE"    # 그린피
    CADDY_FEE = "CADDY_FEE"    # 캐디피
    CART_FEE = "CART_FEE"      # 카트비
    OTHER = "OTHER"            # 기타 (라운딩 커스텀 항목 등)
    SOCIAL_ITEM = "SOCIAL_ITEM"  # 소셜 모임 전용 비용 항목

class NoticeType(str, enum.Enum):
    GENERAL = "GENERAL"
    SYSTEM = "SYSTEM"
    EVENT = "EVENT"
    MAINTENANCE = "MAINTENANCE"

class TokenRevokeReason(str, enum.Enum):
    LOGOUT = "LOGOUT"

class HandicapUpdateMethod(str, enum.Enum):
    MANUAL = "MANUAL"  # 수동 입력
    AUTO = "AUTO"      # 자동 계산

class NotificationType(str, enum.Enum):
    MEETING_CANCELLATION = "MEETING_CANCELLATION"
    MEETING_SETTLEMENT_COMPLETED = "MEETING_SETTLEMENT_COMPLETED"
    MEETING_COMPLETED = "MEETING_COMPLETED"
    MEETING_REMINDER = "MEETING_REMINDER"
    PAYMENT_RECEIVED = "PAYMENT_RECEIVED"
    CLUB_INVITATION = "CLUB_INVITATION"
    GENERAL = "GENERAL"
    SYSTEM = "SYSTEM"
    OTHER = "OTHER"
    NEW_NOTICE = "NEW_NOTICE"
    CLUB_MEMBERSHIP_APPROVED = "CLUB_MEMBERSHIP_APPROVED"
    CLUB_MEMBERSHIP_REJECTED = "CLUB_MEMBERSHIP_REJECTED"
    CLUB_MEMBERSHIP_REQUEST = "CLUB_MEMBERSHIP_REQUEST"
    TEAM_FORMATION_COMPLETED = "TEAM_FORMATION_COMPLETED"
    SOCIAL_SETTLEMENT_COMPLETED = "SOCIAL_SETTLEMENT_COMPLETED"

class NotificationStatus(str, enum.Enum):
    UNREAD = "UNREAD"
    READ = "READ"
    ARCHIVED = "ARCHIVED"

class ReportTargetType(str, enum.Enum):
    """신고 대상 종류"""
    CLUB = "CLUB"
    MEETING = "MEETING"
    CLUB_NOTICE = "CLUB_NOTICE"
    REGULATION = "REGULATION"
    USER = "USER"


class ReportReason(str, enum.Enum):
    """신고 사유"""
    SPAM = "SPAM"                    # 스팸·광고
    ABUSE = "ABUSE"                  # 욕설·비방·괴롭힘
    INAPPROPRIATE = "INAPPROPRIATE"  # 부적절한 콘텐츠
    FRAUD = "FRAUD"                  # 사기·허위 정보
    PRIVACY = "PRIVACY"              # 개인정보 노출
    OTHER = "OTHER"                  # 기타


class ReportStatus(str, enum.Enum):
    """신고 처리 상태"""
    PENDING = "PENDING"
    REVIEWING = "REVIEWING"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"
