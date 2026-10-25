# 비용 정산 관련 스키마
from pydantic import BaseModel, Field
from datetime import datetime
# 비용 정산 관련 스키마
class ExpenseCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="비용 제목")
    description: str | None = Field(None, description="비용 설명")
    amount: float = Field(..., ge=0, description="총 비용")
    total_participants: int = Field(..., ge=1, description="참가자 수")
    participant_ids: list[int] | None = Field(default_factory=list, description="참가자(MeetingParticipant) ID 목록")
    extra_payer_id: int | None = Field(None, description="나머지 10원 부담자(participant_ids 중 하나)")
    notes: str | None = Field(None, description="메모")

class ExpenseUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255, description="비용 제목")
    description: str | None = Field(None, description="비용 설명")
    amount: float | None = Field(None, ge=0, description="총 비용")
    total_participants: int | None = Field(None, ge=1, description="참가자 수")
    extra_payer_id: int | None = Field(None, description="나머지 10원 부담자(user_id 또는 guest_id)")
    notes: str | None = Field(None, description="메모")

class ExpenseResponse(BaseModel):
    id: int
    title: str
    description: str | None
    amount: float
    total_participants: int
    amount_per_person: float
    meeting_id: int
    club_id: int
    created_by: int
    creator_name: str
    notes: str | None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    participants: list["ExpenseParticipantResponse"] | None = None
    settlement_type: str | None = None

    model_config = {"from_attributes": True}

class ExpenseListResponse(BaseModel):
    expenses: list[ExpenseResponse]
    total: int
    page: int
    limit: int
    total_pages: int

class ExpenseParticipantResponse(BaseModel):
    id: int
    expense_id: int
    user_id: int | None = None
    guest_id: int | None = None
    user_name: str
    user_nickname: str
    is_guest: bool = False
    amount_paid: float | None = None
    is_paid: bool
    paid_at: datetime | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}

class ExpenseParticipantUpdate(BaseModel):
    amount_paid: float | None = Field(None, ge=0, description="지불 금액")
    is_paid: bool | None = Field(None, description="지불 여부")


ExpenseResponse.model_rebuild()  # resolve forward ref for participants

