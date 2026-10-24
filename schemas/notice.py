# 공지사항 관련 스키마
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from .enums import NoticeType
# 공지사항 관련 스키마
class NoticeCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="공지사항 제목")
    content: str = Field(..., min_length=1, description="공지사항 내용")
    type: NoticeType = Field(..., description="공지사항 타입")
    is_important: bool = Field(default=False, description="중요 공지 여부")
    is_published: bool = Field(default=False, description="발행 여부")
    attachment_file: Optional[str] = Field(None, description="첨부 파일명")
    web_view_link: Optional[str] = Field(None, description="Google Drive 웹 뷰 링크")

class NoticeUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255, description="공지사항 제목")
    content: Optional[str] = Field(None, min_length=1, description="공지사항 내용")
    type: Optional[NoticeType] = Field(None, description="공지사항 타입")
    is_important: Optional[bool] = Field(None, description="중요 공지 여부")
    is_published: Optional[bool] = Field(None, description="발행 여부")
    attachment_file: Optional[str] = Field(None, description="첨부 파일명")
    web_view_link: Optional[str] = Field(None, description="Google Drive 웹 뷰 링크")

class NoticeResponse(BaseModel):
    id: int
    title: str
    content: str
    type: str
    is_important: bool
    is_published: bool
    view_count: int
    published_at: Optional[datetime]
    attachment_file: Optional[str] = None
    web_view_link: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    model_config = {"from_attributes": True}

class NoticeListResponse(BaseModel):
    notices: List[NoticeResponse]
    total: int
    page: int
    size: int
    total_pages: int

