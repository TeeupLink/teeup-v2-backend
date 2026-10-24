# 문의 관련 스키마
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from .enums import InquiryType, InquiryStatus
# 문의 관련 스키마
class InquiryCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="문의 제목")
    content: str = Field(..., min_length=1, description="문의 내용")
    type: InquiryType = Field(..., description="문의 타입")

class InquiryUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255, description="문의 제목")
    content: Optional[str] = Field(None, min_length=1, description="문의 내용")
    type: Optional[InquiryType] = Field(None, description="문의 타입")
    status: Optional[InquiryStatus] = Field(None, description="문의 상태")

class InquiryResponse(BaseModel):
    id: int
    user_id: int
    user_name: str
    user_nickname: str
    title: str
    content: str
    type: str
    status: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    model_config = {"from_attributes": True}

class InquiryListResponse(BaseModel):
    inquiries: List[InquiryResponse]
    total: int
    page: int
    size: int
    total_pages: int

class InquiryResponseCreate(BaseModel):
    inquiry_id: int = Field(..., description="문의 ID")
    content: str = Field(..., min_length=1, description="답변 내용")
    is_internal: bool = Field(default=False, description="내부 메모 여부")

class InquiryResponseUpdate(BaseModel):
    content: Optional[str] = Field(None, min_length=1, description="답변 내용")
    is_internal: Optional[bool] = Field(None, description="내부 메모 여부")

class InquiryResponseResponse(BaseModel):
    id: int
    inquiry_id: int
    admin_id: int
    admin_name: str
    content: str
    is_internal: bool
    created_at: Optional[datetime] = None
    
    model_config = {"from_attributes": True}

class InquiryDetailResponse(BaseModel):
    inquiry: InquiryResponse
    responses: List[InquiryResponseResponse]
    total_responses: int

