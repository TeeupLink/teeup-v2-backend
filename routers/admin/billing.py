"""
백오피스 구독·정기결제 API (backend #51, backoffice API스펙 API-125~130)

- 구독 해지는 다음 청구를 멈추는 것이다. 이미 결제된 금액 환불은 결제 상세에서 따로 한다
- 수동 재청구는 BILLING_ENABLED=false 면 409
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import get_db
from models import BillingCharge, BillingKey, Payment, Plan, Subscription, User
from models.billing import CHARGE_FAILED, CHARGE_GAVE_UP
from services.billing import engine, toss
from utils import audit
from utils.datetime_utils import get_kst_now

from .deps import get_admin_user
from .pagination import PageParams, apply_sort, paginate

router = APIRouter(tags=["admin-billing"])

SUBSCRIPTION_STATUSES = ("ACTIVE", "PAST_DUE", "CANCELED")


def _iso(d):
    return d.isoformat() if d else None


def _v(x):
    return x.value if hasattr(x, "value") else x


def _key(k: BillingKey | None) -> dict | None:
    if not k:
        return None
    return {"id": k.id, "card_company": k.card_company, "card_number_masked": k.card_number_masked, "status": k.status,
            "mode": k.mode, "created_at": _iso(k.created_at)}


def _sub_row(t) -> dict:
    s, u, p, k = t
    return {
        "id": s.id,
        "status": _v(s.status),
        "user": {"id": u.id, "nickname": u.nickname, "realname": u.realname} if u else None,
        "plan": {"id": p.id, "name": p.name, "price": int(p.price or 0), "billing_cycle": _v(p.billing_cycle)} if p else None,
        "billing_key": _key(k),
        "start_date": _iso(s.start_date),
        "next_billing_date": _iso(s.next_billing_date),
        "end_date": _iso(s.end_date),
        "canceled_at": _iso(s.canceled_at),
        "cancel_reason": s.cancel_reason,
        "created_at": _iso(s.created_at),
    }


def _sub_query(db: Session):
    return (db.query(Subscription, User, Plan, BillingKey)
            .outerjoin(User, User.id == Subscription.user_id)
            .outerjoin(Plan, Plan.id == Subscription.plan_id)
            .outerjoin(BillingKey, BillingKey.id == Subscription.billing_key_id))


@router.get("/subscriptions")
async def list_subscriptions(
    params: PageParams = Depends(),
    status_: list[str] = Query([], alias="status"),
    user_id: int | None = Query(None),
    plan_id: int | None = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """구독 목록 (API-125). q 는 회원 닉네임"""
    if any(s not in SUBSCRIPTION_STATUSES for s in status_):
        raise HTTPException(status_code=422, detail="알 수 없는 구독 상태입니다")
    q = _sub_query(db)
    if status_:
        q = q.filter(Subscription.status.in_(status_))
    if user_id:
        q = q.filter(Subscription.user_id == user_id)
    if plan_id:
        q = q.filter(Subscription.plan_id == plan_id)
    if params.q:
        q = q.filter(User.nickname.contains(params.q))
    q = apply_sort(q, params.sort, {"next_billing_date": Subscription.next_billing_date, "created_at": Subscription.created_at},
                   (Subscription.created_at.desc(), Subscription.id.desc()))
    return paginate(q, params, _sub_row)


def _charge_row(c: BillingCharge) -> dict:
    return {
        "id": c.id, "subscription_id": c.subscription_id, "period_start": _iso(c.period_start), "period_end": _iso(c.period_end),
        "amount": c.amount, "order_id": c.order_id, "attempt": c.attempt, "max_attempt": engine.MAX_ATTEMPT, "status": c.status,
        "failure_code": c.failure_code, "failure_message": c.failure_message, "next_retry_at": _iso(c.next_retry_at),
        "payment_id": c.payment_id, "created_at": _iso(c.created_at), "updated_at": _iso(c.updated_at),
    }


def _get_sub(db: Session, sub_id: int):
    t = _sub_query(db).filter(Subscription.id == sub_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="구독을 찾을 수 없습니다")
    return t


def _sub_detail(db: Session, t) -> dict:
    out = _sub_row(t)
    charges = (db.query(BillingCharge).filter(BillingCharge.subscription_id == t[0].id)
               .order_by(BillingCharge.period_start.desc(), BillingCharge.id.desc()).limit(100).all())
    out["charges"] = [_charge_row(c) for c in charges]
    return out


@router.get("/subscriptions/{sub_id}")
async def get_subscription(sub_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """구독 상세 + 청구 이력 (API-126)"""
    t = _get_sub(db, sub_id)
    audit.record("VIEW_PII", "SUBSCRIPTION", sub_id)
    return _sub_detail(db, t)


class ReasonBody(BaseModel):
    reason: str = Field(..., min_length=1, max_length=200)


@router.post("/subscriptions/{sub_id}/cancel")
async def cancel_subscription(sub_id: int, body: ReasonBody, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """해지 (API-127). 다음 청구를 멈추고, 재시도 대기 중인 실패 청구도 멈춘다. 환불은 하지 않는다"""
    t = _get_sub(db, sub_id)
    s = t[0]
    if _v(s.status) == "CANCELED":
        raise HTTPException(status_code=409, detail="이미 해지된 구독입니다")
    reason = body.reason.strip()
    before = {"status": _v(s.status), "next_billing_date": _iso(s.next_billing_date)}
    now = get_kst_now().replace(tzinfo=None)
    s.status = "CANCELED"
    s.canceled_at = now
    s.cancel_reason = reason
    s.end_date = s.next_billing_date or now
    stopped = (db.query(BillingCharge)
               .filter(BillingCharge.subscription_id == s.id, BillingCharge.status == CHARGE_FAILED).all())
    for c in stopped:
        c.status = CHARGE_GAVE_UP
        c.next_retry_at = None
    db.commit()
    audit.record("STATUS_CHANGE", "SUBSCRIPTION", s.id, before=before, after={"status": "CANCELED", "end_date": _iso(s.end_date)},
                 reason=reason, meta={"stopped_charges": [c.id for c in stopped]})
    return _sub_detail(db, _get_sub(db, sub_id))


# ---- 정기결제 상태·실패 ----------------------------------------------------

@router.get("/billing/status")
async def billing_status(current_user: dict = Depends(get_admin_user)):
    """API-128. mode: test·live·unset (토스 시크릿 키 종류). encryption_ready: 빌링키 암호화 키 설정 여부"""
    from config import settings

    return {"enabled": engine.enabled(), "mode": toss.mode(), "encryption_ready": bool(settings.BILLING_KEY_ENC_KEY),
            "max_attempt": engine.MAX_ATTEMPT, "retry_days": list(engine.RETRY_DAYS)}


@router.get("/billing/failures")
async def list_failures(
    params: PageParams = Depends(),
    status_: list[str] = Query([], alias="status"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """청구 실패 (API-129). 기본은 재시도 대기(FAILED)와 포기(GAVE_UP) 전부, 최근 시도순"""
    wanted = status_ or [CHARGE_FAILED, CHARGE_GAVE_UP]
    if any(s not in (CHARGE_FAILED, CHARGE_GAVE_UP) for s in wanted):
        raise HTTPException(status_code=422, detail="FAILED 또는 GAVE_UP 만 조회할 수 있습니다")
    q = (db.query(BillingCharge, Subscription, User, Plan)
         .join(Subscription, Subscription.id == BillingCharge.subscription_id)
         .outerjoin(User, User.id == Subscription.user_id)
         .outerjoin(Plan, Plan.id == Subscription.plan_id)
         .filter(BillingCharge.status.in_(wanted)))
    if params.q:
        q = q.filter(or_(User.nickname.contains(params.q), BillingCharge.failure_code.contains(params.q)))
    q = apply_sort(q, params.sort, {"updated_at": BillingCharge.updated_at, "next_retry_at": BillingCharge.next_retry_at,
                                    "amount": BillingCharge.amount}, (BillingCharge.updated_at.desc(), BillingCharge.id.desc()))

    def row(t):
        c, s, u, p = t
        out = _charge_row(c)
        out["subscription_status"] = _v(s.status)
        out["user"] = {"id": u.id, "nickname": u.nickname, "realname": u.realname} if u else None
        out["plan"] = {"id": p.id, "name": p.name} if p else None
        return out

    return paginate(q, params, row)


@router.post("/billing/charges/{charge_id}/retry")
async def retry_charge(charge_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """수동 재청구 (API-130). 결과(성공·실패)를 그대로 돌려준다 — 토스 실패도 200 + status=FAILED"""
    c = db.get(BillingCharge, charge_id)
    if not c:
        raise HTTPException(status_code=404, detail="청구를 찾을 수 없습니다")
    before = {"status": c.status, "attempt": c.attempt}
    try:
        engine.retry(db, c)
    except engine.BillingDisabled as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    after = {"status": c.status, "attempt": c.attempt, "payment_id": c.payment_id}
    audit.record("UPDATE", "BILLING_CHARGE", c.id, before=before, after=after,
                 result=None if c.status == "SUCCEEDED" else "FAIL", meta={"failure_code": c.failure_code})
    out = _charge_row(c)
    if c.payment_id:
        p = db.get(Payment, c.payment_id)
        out["payment_key"] = p.payment_key if p else None
    return out
