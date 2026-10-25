# 약관 관련 스키마
from pydantic import BaseModel, Field
from datetime import datetime
from .enums import TermsType
# 약관 관련 스키마
class TermsCreate(BaseModel):
    type: TermsType = Field(..., description="약관 타입")
    title: str = Field(..., min_length=1, max_length=255, description="약관 제목")
    content: str = Field(..., min_length=1, description="약관 내용")
    is_active: bool = Field(default=True, description="활성화 여부")

class TermsUpdate(BaseModel):
    type: TermsType | None = Field(None, description="약관 타입")
    title: str | None = Field(None, min_length=1, max_length=255, description="약관 제목")
    content: str | None = Field(None, min_length=1, description="약관 내용")
    is_active: bool | None = Field(None, description="활성화 여부")

class TermsResponse(BaseModel):
    id: int
    type: str
    title: str
    content: str
    is_active: bool
    is_required: bool
    published_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    
    model_config = {"from_attributes": True}

class TermsListResponse(BaseModel):
    terms: list[TermsResponse]
    total: int
    page: int
    size: int
    total_pages: int

class TermsAgreementCreate(BaseModel):
    terms_id: int = Field(..., description="약관 ID")
    agreed_at: datetime = Field(..., description="동의 시간")
    ip_address: str | None = Field(None, description="IP 주소")
    user_agent: str | None = Field(None, description="사용자 에이전트")

class TermsAgreementResponse(BaseModel):
    id: int
    user_id: int
    terms_id: int
    terms_title: str
    agreed_at: datetime
    ip_address: str | None
    user_agent: str | None
    created_at: datetime | None = None
    
    model_config = {"from_attributes": True}

class TermsAgreementBulkCreate(BaseModel):
    """여러 약관을 한 번에 동의"""
    terms_ids: list[int] = Field(..., description="약관 ID 목록")
    agreed_at: datetime = Field(default_factory=datetime.now, description="동의 시간")
    ip_address: str | None = Field(None, description="IP 주소")
    user_agent: str | None = Field(None, description="사용자 에이전트")

class TermsAgreementBulkResponse(BaseModel):
    """여러 약관 동의 응답"""
    agreements: list[TermsAgreementResponse]
    all_required_agreed: bool = Field(..., description="모든 필수 약관 동의 완료 여부")

