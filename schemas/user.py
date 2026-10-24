# 사용자 관련 스키마
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from .enums import UserStatus, NotificationType, NotificationStatus, Provider


class UserCreate(BaseModel):
    email: str = Field(..., description="이메일")
    realname: str = Field(..., description="실명")
    nickname: str = Field(..., description="닉네임")
    password: str = Field(..., description="비밀번호")
    phone_number: str = Field(..., description="전화번호")
    birthdate: Optional[str] = Field(None, description="생년월일")
    gender: Optional[str] = Field(None, description="성별")
    handicap: Optional[float] = Field(None, description="핸디캡")
    average_score: Optional[int] = Field(None, description="평균 점수")
    # role: Optional[str] = Field(None, description="역할")
    status: Optional[str] = Field(None, description="상태")
    needs_terms_agreement: Optional[bool] = Field(None, description="약관 동의 필요")
    terms_agreement: Optional[bool] = Field(None, description="약관 동의")
    privacy_policy: Optional[bool] = Field(None, description="개인정보 처리방침")
    privacy_collection: Optional[bool] = Field(None, description="개인정보 수집")
    marketing_consent: Optional[bool] = Field(None, description="마케팅 동의")


class UserUpdate(BaseModel):
    email: Optional[str] = Field(None, description="이메일")
    realname: Optional[str] = Field(None, description="실명")
    nickname: Optional[str] = Field(None, description="닉네임")
    phone_number: Optional[str] = Field(None, description="전화번호")
    birthdate: Optional[str] = Field(None, description="생년월일")
    gender: Optional[str] = Field(None, description="성별")
    handicap_init: Optional[float] = Field(None, description="핸디캡")
    average_score_init: Optional[int] = Field(None, description="평균 점수")
    # role: Optional[str] = Field(None, description="역할")
    status: Optional[str] = Field(None, description="상태")
    needs_terms_agreement: Optional[bool] = Field(None, description="약관 동의 필요")
    terms_agreement: Optional[bool] = Field(None, description="약관 동의")
    privacy_policy: Optional[bool] = Field(None, description="개인정보 처리방침")
    privacy_collection: Optional[bool] = Field(None, description="개인정보 수집")
    marketing_consent: Optional[bool] = Field(None, description="마케팅 동의")


class UserResponse(BaseModel):
    id: int
    email: str
    realname: Optional[str] = None
    nickname: str
    phone_number: Optional[str] = None
    birthdate: Optional[datetime] = None
    gender: Optional[str] = None

    handicap: Optional[float] = None
    handicap_init: Optional[float] = None
    average_score: Optional[int] = None
    average_score_init: Optional[int] = None

    # role: str
    status: str
    provider: Optional[str] = None
    email_verified: Optional[datetime] = None
    needs_terms_agreement: bool = False
    terms_agreement: Optional[bool] = None
    privacy_policy: Optional[bool] = None
    privacy_collection: Optional[bool] = None
    marketing_consent: Optional[bool] = None
    club_count: Optional[int] = 0
    deactivated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class NotificationResponse(BaseModel):
    id: int
    user_id: int
    type: str
    title: str
    content: str
    status: str
    read_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

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
    push_enabled: Optional[bool] = Field(None, description="푸시 알림 활성화")
    email_enabled: Optional[bool] = Field(None, description="이메일 알림 활성화")
    meeting_reminders: Optional[bool] = Field(None, description="모임 알림")
    payment_notifications: Optional[bool] = Field(None, description="결제 알림")
    club_updates: Optional[bool] = Field(None, description="클럽 업데이트")
    marketing_emails: Optional[bool] = Field(None, description="마케팅 이메일")
