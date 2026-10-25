# 신고 / 차단 스키마
from datetime import datetime
from pydantic import BaseModel, Field
from .enums import ReportTargetType, ReportReason, ReportStatus


class ContentReportCreate(BaseModel):
    target_type: ReportTargetType
    target_id: int = Field(..., ge=1)
    reason: ReportReason
    description: str | None = Field(None, max_length=2000)


class ContentReportResponse(BaseModel):
    id: int
    reporter_id: int
    target_type: ReportTargetType
    target_id: int
    reason: ReportReason
    description: str | None = None
    status: ReportStatus
    created_at: datetime | None = None

    class Config:
        from_attributes = True


class BlockedUserItem(BaseModel):
    user_id: int
    nickname: str | None = None
    profile_image: str | None = None
    blocked_at: datetime | None = None


class BlockedUsersResponse(BaseModel):
    blocked_user_ids: list[int]
    blocked_users: list[BlockedUserItem]
