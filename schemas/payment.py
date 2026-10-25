# 결제 관련 스키마 (플랜, 결제 수단, 구독, 결제 포함)
from pydantic import BaseModel, Field
from typing import Any
from datetime import datetime
from .enums import PlanType, BillingCycle, PaymentMethod, PaymentMethodStatus, PaymentStatus, SubscriptionStatus

# 플랜 관련 스키마
class PlanCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="플랜 이름")
    description: str | None = Field(None, description="플랜 설명")
    type: PlanType = Field(..., description="플랜 타입")
    price: float = Field(..., ge=0, description="가격")
    billing_cycle: BillingCycle = Field(..., description="결제 주기")
    features: dict[str, Any] | None = Field(None, description="기능 목록")
    max_clubs: int | None = Field(None, ge=1, description="최대 클럽 수")
    max_members_per_club: int | None = Field(None, ge=1, description="클럽당 최대 멤버 수")
    max_meetings_per_month: int | None = Field(None, ge=1, description="월 최대 모임 수")

class PlanUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255, description="플랜 이름")
    description: str | None = Field(None, description="플랜 설명")
    type: PlanType | None = Field(None, description="플랜 타입")
    price: float | None = Field(None, ge=0, description="가격")
    billing_cycle: BillingCycle | None = Field(None, description="결제 주기")
    features: dict[str, Any] | None = Field(None, description="기능 목록")
    max_clubs: int | None = Field(None, ge=1, description="최대 클럽 수")
    max_members_per_club: int | None = Field(None, ge=1, description="클럽당 최대 멤버 수")
    max_meetings_per_month: int | None = Field(None, ge=1, description="월 최대 모임 수")

class PlanResponse(BaseModel):
    id: int
    name: str
    description: str | None
    type: str
    price: float
    billing_cycle: str
    features: dict[str, Any] | None
    max_clubs: int | None
    max_members_per_club: int | None
    max_meetings_per_month: int | None
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
    
    model_config = {"from_attributes": True}

# 결제 수단 관련 스키마
class PaymentMethodCreate(BaseModel):
    method_type: PaymentMethod = Field(..., description="결제 수단 타입")
    method_name: str = Field(..., min_length=1, max_length=255, description="결제 수단 이름")
    masked_info: str | None = Field(None, max_length=255, description="마스킹된 정보")
    is_default: bool = Field(default=False, description="기본 결제 수단 여부")

class PaymentMethodUpdate(BaseModel):
    method_type: PaymentMethod | None = Field(None, description="결제 수단 타입")
    method_name: str | None = Field(None, min_length=1, max_length=255, description="결제 수단 이름")
    masked_info: str | None = Field(None, max_length=255, description="마스킹된 정보")
    is_default: bool | None = Field(None, description="기본 결제 수단 여부")

class PaymentMethodResponse(BaseModel):
    id: int
    user_id: int
    method_type: str
    method_name: str
    masked_info: str | None
    is_default: bool
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    
    model_config = {"from_attributes": True}

# 구독 관련 스키마
class SubscriptionCreate(BaseModel):
    plan_id: int = Field(..., description="플랜 ID")
    start_date: datetime | None = Field(None, description="시작 날짜")

class SubscriptionUpdate(BaseModel):
    plan_id: int | None = Field(None, description="플랜 ID")
    status: SubscriptionStatus | None = Field(None, description="구독 상태")
    end_date: datetime | None = Field(None, description="종료 날짜")

class SubscriptionResponse(BaseModel):
    id: int
    user_id: int
    plan_id: int
    plan_name: str
    status: str
    start_date: datetime
    next_billing_date: datetime | None
    end_date: datetime | None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    
    model_config = {"from_attributes": True}

# 결제 관련 스키마
class PaymentResponse(BaseModel):
    payment_key: str
    order_id: str
    order_name: str
    status: str
    requested_at: str
    approved_at: str | None
    total_amount: int
    method: str
    card: dict | None
    virtual_account: dict | None
    
    model_config = {"from_attributes": True}

