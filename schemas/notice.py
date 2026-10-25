# 공지사항 관련 스키마
from pydantic import BaseModel, Field
from datetime import datetime
from .enums import NoticeType
# 공지사항 관련 스키마
class NoticeCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="공지사항 제목")
    content: str = Field(..., min_length=1, description="공지사항 내용")
    type: NoticeType = Field(..., description="공지사항 타입")
    is_important: bool = Field(default=False, description="중요 공지 여부")
    is_published: bool = Field(default=False, description="발행 여부")
    attachment_file: str | None = Field(None, description="첨부 파일명")
    web_view_link: str | None = Field(None, description="Google Drive 웹 뷰 링크")

class NoticeUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255, description="공지사항 제목")
    content: str | None = Field(None, min_length=1, description="공지사항 내용")
    type: NoticeType | None = Field(None, description="공지사항 타입")
    is_important: bool | None = Field(None, description="중요 공지 여부")
    is_published: bool | None = Field(None, description="발행 여부")
    attachment_file: str | None = Field(None, description="첨부 파일명")
    web_view_link: str | None = Field(None, description="Google Drive 웹 뷰 링크")

class NoticeResponse(BaseModel):
    id: int
    title: str
    content: str
    type: str
    is_important: bool
    is_published: bool
    view_count: int
    published_at: datetime | None
    attachment_file: str | None = None
    web_view_link: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    
    model_config = {"from_attributes": True}

class NoticeListResponse(BaseModel):
    notices: list[NoticeResponse]
    total: int
    page: int
    size: int
    total_pages: int

