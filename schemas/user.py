# 사용자 관련 스키마
from pydantic import BaseModel, Field
from datetime import datetime
from .enums import UserStatus, NotificationType, NotificationStatus, Provider


class UserCreate(BaseModel):
    email: str = Field(..., description="이메일")
    realname: str = Field(..., description="실명")
    nickname: str = Field(..., description="닉네임")
    password: str = Field(..., description="비밀번호")
    phone_number: str = Field(..., description="전화번호")
    birthdate: str | None = Field(None, description="생년월일")
    gender: str | None = Field(None, description="성별")
    handicap: float | None = Field(None, description="핸디캡")
    average_score: int | None = Field(None, description="평균 점수")
    # role: Optional[str] = Field(None, description="역할")
    status: str | None = Field(None, description="상태")
    needs_terms_agreement: bool | None = Field(None, description="약관 동의 필요")
    terms_agreement: bool | None = Field(None, description="약관 동의")
    privacy_policy: bool | None = Field(None, description="개인정보 처리방침")
    privacy_collection: bool | None = Field(None, description="개인정보 수집")
    marketing_consent: bool | None = Field(None, description="마케팅 동의")


class UserUpdate(BaseModel):
    email: str | None = Field(None, description="이메일")
    realname: str | None = Field(None, description="실명")
    nickname: str | None = Field(None, description="닉네임")
    phone_number: str | None = Field(None, description="전화번호")
    birthdate: str | None = Field(None, description="생년월일")
    gender: str | None = Field(None, description="성별")
    handicap_init: float | None = Field(None, description="핸디캡")
    average_score_init: int | None = Field(None, description="평균 점수")
    # role: Optional[str] = Field(None, description="역할")
    status: str | None = Field(None, description="상태")
    needs_terms_agreement: bool | None = Field(None, description="약관 동의 필요")
    terms_agreement: bool | None = Field(None, description="약관 동의")
    privacy_policy: bool | None = Field(None, description="개인정보 처리방침")
    privacy_collection: bool | None = Field(None, description="개인정보 수집")
    marketing_consent: bool | None = Field(None, description="마케팅 동의")


class UserResponse(BaseModel):
    id: int
    email: str
    realname: str | None = None
    nickname: str
    phone_number: str | None = None
    birthdate: datetime | None = None
    gender: str | None = None

    handicap: float | None = None
    handicap_init: float | None = None
    average_score: int | None = None
    average_score_init: int | None = None

    # role: str
    status: str
    provider: str | None = None
    email_verified: datetime | None = None
    needs_terms_agreement: bool = False
    terms_agreement: bool | None = None
    privacy_policy: bool | None = None
    privacy_collection: bool | None = None
    marketing_consent: bool | None = None
    club_count: int | None = 0
    deactivated_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class NotificationResponse(BaseModel):
    id: int
    user_id: int
    type: str
    title: str
    content: str
    status: str
    read_at: datetime | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class NotificationSettingsResponse(BaseModel):
    user_id: int
    push_enabled: bool
    email_enabled: bool
    meeting_reminders: bool
    payment_notifications: bool
    club_updates: bool
    marketing_emails: bool

    model_config = {"from_attributes": True}


class NotificationSettingsUpdate(BaseModel):
    push_enabled: bool | None = Field(None, description="푸시 알림 활성화")
    email_enabled: bool | None = Field(None, description="이메일 알림 활성화")
    meeting_reminders: bool | None = Field(None, description="모임 알림")
    payment_notifications: bool | None = Field(None, description="결제 알림")
    club_updates: bool | None = Field(None, description="클럽 업데이트")
    marketing_emails: bool | None = Field(None, description="마케팅 이메일")
