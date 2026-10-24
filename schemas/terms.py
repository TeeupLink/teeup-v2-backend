# 약관 관련 스키마
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from .enums import TermsType
# 약관 관련 스키마
class TermsCreate(BaseModel):
    type: TermsType = Field(..., description="약관 타입")
    title: str = Field(..., min_length=1, max_length=255, description="약관 제목")
    content: str = Field(..., min_length=1, description="약관 내용")
    is_active: bool = Field(default=True, description="활성화 여부")

class TermsUpdate(BaseModel):
    type: Optional[TermsType] = Field(None, description="약관 타입")
    title: Optional[str] = Field(None, min_length=1, max_length=255, description="약관 제목")
    content: Optional[str] = Field(None, min_length=1, description="약관 내용")
    is_active: Optional[bool] = Field(None, description="활성화 여부")

class TermsResponse(BaseModel):
    id: int
    type: str
    title: str
    content: str
    is_active: bool
    is_required: bool
    published_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    model_config = {"from_attributes": True}

class TermsListResponse(BaseModel):
    terms: List[TermsResponse]
    total: int
    page: int
    size: int
    total_pages: int

class TermsAgreementCreate(BaseModel):
    terms_id: int = Field(..., description="약관 ID")
    agreed_at: datetime = Field(..., description="동의 시간")
    ip_address: Optional[str] = Field(None, description="IP 주소")
    user_agent: Optional[str] = Field(None, description="사용자 에이전트")

class TermsAgreementResponse(BaseModel):
    id: int
    user_id: int
    terms_id: int
    terms_title: str
    agreed_at: datetime
    ip_address: Optional[str]
    user_agent: Optional[str]
    created_at: Optional[datetime] = None
    
    model_config = {"from_attributes": True}

class TermsAgreementBulkCreate(BaseModel):
    """여러 약관을 한 번에 동의"""
    terms_ids: List[int] = Field(..., description="약관 ID 목록")
    agreed_at: datetime = Field(default_factory=datetime.now, description="동의 시간")
    ip_address: Optional[str] = Field(None, description="IP 주소")
    user_agent: Optional[str] = Field(None, description="사용자 에이전트")

class TermsAgreementBulkResponse(BaseModel):
    """여러 약관 동의 응답"""
    agreements: List[TermsAgreementResponse]
    all_required_agreed: bool = Field(..., description="모든 필수 약관 동의 완료 여부")

