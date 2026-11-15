"""
백오피스 결제 내역·요금제 API (backend #51, backoffice API스펙 API-120~124)

- 결제 상태는 저장값(SUCCEEDED·CANCELED·FAILED·PENDING)에 취소 합계를 더해 PARTIAL_CANCELED 를 만든다
- 취소·환불·요금제 변경은 쓰기 권한(최고 관리자)만. 고객지원은 조회만 한다 (permissions.py "payments")
- 예전 /refunds 는 /payments?status=CANCELED 로 대신한다 (status_filter 를 무시하던 버그)
"""
import csv
import io
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database import get_db
from models import Admin, BillingCharge, Payment, PaymentCancel, Plan, Subscription, User
from services.billing import cancel as cancel_service
from services.billing import toss
from utils import audit
from utils.datetime_utils import get_kst_now

from .deps import get_admin_user
from .pagination import PageParams, apply_sort, paginate

router = APIRouter(tags=["admin-payments"])

PAYMENT_STATUSES = ("SUCCEEDED", "PARTIAL_CANCELED", "CANCELED", "FAILED", "PENDING")
EXPORT_MAX = 10000
_DATE = r"^\d{4}-\d{2}-\d{2}$"


def _iso(d):
    return d.isoformat() if d else None


def _user(u: User | None) -> dict | None:
    return {"id": u.id, "nickname": u.nickname, "realname": u.realname} if u else None


def _canceled_sub(db: Session):
    return (db.query(PaymentCancel.payment_id, func.sum(PaymentCancel.cancel_amount).label("canceled"))
            .group_by(PaymentCancel.payment_id).subquery())


class _Filters:
    def __init__(
        self,
        status_: list[str] = Query([], alias="status"),
        user_id: int | None = Query(None),
        method: str | None = Query(None, max_length=50),
        date_from: str | None = Query(None, pattern=_DATE),
        date_to: str | None = Query(None, pattern=_DATE),
    ):
        bad = [s for s in status_ if s not in PAYMENT_STATUSES]
        if bad:
            raise HTTPException(status_code=422, detail="알 수 없는 결제 상태입니다")
        self.statuses, self.user_id, self.method, self.date_from, self.date_to = status_, user_id, method, date_from, date_to


def _query(db: Session, f: _Filters, q: str | None):
    cs = _canceled_sub(db)
    canceled = func.coalesce(cs.c.canceled, 0)
    query = (db.query(Payment, User, canceled.label("canceled"))
             .outerjoin(cs, cs.c.payment_id == Payment.id)
             .outerjoin(User, User.id == Payment.user_id))
    if f.statuses:
        conds = {
            "SUCCEEDED": (Payment.status == "SUCCEEDED") & (canceled == 0),
            "PARTIAL_CANCELED": (Payment.status == "SUCCEEDED") & (canceled > 0),
            "CANCELED": Payment.status == "CANCELED",
            "FAILED": Payment.status == "FAILED",
            "PENDING": Payment.status == "PENDING",
        }
        query = query.filter(or_(*[conds[s] for s in f.statuses]))
    if f.user_id:
        query = query.filter(Payment.user_id == f.user_id)
    if f.method:
        query = query.filter(Payment.method == f.method)
    if f.date_from:
        query = query.filter(Payment.created_at >= datetime.strptime(f.date_from, "%Y-%m-%d"))
    if f.date_to:
        query = query.filter(Payment.created_at < datetime.strptime(f.date_to, "%Y-%m-%d") + timedelta(days=1))
    if q:
        query = query.filter(or_(Payment.order_id.contains(q), Payment.order_name.contains(q), User.nickname.contains(q)))
    return query


def _row(t) -> dict:
    p, u, canceled = t
    canceled = int(canceled or 0)
    return {
        "id": p.id,
        "order_id": p.order_id,
        "order_name": p.order_name,
        "amount": int(p.amount),
        "canceled_amount": canceled,
        "balance": int(p.amount) - canceled,
        "status": cancel_service.display_status(p, canceled),
        "method": p.method,
        "user": _user(u),
        "approved_at": _iso(p.approved_at),
        "created_at": _iso(p.created_at),
    }


@router.get("/payments")
async def list_payments(params: PageParams = Depends(), f: _Filters = Depends(), db: Session = Depends(get_db),
                        current_user: dict = Depends(get_admin_user)):
    """결제 내역 (API-120). 기본 최신순"""
    query = apply_sort(_query(db, f, params.q), params.sort,
                       {"created_at": Payment.created_at, "approved_at": Payment.approved_at, "amount": Payment.amount},
                       (Payment.created_at.desc(), Payment.id.desc()))
    return paginate(query, params, _row)


@router.get("/payments/export")
async def export_payments(reason: str = Query(..., min_length=1, max_length=500), q: str | None = Query(None, max_length=100),
                          f: _Filters = Depends(), db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """CSV 내보내기 (API-123). 사유 필수, 감사 로그 EXPORT"""
    rows = _query(db, f, q).order_by(Payment.created_at.desc(), Payment.id.desc()).limit(EXPORT_MAX + 1).all()
    if len(rows) > EXPORT_MAX:
        raise HTTPException(status_code=422, detail=f"한 번에 {EXPORT_MAX}건까지 내보낼 수 있습니다. 기간을 줄여 주세요")
    audit.record("EXPORT", "PAYMENT", None, reason=reason,
                 meta={"count": len(rows), "filters": {"status": f.statuses, "user_id": f.user_id, "method": f.method,
                                                        "date_from": f.date_from, "date_to": f.date_to, "q": q}})
    buf = io.StringIO()
    buf.write("﻿")  # 엑셀이 UTF-8 로 읽도록
    w = csv.writer(buf)
    w.writerow(["id", "주문번호", "상품", "회원 ID", "닉네임", "금액", "취소 금액", "잔액", "상태", "수단", "승인일시", "생성일시"])
    for t in rows:
        r = _row(t)
        u = r["user"] or {}
        w.writerow([r["id"], r["order_id"], r["order_name"], u.get("id"), u.get("nickname"), r["amount"], r["canceled_amount"],
                    r["balance"], r["status"], r["method"], r["approved_at"], r["created_at"]])
    filename = f"payments-{get_kst_now():%Y%m%d-%H%M}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _get_payment(db: Session, payment_id: int) -> Payment:
    p = db.get(Payment, payment_id)
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="결제 내역을 찾을 수 없습니다")
    return p


def _detail(db: Session, p: Payment) -> dict:
    canceled = cancel_service.canceled_total(db, p.id)
    u = db.get(User, p.user_id) if p.user_id else None
    out = _row((p, u, canceled))
    cancels = (db.query(PaymentCancel, Admin.email).outerjoin(Admin, Admin.id == PaymentCancel.canceled_by_admin_id)
               .filter(PaymentCancel.payment_id == p.id).order_by(PaymentCancel.created_at, PaymentCancel.id).all())
    charge = db.query(BillingCharge).filter(BillingCharge.payment_id == p.id).first()
    out.update({
        "payment_key": p.payment_key,
        "requested_at": _iso(p.requested_at),
        "subscription_id": charge.subscription_id if charge else None,
        "cancels": [{"id": c.id, "amount": c.cancel_amount, "reason": c.cancel_reason, "transaction_key": c.transaction_key,
                     "admin_email": email, "created_at": _iso(c.created_at)} for c, email in cancels],
    })
    return out


@router.get("/payments/{payment_id}")
async def get_payment(payment_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """결제 상세 + 취소 이력 (API-121). 회원 정보가 보이므로 VIEW_PII 로 남긴다"""
    p = _get_payment(db, payment_id)
    audit.record("VIEW_PII", "PAYMENT", p.id)
    return _detail(db, p)


class CancelBody(BaseModel):
    amount: int | None = Field(None, ge=1, description="비우면 잔액 전부")
    reason: str = Field(..., min_length=1, max_length=200)


@router.post("/payments/{payment_id}/cancel")
async def cancel_payment(payment_id: int, body: CancelBody, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """전액 취소·부분 환불 (API-122). 토스 오류는 코드·메시지를 그대로 400 으로 돌려준다"""
    p = _get_payment(db, payment_id)
    before = {"status": p.status, "balance": cancel_service.balance(db, p)}
    reason = body.reason.strip()
    try:
        row = cancel_service.cancel_payment(db, p, amount=body.amount, reason=reason, admin_id=current_user.get("id"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except toss.TossError as e:
        db.rollback()
        audit.record("UPDATE", "PAYMENT", p.id, before=before, reason=reason, result="FAIL", meta={"toss_code": e.code})
        raise HTTPException(status_code=400, detail=f"토스 취소 실패: {e.message} ({e.code})")
    audit.record("UPDATE", "PAYMENT", p.id, before=before, after={"status": p.status, "balance": cancel_service.balance(db, p)},
                 reason=reason, meta={"cancel_id": row.id, "cancel_amount": row.cancel_amount})
    return _detail(db, p)


# ---- 요금제 ----------------------------------------------------------------

_PLAN_FIELDS = ("name", "description", "type", "price", "billing_cycle", "is_active")


def _plan(p: Plan, subscribers: int) -> dict:
    return {
        "id": p.id, "name": p.name, "description": p.description, "type": p.type, "price": int(p.price or 0),
        "billing_cycle": p.billing_cycle, "is_active": bool(p.is_active), "subscribers": subscribers,
        "created_at": _iso(p.created_at), "updated_at": _iso(p.updated_at),
    }


def _subscriber_counts(db: Session) -> dict[int, int]:
    rows = (db.query(Subscription.plan_id, func.count(Subscription.id)).filter(Subscription.status != "CANCELED")
            .group_by(Subscription.plan_id).all())
    return dict(rows)


@router.get("/plans")
async def list_plans(db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """요금제 전체 (API-124). 개수가 적어 페이징하지 않는다. 구독자 수 = 해지 안 된 구독"""
    counts = _subscriber_counts(db)
    plans = db.query(Plan).order_by(Plan.is_active.desc(), Plan.price, Plan.id).all()
    return {"items": [_plan(p, counts.get(p.id, 0)) for p in plans], "total": len(plans)}


class PlanBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(None, max_length=1000)
    type: str = Field("BASIC", max_length=50)
    price: int = Field(..., ge=0, le=10_000_000, description="원")
    billing_cycle: str = Field("MONTHLY", pattern="^(MONTHLY|QUARTERLY|YEARLY)$")
    is_active: bool = True


@router.post("/plans", status_code=201)
async def create_plan(body: PlanBody, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    p = Plan(**body.model_dump())
    db.add(p)
    db.commit()
    audit.record("CREATE", "PLAN", p.id, after=audit.snapshot(p, _PLAN_FIELDS))
    return _plan(p, 0)


def _get_plan(db: Session, plan_id: int) -> Plan:
    p = db.get(Plan, plan_id)
    if not p:
        raise HTTPException(status_code=404, detail="요금제를 찾을 수 없습니다")
    return p


@router.put("/plans/{plan_id}")
async def update_plan(plan_id: int, body: PlanBody, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """금액을 바꾸면 다음 청구부터 새 금액이 적용된다 (이미 만든 청구는 그대로)"""
    p = _get_plan(db, plan_id)
    before = audit.snapshot(p, _PLAN_FIELDS)
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    db.commit()
    audit.record("UPDATE", "PLAN", p.id, before=before, after=audit.snapshot(p, _PLAN_FIELDS))
    return _plan(p, _subscriber_counts(db).get(p.id, 0))


@router.delete("/plans/{plan_id}")
async def delete_plan(plan_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """구독(해지 포함) 기록이 있으면 삭제하지 않는다 — 비활성으로 바꾼다"""
    p = _get_plan(db, plan_id)
    if db.query(Subscription.id).filter(Subscription.plan_id == p.id).first():
        raise HTTPException(status_code=409, detail="구독 기록이 있는 요금제는 삭제할 수 없습니다. 비활성으로 바꿔 주세요")
    before = audit.snapshot(p, _PLAN_FIELDS)
    db.delete(p)
    db.commit()
    audit.record("DELETE", "PLAN", plan_id, before=before)
    return {"message": "요금제를 삭제했습니다", "success": True}
