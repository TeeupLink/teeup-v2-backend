# 결제 관련 스키마 (플랜, 결제 수단, 구독, 결제 포함)
from pydantic import BaseModel, Field
from typing import List, Optional, Any, Dict
from datetime import datetime
from .enums import PlanType, BillingCycle, PaymentMethod, PaymentMethodStatus, PaymentStatus, SubscriptionStatus

# 플랜 관련 스키마
class PlanCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="플랜 이름")
    description: Optional[str] = Field(None, description="플랜 설명")
    type: PlanType = Field(..., description="플랜 타입")
    price: float = Field(..., ge=0, description="가격")
    billing_cycle: BillingCycle = Field(..., description="결제 주기")
    features: Optional[Dict[str, Any]] = Field(None, description="기능 목록")
    max_clubs: Optional[int] = Field(None, ge=1, description="최대 클럽 수")
    max_members_per_club: Optional[int] = Field(None, ge=1, description="클럽당 최대 멤버 수")
    max_meetings_per_month: Optional[int] = Field(None, ge=1, description="월 최대 모임 수")

class PlanUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255, description="플랜 이름")
    description: Optional[str] = Field(None, description="플랜 설명")
    type: Optional[PlanType] = Field(None, description="플랜 타입")
    price: Optional[float] = Field(None, ge=0, description="가격")
    billing_cycle: Optional[BillingCycle] = Field(None, description="결제 주기")
    features: Optional[Dict[str, Any]] = Field(None, description="기능 목록")
    max_clubs: Optional[int] = Field(None, ge=1, description="최대 클럽 수")
    max_members_per_club: Optional[int] = Field(None, ge=1, description="클럽당 최대 멤버 수")
    max_meetings_per_month: Optional[int] = Field(None, ge=1, description="월 최대 모임 수")

class PlanResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    type: str
    price: float
    billing_cycle: str
    features: Optional[Dict[str, Any]]
    max_clubs: Optional[int]
    max_members_per_club: Optional[int]
    max_meetings_per_month: Optional[int]
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    model_config = {"from_attributes": True}

# 결제 수단 관련 스키마
class PaymentMethodCreate(BaseModel):
    method_type: PaymentMethod = Field(..., description="결제 수단 타입")
    method_name: str = Field(..., min_length=1, max_length=255, description="결제 수단 이름")
    masked_info: Optional[str] = Field(None, max_length=255, description="마스킹된 정보")
    is_default: bool = Field(default=False, description="기본 결제 수단 여부")

class PaymentMethodUpdate(BaseModel):
    method_type: Optional[PaymentMethod] = Field(None, description="결제 수단 타입")
    method_name: Optional[str] = Field(None, min_length=1, max_length=255, description="결제 수단 이름")
    masked_info: Optional[str] = Field(None, max_length=255, description="마스킹된 정보")
    is_default: Optional[bool] = Field(None, description="기본 결제 수단 여부")

class PaymentMethodResponse(BaseModel):
    id: int
    user_id: int
    method_type: str
    method_name: str
    masked_info: Optional[str]
    is_default: bool
    status: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    model_config = {"from_attributes": True}

# 구독 관련 스키마
class SubscriptionCreate(BaseModel):
    plan_id: int = Field(..., description="플랜 ID")
    start_date: Optional[datetime] = Field(None, description="시작 날짜")

class SubscriptionUpdate(BaseModel):
    plan_id: Optional[int] = Field(None, description="플랜 ID")
    status: Optional[SubscriptionStatus] = Field(None, description="구독 상태")
    end_date: Optional[datetime] = Field(None, description="종료 날짜")

class SubscriptionResponse(BaseModel):
    id: int
    user_id: int
    plan_id: int
    plan_name: str
    status: str
    start_date: datetime
    next_billing_date: Optional[datetime]
    end_date: Optional[datetime]
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    model_config = {"from_attributes": True}

# 결제 관련 스키마
class PaymentResponse(BaseModel):
    payment_key: str
    order_id: str
    order_name: str
    status: str
    requested_at: str
    approved_at: Optional[str]
    total_amount: int
    method: str
    card: Optional[dict]
    virtual_account: Optional[dict]
    
    model_config = {"from_attributes": True}

