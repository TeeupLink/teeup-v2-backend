"""결제 전액 취소·부분 환불 (API-122). 잔액 = amount - SUM(payment_cancels.cancel_amount)"""
from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Payment, PaymentCancel

from . import toss

CANCELABLE = {"SUCCEEDED"}


def canceled_total(db: Session, payment_id: int) -> int:
    return int(db.query(func.coalesce(func.sum(PaymentCancel.cancel_amount), 0)).filter(PaymentCancel.payment_id == payment_id).scalar())


def balance(db: Session, payment: Payment) -> int:
    return int(payment.amount) - canceled_total(db, payment.id)


def display_status(payment: Payment, canceled: int) -> str:
    """화면 상태: 완료·부분취소·취소·실패·대기 (저장값 + 취소 합계)"""
    status = payment.status.value if hasattr(payment.status, "value") else payment.status
    if status == "SUCCEEDED" and canceled > 0:
        return "CANCELED" if canceled >= payment.amount else "PARTIAL_CANCELED"
    return status


def cancel_payment(db: Session, payment: Payment, *, amount: int | None, reason: str, admin_id: int | None) -> PaymentCancel:
    """토스 취소 후 이력을 남긴다. 검증 실패는 ValueError, 토스 실패는 TossError"""
    status = payment.status.value if hasattr(payment.status, "value") else payment.status
    if status not in CANCELABLE:
        raise ValueError("취소할 수 있는 결제가 아닙니다")
    left = balance(db, payment)
    if left <= 0:
        raise ValueError("이미 전액 취소된 결제입니다")
    amount = left if amount is None else amount
    if amount <= 0 or amount > left:
        raise ValueError(f"취소 금액은 1원 이상, 잔액 {left:,}원 이하여야 합니다")
    if not payment.payment_key:
        raise ValueError("토스 결제 키가 없어 취소할 수 없습니다")

    n = db.query(PaymentCancel).filter(PaymentCancel.payment_id == payment.id).count() + 1
    res = toss.client.cancel(payment.payment_key, reason=reason, amount=amount, idempotency_key=f"cancel_{payment.id}_{n}")
    cancels = res.get("cancels") or []
    row = PaymentCancel(
        payment_id=payment.id, cancel_amount=amount, cancel_reason=reason[:200],
        transaction_key=(cancels[-1].get("transactionKey") if cancels else None), canceled_by_admin_id=admin_id,
    )
    db.add(row)
    if amount == left:
        payment.status = "CANCELED"
    db.commit()
    return row
