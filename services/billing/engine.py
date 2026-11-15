"""정기 청구 엔진 (backend #51, backoffice API스펙 API-128~130).

- 청구 1건 = billing_charges 1행 (구독·기간마다 하나). 시도할 때마다 attempt 를 올리고 새 멱등키를 쓴다
- 실패하면 PAST_DUE 로 두고 1·3·7일 뒤 재시도, 마지막 시도까지 실패하면 GAVE_UP + 관리자 알림
- 응답을 못 받은 시도(NETWORK_ERROR, 오래 걸린 PENDING)는 토스가 처리했을 수 있으므로
  다음 시도에서 같은 멱등키를 다시 쓴다 → 토스가 이중 청구 대신 처음 결과를 돌려준다
- BILLING_ENABLED=false 면 아무것도 청구하지 않는다 (run_due 는 바로 돌아가고 retry 는 BillingDisabled)
"""
import calendar
import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from config import settings
from models import BillingCharge, BillingKey, Payment, Plan, Subscription
from models.billing import CHARGE_FAILED, CHARGE_GAVE_UP, CHARGE_PENDING, CHARGE_SUCCEEDED, KEY_ACTIVE
from utils import admin_notify
from utils.datetime_utils import get_kst_now

from . import crypto, toss

logger = logging.getLogger(__name__)

RETRY_DAYS = (1, 3, 7)
MAX_ATTEMPT = len(RETRY_DAYS) + 1
STALE_PENDING = timedelta(hours=1)
_CYCLE_MONTHS = {"MONTHLY": 1, "QUARTERLY": 3, "YEARLY": 12}
_UNKNOWN_OUTCOME = "NETWORK_ERROR"


class BillingDisabled(Exception):
    pass


def enabled() -> bool:
    return bool(settings.BILLING_ENABLED)


def _now() -> datetime:
    return get_kst_now().replace(tzinfo=None)


def _v(x):
    return x.value if hasattr(x, "value") else x


def add_cycle(d: date, cycle: str) -> date:
    months = _CYCLE_MONTHS.get(_v(cycle))
    if not months:
        raise ValueError(f"정기결제할 수 없는 주기입니다: {_v(cycle)}")
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def active_key(db: Session, sub: Subscription) -> BillingKey | None:
    if not sub.billing_key_id:
        return None
    return db.query(BillingKey).filter(BillingKey.id == sub.billing_key_id, BillingKey.status == KEY_ACTIVE).first()


def open_charge(db: Session, sub: Subscription, key: BillingKey) -> BillingCharge:
    """다음 결제일부터 한 주기 청구 행. 이미 있으면 그대로 돌려준다 (같은 기간 이중 청구 방지)"""
    plan = db.get(Plan, sub.plan_id)
    start = sub.next_billing_date.date()
    existing = db.query(BillingCharge).filter(BillingCharge.subscription_id == sub.id, BillingCharge.period_start == start).first()
    if existing:
        return existing
    order_id = f"sub{sub.id}_{start:%Y%m%d}"
    charge = BillingCharge(
        subscription_id=sub.id, billing_key_id=key.id, period_start=start, period_end=add_cycle(start, plan.billing_cycle),
        amount=int(plan.price), order_id=order_id, idempotency_key=f"{order_id}:0", attempt=0, status=CHARGE_PENDING,
    )
    db.add(charge)
    db.commit()
    return charge


def _approved_at(res: dict) -> datetime | None:
    raw = res.get("approvedAt")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except ValueError:
        return None


def attempt(db: Session, charge: BillingCharge, now: datetime | None = None) -> BillingCharge:
    """한 번 청구한다. 결과는 charge.status 로 본다 (토스 실패는 예외가 아니라 FAILED/GAVE_UP)"""
    now = now or _now()
    if charge.status == CHARGE_SUCCEEDED:
        raise ValueError("이미 결제된 청구입니다")
    sub = db.get(Subscription, charge.subscription_id)
    if _v(sub.status) == "CANCELED":
        raise ValueError("해지된 구독입니다")
    plan = db.get(Plan, sub.plan_id)
    key = db.get(BillingKey, charge.billing_key_id)
    if key is None or key.status != KEY_ACTIVE:
        raise ValueError("사용할 수 있는 결제 수단이 없습니다")

    reuse = charge.attempt > 0 and (charge.status == CHARGE_PENDING or charge.failure_code == _UNKNOWN_OUTCOME)
    if not reuse:
        charge.attempt += 1
        charge.idempotency_key = f"{charge.order_id}:{charge.attempt}"
    was_gave_up = charge.status == CHARGE_GAVE_UP
    charge.status = CHARGE_PENDING
    db.commit()

    toss_order_id = f"{charge.order_id}_a{charge.attempt}"
    order_name = f"티업링크 {plan.name}"
    try:
        res = toss.client.charge(
            crypto.decrypt(key.billing_key_enc), customer_key=key.customer_key, amount=charge.amount,
            order_id=toss_order_id, order_name=order_name, idempotency_key=charge.idempotency_key,
        )
    except toss.TossError as e:
        _fail(db, charge, sub, plan, e.code, e.message, was_gave_up, now)
        return charge

    payment = Payment(
        payment_key=res.get("paymentKey"), order_id=toss_order_id, order_name=order_name, amount=int(res.get("totalAmount", charge.amount)),
        status="SUCCEEDED", method=res.get("method") or "CARD", customer_key=key.customer_key, user_id=sub.user_id,
        requested_at=now, approved_at=_approved_at(res) or now,
    )
    db.add(payment)
    db.flush()
    charge.payment_id = payment.id
    charge.status = CHARGE_SUCCEEDED
    charge.failure_code = charge.failure_message = charge.next_retry_at = None
    sub.status = "ACTIVE"
    sub.next_billing_date = datetime.combine(charge.period_end, time())
    db.commit()
    logger.info(f"정기결제 성공 subscription={sub.id} charge={charge.id} attempt={charge.attempt}")
    return charge


def _fail(db: Session, charge: BillingCharge, sub: Subscription, plan: Plan, code: str, message: str, was_gave_up: bool,
          now: datetime) -> None:
    charge.failure_code = code[:100]
    charge.failure_message = message[:500]
    sub.status = "PAST_DUE"
    if charge.attempt >= MAX_ATTEMPT:
        charge.status = CHARGE_GAVE_UP
        charge.next_retry_at = None
    else:
        charge.status = CHARGE_FAILED
        charge.next_retry_at = now + timedelta(days=RETRY_DAYS[charge.attempt - 1])
    db.commit()
    logger.warning(f"정기결제 실패 subscription={sub.id} charge={charge.id} attempt={charge.attempt} code={code}")
    if charge.status == CHARGE_GAVE_UP and not was_gave_up:
        admin_notify.notify(
            "BILLING_FAILURE", f"정기결제 최종 실패: {plan.name} {charge.amount:,}원",
            f"{charge.attempt}회 시도 모두 실패했습니다 ({code})", f"/subscriptions/{sub.id}",
        )


def retry(db: Session, charge: BillingCharge) -> BillingCharge:
    """관리자 수동 재청구 (API-130)"""
    if not enabled():
        raise BillingDisabled("정기결제가 비활성 상태입니다")
    if charge.status not in (CHARGE_FAILED, CHARGE_GAVE_UP):
        raise ValueError("실패한 청구만 다시 청구할 수 있습니다")
    return attempt(db, charge)


def run_due(db: Session, now: datetime | None = None) -> dict:
    """하루 한 번: 결제일이 된 구독을 청구하고, 재시도할 때가 된 실패 건을 다시 청구한다"""
    if not enabled():
        return {"skipped": True}
    now = now or _now()
    counts = {"charged": 0, "retried": 0, "succeeded": 0, "failed": 0, "no_key": 0}

    def tally(c: BillingCharge):
        counts["succeeded" if c.status == CHARGE_SUCCEEDED else "failed"] += 1

    end_of_day = datetime.combine(now.date(), time.max)
    due = (db.query(Subscription)
           .filter(Subscription.status == "ACTIVE", Subscription.next_billing_date.isnot(None), Subscription.next_billing_date <= end_of_day)
           .all())
    for sub in due:
        key = active_key(db, sub)
        if key is None:
            counts["no_key"] += 1
            continue
        charge = open_charge(db, sub, key)
        if charge.status == CHARGE_PENDING and charge.attempt == 0:
            counts["charged"] += 1
            tally(attempt(db, charge, now))

    retry_due = db.query(BillingCharge).filter(
        ((BillingCharge.status == CHARGE_FAILED) & (BillingCharge.next_retry_at <= now))
        | ((BillingCharge.status == CHARGE_PENDING) & (BillingCharge.attempt > 0) & (BillingCharge.updated_at <= now - STALE_PENDING))
    ).all()
    for charge in retry_due:
        counts["retried"] += 1
        try:
            tally(attempt(db, charge, now))
        except ValueError as e:
            logger.warning(f"재청구 건너뜀 charge={charge.id}: {e}")
    return counts
