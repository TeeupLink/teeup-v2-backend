# FAQ 관련 스키마
from pydantic import BaseModel, Field
from datetime import datetime
# FAQ 관련 스키마
class FAQCategoryBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=50)
    order: int = 0
    is_active: bool = True

class FAQCategoryCreate(FAQCategoryBase):
    pass

class FAQCategoryUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=50)
    order: int | None = None
    is_active: bool | None = None

class FAQCategoryResponse(FAQCategoryBase):
    id: int
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}

class FAQBase(BaseModel):
    question: str = Field(..., min_length=1, max_length=300, description="질문")
    answer: str = Field(..., description="답변")
    category_id: int | None = Field(None, description="카테고리 ID")
    order: int = Field(default=0, description="정렬 순서")
    is_active: bool = Field(default=True, description="활성 상태")

class FAQCreate(FAQBase):
    """FAQ 생성 스키마"""
    pass

class FAQUpdate(BaseModel):
    """FAQ 수정 스키마"""
    question: str | None = Field(None, min_length=1, max_length=300, description="질문")
    answer: str | None = Field(None, description="답변")
    category_id: int | None = Field(None, description="카테고리 ID")
    order: int | None = Field(None, description="정렬 순서")
    is_active: bool | None = Field(None, description="활성 상태")

class FAQResponse(BaseModel):
    id: int
    question: str
    answer: str
    category_id: int | None = None
    category_title: str | None = None
    order: int
    is_active: bool
    is_deleted: bool
    view_count: int
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None

    model_config = {"from_attributes": True}

class FAQPageResponse(BaseModel):
    items: list[FAQResponse]
    total: int
    page: int
    limit: int
    total_pages: int

