# 비용 정산 관련 스키마
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
# 비용 정산 관련 스키마
class ExpenseCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="비용 제목")
    description: Optional[str] = Field(None, description="비용 설명")
    amount: float = Field(..., ge=0, description="총 비용")
    total_participants: int = Field(..., ge=1, description="참가자 수")
    participant_ids: Optional[List[int]] = Field(default_factory=list, description="참가자(MeetingParticipant) ID 목록")
    extra_payer_id: Optional[int] = Field(None, description="나머지 10원 부담자(participant_ids 중 하나)")
    notes: Optional[str] = Field(None, description="메모")

class ExpenseUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255, description="비용 제목")
    description: Optional[str] = Field(None, description="비용 설명")
    amount: Optional[float] = Field(None, ge=0, description="총 비용")
    total_participants: Optional[int] = Field(None, ge=1, description="참가자 수")
    extra_payer_id: Optional[int] = Field(None, description="나머지 10원 부담자(user_id 또는 guest_id)")
    notes: Optional[str] = Field(None, description="메모")

class ExpenseResponse(BaseModel):
    id: int
    title: str
    description: Optional[str]
    amount: float
    total_participants: int
    amount_per_person: float
    meeting_id: int
    club_id: int
    created_by: int
    creator_name: str
    notes: Optional[str]
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    participants: Optional[List["ExpenseParticipantResponse"]] = None
    settlement_type: Optional[str] = None

    model_config = {"from_attributes": True}

class ExpenseListResponse(BaseModel):
    expenses: List[ExpenseResponse]
    total: int
    page: int
    limit: int
    total_pages: int

class ExpenseParticipantResponse(BaseModel):
    id: int
    expense_id: int
    user_id: Optional[int] = None
    guest_id: Optional[int] = None
    user_name: str
    user_nickname: str
    is_guest: bool = False
    amount_paid: Optional[float] = None
    is_paid: bool
    paid_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}

class ExpenseParticipantUpdate(BaseModel):
    amount_paid: Optional[float] = Field(None, ge=0, description="지불 금액")
    is_paid: Optional[bool] = Field(None, description="지불 여부")


ExpenseResponse.model_rebuild()  # resolve forward ref for participants

