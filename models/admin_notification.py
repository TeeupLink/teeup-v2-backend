# 관리자 알림 (backoffice F-016 헤더 알림 벨). 회원 알림(notifications, users FK)과 분리한다.
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.sql import func

from database import Base


class AdminNotification(Base):
    """admin_id 는 admins.id — 회원 번호(users.id)와 섞지 않는다"""

    __tablename__ = "admin_notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    admin_id = Column(Integer, ForeignKey("admins.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(40), nullable=False, comment="JOIN_REQUEST, INQUIRY, REPORT, BILLING_FAILURE 등")
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=True)
    link = Column(String(255), nullable=True, comment="백오피스 경로 (예: /inquiries/12)")
    read_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now())

    __table_args__ = (Index("ix_admin_noti_admin_created", "admin_id", "created_at"),)
