# 신고 / 차단 스키마
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field
from .enums import ReportTargetType, ReportReason, ReportStatus


class ContentReportCreate(BaseModel):
    target_type: ReportTargetType
    target_id: int = Field(..., ge=1)
    reason: ReportReason
    description: Optional[str] = Field(None, max_length=2000)


class ContentReportResponse(BaseModel):
    id: int
    reporter_id: int
    target_type: ReportTargetType
    target_id: int
    reason: ReportReason
    description: Optional[str] = None
    status: ReportStatus
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class BlockedUserItem(BaseModel):
    user_id: int
    nickname: Optional[str] = None
    profile_image: Optional[str] = None
    blocked_at: datetime


class BlockedUsersResponse(BaseModel):
    blocked_user_ids: List[int]
    blocked_users: List[BlockedUserItem]
