"""정기결제·결제 관리 (backend #51, backoffice #31). SQLite 메모리 DB + 가짜 토스 클라이언트."""

import os
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("BACKEND_PORT", "7777")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import models  # noqa: E402,F401
from config import settings  # noqa: E402
from database import Base, get_db  # noqa: E402
from models import (AdminAuditLog, BillingCharge, BillingKey, Payment, PaymentCancel, Plan,  # noqa: E402
                    Subscription, User, UserStatus)
from routers import admin as admin_pkg  # noqa: E402
from routers import payment_webhook  # noqa: E402
from routers.admin.deps import get_admin_user  # noqa: E402
from services.billing import crypto, engine, toss  # noqa: E402
from utils import activity, audit  # noqa: E402
from utils.audit import AuditMiddleware  # noqa: E402

A = "/api/v1/admin"


class FakeToss:
    def __init__(self):
        self.calls = []
        self.fail_next: list[toss.TossError] = []
        self.payment_status = "DONE"

    def _maybe_fail(self):
        if self.fail_next:
            raise self.fail_next.pop(0)

    def issue_billing_key(self, auth_key, customer_key):
        self.calls.append(("issue", auth_key, customer_key))
        self._maybe_fail()
        return {"billingKey": "bk_RAW_SECRET_99", "customerKey": customer_key, "cardCompany": "현대", "cardNumber": "433012******1234"}

    def charge(self, billing_key, *, customer_key, amount, order_id, order_name, idempotency_key):
        self.calls.append(("charge", billing_key, amount, order_id, idempotency_key))
        self._maybe_fail()
        return {"paymentKey": f"pk_{order_id}", "orderId": order_id, "totalAmount": amount, "status": "DONE",
                "method": "카드", "approvedAt": "2026-11-01T04:00:01+09:00"}

    def cancel(self, payment_key, *, reason, amount, idempotency_key):
        self.calls.append(("cancel", payment_key, amount, idempotency_key))
        self._maybe_fail()
        return {"status": "PARTIAL_CANCELED", "cancels": [{"transactionKey": f"tx_{idempotency_key}"}]}

    def get_payment(self, payment_key):
        self.calls.append(("get", payment_key))
        return {"paymentKey": payment_key, "status": self.payment_status}


@pytest.fixture
def env(monkeypatch):
    engine_ = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine_)
    Session = sessionmaker(bind=engine_)
    monkeypatch.setattr(activity, "session_factory", Session)
    fake = FakeToss()
    monkeypatch.setattr(toss, "client", fake)
    monkeypatch.setattr(settings, "TOSS_PAYMENTS_SECRET_KEY", "test_sk_dummy")
    monkeypatch.setattr(settings, "BILLING_KEY_ENC_KEY", "unit-test-key")
    monkeypatch.setattr(settings, "BILLING_ENABLED", True)

    db = Session()
    u = User(email="m@t.run", nickname="결제회원", realname="김결제", status=UserStatus.ACTIVE)
    plan = Plan(name="프로", type="PRO", price=9900, billing_cycle="MONTHLY", is_active=True)
    db.add_all([u, plan])
    db.flush()
    key = BillingKey(user_id=u.id, customer_key="cust_1", billing_key_enc=crypto.encrypt("bk_LIVE_KEY"), card_company="신한",
                     card_number_masked="1234****", status="ACTIVE", mode="TEST")
    db.add(key)
    db.flush()
    sub = Subscription(user_id=u.id, plan_id=plan.id, status="ACTIVE", start_date=datetime(2026, 10, 1),
                       next_billing_date=datetime(2026, 11, 1), billing_key_id=key.id)
    paid = Payment(payment_key="pk_paid", order_id="o_paid", order_name="티업링크 프로", amount=9900, status="SUCCEEDED",
                   method="카드", user_id=u.id, approved_at=datetime(2026, 10, 1, 4))
    failed = Payment(order_id="o_fail", order_name="실패", amount=9900, status="FAILED", user_id=u.id)
    db.add_all([sub, paid, failed])
    db.commit()

    who = {"role": "SUPER_ADMIN"}
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.include_router(admin_pkg.router, prefix="/api/v1")
    app.include_router(payment_webhook.router, prefix="/api/v1/payments")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def _me():
        me = {"id": 1, "email": "owner@teeup.run", "name": "운영", "role": who["role"], "status": "ACTIVE", "type": "admin"}
        audit.set_actor(me)
        return me

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_admin_user] = _me

    def as_role(role):
        who["role"] = role
        return TestClient(app)

    yield db, (u, plan, key, sub, paid, failed), fake, as_role
    db.close()


def _audits(db, target_type):
    db.expire_all()
    return db.query(AdminAuditLog).filter(AdminAuditLog.target_type == target_type).all()


# ---- 요금제 -----------------------------------------------------------------

def test_plans_crud_and_delete_guard(env):
    db, (u, plan, *_), _, as_role = env
    c = as_role("SUPER_ADMIN")
    r = c.get(f"{A}/plans")
    assert r.status_code == 200
    assert r.json()["items"][0] == {**r.json()["items"][0], "name": "프로", "price": 9900, "subscribers": 1}

    r = c.post(f"{A}/plans", json={"name": "연간", "price": 99000, "billing_cycle": "YEARLY"})
    assert r.status_code == 201
    new_id = r.json()["id"]
    assert c.put(f"{A}/plans/{new_id}", json={"name": "연간", "price": 89000, "billing_cycle": "YEARLY", "is_active": False}).json()["price"] == 89000
    assert c.post(f"{A}/plans", json={"name": "x", "price": 1, "billing_cycle": "ONE_TIME"}).status_code == 422

    assert c.delete(f"{A}/plans/{plan.id}").status_code == 409
    assert c.delete(f"{A}/plans/{new_id}").status_code == 200
    actions = sorted(a.action for a in _audits(db, "PLAN") if a.result == "SUCCESS")
    assert actions == ["CREATE", "DELETE", "UPDATE"]


def test_support_reads_but_cannot_change(env):
    _, (_, plan, _, sub, paid, _), _, as_role = env
    c = as_role("SUPPORT_ADMIN")
    assert c.get(f"{A}/payments").status_code == 200
    assert c.get(f"{A}/subscriptions").status_code == 200
    assert c.get(f"{A}/billing/failures").status_code == 200
    assert c.post(f"{A}/payments/{paid.id}/cancel", json={"reason": "x"}).status_code == 403
    assert c.post(f"{A}/subscriptions/{sub.id}/cancel", json={"reason": "x"}).status_code == 403
    assert c.post(f"{A}/plans", json={"name": "x", "price": 1}).status_code == 403
    assert c.post(f"{A}/billing/sandbox/issue", json={"auth_key": "a", "customer_key": "cust"}).status_code == 403
    assert as_role("CLUB_ADMIN").get(f"{A}/payments").status_code == 403


# ---- 결제 내역·취소 -----------------------------------------------------------

def test_partial_then_full_cancel(env):
    db, (_, _, _, _, paid, failed), fake, as_role = env
    c = as_role("SUPER_ADMIN")
    rows = {r["id"]: r for r in c.get(f"{A}/payments").json()["items"]}
    assert rows[paid.id]["status"] == "SUCCEEDED" and rows[failed.id]["status"] == "FAILED"

    assert c.post(f"{A}/payments/{paid.id}/cancel", json={"amount": 10000, "reason": "환불"}).status_code == 400
    assert c.post(f"{A}/payments/{failed.id}/cancel", json={"reason": "환불"}).status_code == 400

    r = c.post(f"{A}/payments/{paid.id}/cancel", json={"amount": 3000, "reason": " 일부 환불 "})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "PARTIAL_CANCELED" and body["balance"] == 6900
    assert body["cancels"][0]["reason"] == "일부 환불"
    assert [x["id"] for x in c.get(f"{A}/payments", params={"status": "PARTIAL_CANCELED"}).json()["items"]] == [paid.id]

    r = c.post(f"{A}/payments/{paid.id}/cancel", json={"reason": "나머지"})
    assert r.json()["status"] == "CANCELED" and r.json()["balance"] == 0
    assert [call[2:] for call in fake.calls if call[0] == "cancel"] == [(3000, f"cancel_{paid.id}_1"), (6900, f"cancel_{paid.id}_2")]
    assert c.post(f"{A}/payments/{paid.id}/cancel", json={"reason": "또"}).status_code == 400
    updates = [a for a in _audits(db, "PAYMENT") if a.action == "UPDATE"]
    assert len(updates) == 2 and updates[0].before_json["balance"] == 9900


def test_toss_cancel_error_is_reported(env):
    db, (_, _, _, _, paid, _), fake, as_role = env
    fake.fail_next.append(toss.TossError("NOT_CANCELABLE_PAYMENT", "취소할 수 없는 결제입니다", 400))
    r = as_role("SUPER_ADMIN").post(f"{A}/payments/{paid.id}/cancel", json={"reason": "환불"})
    assert r.status_code == 400 and "NOT_CANCELABLE_PAYMENT" in r.json()["detail"]
    db.expire_all()
    assert db.query(PaymentCancel).count() == 0
    assert [a.result for a in _audits(db, "PAYMENT") if a.action == "UPDATE"] == ["FAIL"]


def test_detail_and_export_are_audited(env):
    db, (_, _, _, _, paid, _), _, as_role = env
    c = as_role("SUPPORT_ADMIN")
    assert c.get(f"{A}/payments/{paid.id}").json()["payment_key"] == "pk_paid"
    assert c.get(f"{A}/payments/export").status_code == 422
    r = c.get(f"{A}/payments/export", params={"reason": "월말 정산", "status": "SUCCEEDED"})
    assert r.status_code == 200 and "o_paid" in r.text and "o_fail" not in r.text
    assert sorted(a.action for a in _audits(db, "PAYMENT")) == ["EXPORT", "VIEW_PII"]


# ---- 청구 엔진 ---------------------------------------------------------------

def test_run_due_does_nothing_when_disabled(env, monkeypatch):
    db, *_ , fake, _ = env
    monkeypatch.setattr(settings, "BILLING_ENABLED", False)
    assert engine.run_due(db, datetime(2026, 11, 1, 4)) == {"skipped": True}
    assert fake.calls == [] and db.query(BillingCharge).count() == 0


def test_run_due_charges_and_advances(env):
    db, (u, _, _, sub, _, _), fake, _ = env
    result = engine.run_due(db, datetime(2026, 11, 1, 4))
    assert result["charged"] == 1 and result["succeeded"] == 1
    charge = db.query(BillingCharge).one()
    assert charge.status == "SUCCEEDED" and charge.attempt == 1 and charge.order_id == f"sub{sub.id}_20261101"
    pay = db.get(Payment, charge.payment_id)
    assert pay.user_id == u.id and pay.amount == 9900 and pay.order_id == f"sub{sub.id}_20261101_a1"
    db.refresh(sub)
    assert sub.next_billing_date == datetime(2026, 12, 1) and sub.status == "ACTIVE"
    assert fake.calls[0][1] == "bk_LIVE_KEY"
    # 같은 날 다시 돌려도 이중 청구하지 않는다
    assert engine.run_due(db, datetime(2026, 11, 1, 5))["charged"] == 0


def test_retry_schedule_then_gave_up_with_notification(env, monkeypatch):
    db, (_, _, _, sub, _, _), fake, _ = env
    sent = []
    monkeypatch.setattr(engine.admin_notify, "notify", lambda *a: sent.append(a))
    err = toss.TossError("REJECT_CARD_PAYMENT", "한도초과", 400)
    fake.fail_next.extend([err, err, err, err])
    now = datetime(2026, 11, 1, 4)
    engine.run_due(db, now)
    charge = db.query(BillingCharge).one()
    db.refresh(sub)
    assert charge.status == "FAILED" and sub.status == "PAST_DUE" and charge.attempt == 1
    for days in (1, 3, 7):
        assert charge.next_retry_at.date() >= (now + timedelta(days=days - 1)).date()
        engine.run_due(db, charge.next_retry_at + timedelta(minutes=1))
        db.refresh(charge)
    assert charge.status == "GAVE_UP" and charge.attempt == engine.MAX_ATTEMPT and charge.next_retry_at is None
    keys = [c[4] for c in fake.calls if c[0] == "charge"]
    assert len(set(keys)) == 4
    assert len(sent) == 1 and sent[0][0] == "BILLING_FAILURE" and sent[0][3] == f"/subscriptions/{sub.id}"


def test_unknown_outcome_reuses_idempotency_key(env):
    db, *_, fake, _ = env
    fake.fail_next.append(toss.TossError("NETWORK_ERROR", "연결 실패"))
    engine.run_due(db, datetime(2026, 11, 1, 4))
    charge = db.query(BillingCharge).one()
    engine.run_due(db, charge.next_retry_at + timedelta(minutes=1))
    db.refresh(charge)
    keys = [c[4] for c in fake.calls if c[0] == "charge"]
    assert keys[0] == keys[1] and charge.status == "SUCCEEDED" and charge.attempt == 1


def test_manual_retry_endpoint(env, monkeypatch):
    db, *_, fake, as_role = env
    fake.fail_next.append(toss.TossError("REJECT_CARD_PAYMENT", "한도초과", 400))
    engine.run_due(db, datetime(2026, 11, 1, 4))
    charge = db.query(BillingCharge).one()
    c = as_role("SUPER_ADMIN")
    failures = c.get(f"{A}/billing/failures").json()["items"]
    assert failures[0]["failure_code"] == "REJECT_CARD_PAYMENT" and failures[0]["max_attempt"] == 4

    monkeypatch.setattr(settings, "BILLING_ENABLED", False)
    assert c.post(f"{A}/billing/charges/{charge.id}/retry").status_code == 409
    monkeypatch.setattr(settings, "BILLING_ENABLED", True)
    r = c.post(f"{A}/billing/charges/{charge.id}/retry")
    assert r.status_code == 200 and r.json()["status"] == "SUCCEEDED" and r.json()["attempt"] == 2
    assert c.post(f"{A}/billing/charges/{charge.id}/retry").status_code == 400
    assert [a.action for a in _audits(db, "BILLING_CHARGE")] == ["UPDATE"]


def test_cancel_subscription_stops_retries(env):
    db, (_, _, _, sub, _, _), fake, as_role = env
    fake.fail_next.append(toss.TossError("REJECT_CARD_PAYMENT", "한도초과", 400))
    engine.run_due(db, datetime(2026, 11, 1, 4))
    c = as_role("SUPER_ADMIN")
    r = c.post(f"{A}/subscriptions/{sub.id}/cancel", json={"reason": "회원 요청"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELED"
    assert r.json()["charges"][0]["status"] == "GAVE_UP"
    assert c.post(f"{A}/subscriptions/{sub.id}/cancel", json={"reason": "또"}).status_code == 409
    n = len(fake.calls)
    engine.run_due(db, datetime(2026, 11, 10))
    assert len(fake.calls) == n
    detail = c.get(f"{A}/subscriptions/{sub.id}").json()
    assert detail["billing_key"]["card_number_masked"] == "1234****" and "billing_key_enc" not in str(detail)


# ---- 상태·샌드박스·웹훅 -------------------------------------------------------

def test_status_reports_mode(env, monkeypatch):
    *_, as_role = env
    c = as_role("SUPPORT_ADMIN")
    assert c.get(f"{A}/billing/status").json() == {**c.get(f"{A}/billing/status").json(), "enabled": True, "mode": "test"}
    monkeypatch.setattr(settings, "TOSS_PAYMENTS_SECRET_KEY", "live_sk_x")
    assert c.get(f"{A}/billing/status").json()["mode"] == "live"


def test_sandbox_flow_never_returns_raw_key(env, monkeypatch):
    db, *_, fake, as_role = env
    c = as_role("SUPER_ADMIN")
    r = c.post(f"{A}/billing/sandbox/issue", json={"auth_key": "auth_1", "customer_key": "sandbox_cust"})
    assert r.status_code == 200
    issued = r.json()
    assert "bk_RAW_SECRET_99" not in r.text and issued["billing_key_hint"] == "****99"
    r = c.post(f"{A}/billing/sandbox/charge", json={"token": issued["token"], "customer_key": "sandbox_cust"})
    assert r.status_code == 200 and r.json()["amount"] == 100
    assert fake.calls[-1][1] == "bk_RAW_SECRET_99"
    assert c.post(f"{A}/billing/sandbox/cancel", json={"payment_key": r.json()["payment_key"]}).status_code == 200
    assert c.post(f"{A}/billing/sandbox/charge", json={"token": "garbage", "customer_key": "x1"}).status_code == 400
    assert db.query(Payment).count() == 2  # 샌드박스는 DB 에 남기지 않는다

    monkeypatch.setattr(settings, "TOSS_PAYMENTS_SECRET_KEY", "live_sk_x")
    assert c.post(f"{A}/billing/sandbox/issue", json={"auth_key": "a", "customer_key": "cust"}).status_code == 403


def test_webhook_trusts_toss_lookup_not_body(env):
    db, (_, _, _, _, paid, _), fake, as_role = env
    c = as_role("SUPER_ADMIN")
    body = {"eventType": "PAYMENT_STATUS_CHANGED", "data": {"paymentKey": "pk_paid", "orderId": "o_paid", "status": "CANCELED"}}
    fake.payment_status = "DONE"  # 위조된 본문: 토스 조회 결과는 그대로 DONE
    assert c.post("/api/v1/payments/webhook", json=body).status_code == 200
    db.refresh(paid)
    assert paid.status == "SUCCEEDED"

    fake.payment_status = "CANCELED"
    c.post("/api/v1/payments/webhook", json=body)
    db.refresh(paid)
    assert paid.status == "CANCELED"
    assert c.post("/api/v1/payments/webhook", json={"eventType": "PAYMENT_STATUS_CHANGED", "data": {"paymentKey": "pk_other"}}).status_code == 200
    assert c.post("/api/v1/payments/webhook", content=b"not json", headers={"content-type": "application/json"}).status_code == 200
