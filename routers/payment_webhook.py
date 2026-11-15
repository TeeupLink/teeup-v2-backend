"""
토스 웹훅 (backend #51, backoffice API스펙 API-133). 인증 없음 — 본문을 믿지 않는다.

수신 원문을 남기고, paymentKey 로 토스 조회 API 를 다시 불러 그 결과로만 상태를 바꾼다.
같은 이벤트를 여러 번 받아도 매번 토스 조회 결과를 쓰므로 결과가 같다. 우리 결제가 아니면 건너뛴다.
토스는 200 이 아니면 다시 보내므로 처리하지 않는 경우에도 200 을 돌려준다.
"""
import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from database import get_db
from models import BillingWebhookEvent, Payment
from services.billing import toss
from utils.datetime_utils import get_kst_now

logger = logging.getLogger(__name__)

router = APIRouter(tags=["payments-webhook"])

# 토스 결제 상태 → payments.status. 부분 취소는 관리자 취소 이력으로 관리하므로 바꾸지 않는다
_STATUS = {"DONE": "SUCCEEDED", "CANCELED": "CANCELED", "ABORTED": "FAILED", "EXPIRED": "FAILED"}


def _now():
    return get_kst_now().replace(tzinfo=None)


@router.post("/webhook")
async def toss_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False}
    if not isinstance(payload, dict):
        return {"ok": False}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    payment_key = data.get("paymentKey")
    event = BillingWebhookEvent(event_type=str(payload.get("eventType") or "")[:50], payment_key=payment_key,
                                order_id=data.get("orderId"), payload_json=payload)
    db.add(event)
    db.commit()

    if event.event_type != "PAYMENT_STATUS_CHANGED" or not payment_key:
        return {"ok": True}
    payment = db.query(Payment).filter(Payment.payment_key == payment_key).first()
    if payment is None:
        event.processed_at = _now()
        db.commit()
        return {"ok": True}
    try:
        actual = toss.client.get_payment(payment_key)
    except toss.TossError as e:
        logger.warning(f"웹훅 재확인 실패 payment={payment.id}: {e.code}")
        return {"ok": True}
    event.verified = actual.get("status") == data.get("status")
    new_status = _STATUS.get(actual.get("status"))
    if new_status and payment.status != new_status:
        logger.info(f"웹훅으로 결제 상태 변경 payment={payment.id} {payment.status} -> {new_status}")
        payment.status = new_status
    event.processed_at = _now()
    db.commit()
    return {"ok": True}
