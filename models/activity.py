# 회원 활동 로그 모델 (backend #50, backoffice docs/03.구현문서/데이터베이스설계서.md 4.2)
from sqlalchemy import JSON, BigInteger, Column, DateTime, ForeignKey, Index, Integer, String

from database import Base

_BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


class UserActivityLog(Base):
    """회원 행동 이력. 1년 보관 (utils/activity.purge_expired)"""

    __tablename__ = "user_activity_logs"

    id = Column(_BIGINT_PK, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String(40), nullable=False)
    target_type = Column(String(30), nullable=True)
    target_id = Column(Integer, nullable=True)
    summary = Column(String(255), nullable=False, comment="표시용 요약. 대상이 삭제돼도 의미 유지")
    meta_json = Column(JSON, nullable=True, comment="device, app_version, provider 등")
    ip = Column(String(45), nullable=True)
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_act_user_created", "user_id", "created_at"),
        Index("ix_act_created", "created_at"),
        Index("ix_act_type_created", "event_type", "created_at"),
    )
