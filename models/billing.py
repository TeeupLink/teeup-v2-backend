# 정기결제(빌링) 모델 (backend #51, backoffice docs/03.구현문서/데이터베이스설계서.md 4.4~4.7)
#
# 금액은 전부 원 단위 정수. 상태는 문자열 컬럼에 아래 상수만 쓴다 (기존 payments·subscriptions 와 같은 방식).
from sqlalchemy import JSON, Boolean, Column, Date, DateTime, ForeignKey, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.sql import func

from database import Base

# billing_keys.status
KEY_ACTIVE = "ACTIVE"
KEY_REVOKED = "REVOKED"

# billing_charges.status
CHARGE_PENDING = "PENDING"
CHARGE_SUCCEEDED = "SUCCEEDED"
CHARGE_FAILED = "FAILED"
CHARGE_GAVE_UP = "GAVE_UP"


class BillingKey(Base):
    """토스 빌링키. 원문은 AES-GCM 으로 암호화해 둔다 (services/billing/crypto.py)"""
    __tablename__ = "billing_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_key = Column(String(64), nullable=False, index=True, comment="토스 customerKey (회원별 고정)")
    billing_key_enc = Column(LargeBinary(512), nullable=False, comment="암호화한 빌링키")
    card_company = Column(String(30))
    card_number_masked = Column(String(30))
    status = Column(String(20), nullable=False, default=KEY_ACTIVE)
    mode = Column(String(10), nullable=False, comment="TEST·LIVE (발급한 키 종류)")
    created_at = Column(DateTime, default=func.now(), nullable=False)
    revoked_at = Column(DateTime)


class BillingCharge(Base):
    """구독 1회 청구. 같은 기간은 한 행이고, 재시도는 attempt 를 올려 같은 행을 쓴다"""
    __tablename__ = "billing_charges"
    __table_args__ = (UniqueConstraint("subscription_id", "period_start", name="uq_billing_charge_period"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False)
    billing_key_id = Column(Integer, ForeignKey("billing_keys.id"), nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    amount = Column(Integer, nullable=False)
    order_id = Column(String(64), nullable=False, unique=True, comment="sub{구독id}_{YYYYMMDD}. 토스에는 _a{시도} 를 붙여 보낸다")
    idempotency_key = Column(String(64), nullable=False, unique=True, comment="시도마다 새 값 (order_id:시도)")
    attempt = Column(Integer, nullable=False, default=1)
    status = Column(String(20), nullable=False, default=CHARGE_PENDING, index=True)
    failure_code = Column(String(100))
    failure_message = Column(String(500))
    next_retry_at = Column(DateTime, index=True)
    payment_id = Column(Integer, ForeignKey("payments.id"))
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class PaymentCancel(Base):
    """결제 취소·부분 환불 이력. 잔액 = payments.amount - SUM(cancel_amount)"""
    __tablename__ = "payment_cancels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    payment_id = Column(Integer, ForeignKey("payments.id", ondelete="CASCADE"), nullable=False, index=True)
    cancel_amount = Column(Integer, nullable=False)
    cancel_reason = Column(String(200), nullable=False)
    transaction_key = Column(String(64))
    canceled_by_admin_id = Column(Integer, ForeignKey("admins.id"))
    created_at = Column(DateTime, default=func.now(), nullable=False)


class BillingWebhookEvent(Base):
    """토스 웹훅 수신 원문. verified 는 토스 조회 API 로 다시 확인한 결과"""
    __tablename__ = "billing_webhook_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(50))
    payment_key = Column(String(255), index=True)
    order_id = Column(String(255))
    payload_json = Column(JSON)
    verified = Column(Boolean, default=False, nullable=False)
    processed_at = Column(DateTime)
    created_at = Column(DateTime, default=func.now(), nullable=False)
