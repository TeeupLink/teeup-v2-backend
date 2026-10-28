# 관리자 감사 로그 모델 (backend #49, backoffice docs/03.구현문서/데이터베이스설계서.md 4.1·4.8)
from sqlalchemy import JSON, BigInteger, Column, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text, event
from sqlalchemy.sql import func

from database import Base

# SQLite(테스트)는 BIGINT PK 에 자동 증가를 붙이지 못한다
_BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


class AdminAuditLog(Base):
    """INSERT 전용. 수정·삭제는 ORM 이벤트에서 막는다 (위·변조 방지)"""

    __tablename__ = "admin_audit_logs"

    id = Column(_BIGINT_PK, primary_key=True, autoincrement=True)
    request_id = Column(String(36), nullable=False, comment="요청 단위 UUID. 일괄 처리 건별 행을 묶음")
    actor_admin_id = Column(Integer, ForeignKey("admins.id", ondelete="SET NULL"), nullable=True)
    actor_email = Column(String(255), nullable=False, comment="삭제된 관리자도 식별되도록 스냅샷")
    actor_role = Column(String(30), nullable=True)
    action = Column(String(30), nullable=False)
    target_type = Column(String(40), nullable=True)
    target_id = Column(String(64), nullable=True)
    before_json = Column(JSON, nullable=True)
    after_json = Column(JSON, nullable=True)
    reason = Column(String(500), nullable=True)
    meta_json = Column(JSON, nullable=True)
    method = Column(String(10), nullable=False)
    path = Column(String(255), nullable=False)
    status_code = Column(SmallInteger, nullable=False)
    result = Column(String(10), nullable=False, comment="SUCCESS·FAIL")
    ip = Column(String(45), nullable=False)
    user_agent = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=False), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_audit_created", "created_at"),
        Index("ix_audit_actor_created", "actor_admin_id", "created_at"),
        Index("ix_audit_target", "target_type", "target_id"),
        Index("ix_audit_action_created", "action", "created_at"),
        Index("ix_audit_request", "request_id"),
    )


class AuditMonthlyReview(Base):
    """월간 접속기록 점검 완료 기록"""

    __tablename__ = "audit_monthly_reviews"

    id = Column(Integer, primary_key=True, autoincrement=True)
    month = Column(String(7), nullable=False, unique=True, comment="YYYY-MM")
    reviewer_admin_id = Column(Integer, ForeignKey("admins.id"), nullable=False)
    summary_json = Column(JSON, nullable=True, comment="점검 시점 집계 스냅샷")
    memo = Column(Text, nullable=True)
    reviewed_at = Column(DateTime, nullable=False, server_default=func.now())


class AuditLogImmutableError(RuntimeError):
    pass


@event.listens_for(AdminAuditLog, "before_update")
def _block_update(mapper, connection, target):
    raise AuditLogImmutableError("감사 로그는 수정할 수 없습니다")


@event.listens_for(AdminAuditLog, "before_delete")
def _block_delete(mapper, connection, target):
    raise AuditLogImmutableError("감사 로그는 삭제할 수 없습니다")
