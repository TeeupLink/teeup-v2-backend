# 문의 관련 스키마
from pydantic import BaseModel, Field
from datetime import datetime
from .enums import InquiryType, InquiryStatus
# 문의 관련 스키마
class InquiryCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="문의 제목")
    content: str = Field(..., min_length=1, description="문의 내용")
    type: InquiryType = Field(..., description="문의 타입")

class InquiryUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255, description="문의 제목")
    content: str | None = Field(None, min_length=1, description="문의 내용")
    type: InquiryType | None = Field(None, description="문의 타입")
    status: InquiryStatus | None = Field(None, description="문의 상태")

class InquiryResponse(BaseModel):
    id: int
    user_id: int
    user_name: str
    user_nickname: str
    title: str
    content: str
    type: str
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    
    model_config = {"from_attributes": True}

class InquiryListResponse(BaseModel):
    inquiries: list[InquiryResponse]
    total: int
    page: int
    size: int
    total_pages: int

class InquiryResponseCreate(BaseModel):
    inquiry_id: int = Field(..., description="문의 ID")
    content: str = Field(..., min_length=1, description="답변 내용")
    is_internal: bool = Field(default=False, description="내부 메모 여부")

class InquiryResponseUpdate(BaseModel):
    content: str | None = Field(None, min_length=1, description="답변 내용")
    is_internal: bool | None = Field(None, description="내부 메모 여부")

class InquiryResponseResponse(BaseModel):
    id: int
    inquiry_id: int
    admin_id: int
    admin_name: str
    content: str
    is_internal: bool
    created_at: datetime | None = None
    
    model_config = {"from_attributes": True}

class InquiryDetailResponse(BaseModel):
    inquiry: InquiryResponse
    responses: list[InquiryResponseResponse]
    total_responses: int

