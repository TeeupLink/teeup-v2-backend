"""
빌링 샌드박스 (backend #51, backoffice API스펙 API-131·132, SCR-086) — 최고 관리자 전용, 테스트 키에서만

카드 등록 → 빌링키 발급 → 100원 청구 → 취소를 실제 토스 테스트 환경으로 확인한다.
DB 에는 아무것도 남기지 않는다 (회원·결제 데이터와 섞지 않는다). 발급한 빌링키는 서버만 풀 수 있게
암호화한 토큰으로 브라우저에 잠시 맡기고, 원문은 응답에 넣지 않는다.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from services.billing import crypto, toss

from .deps import get_admin_user

router = APIRouter(tags=["admin-billing-sandbox"])

SANDBOX_AMOUNT = 100


def _test_only():
    if toss.mode() != "test":
        raise HTTPException(status_code=403, detail="테스트 키에서만 샌드박스를 쓸 수 있습니다")


def _toss_error(e: toss.TossError):
    return HTTPException(status_code=400, detail=f"토스 요청 실패: {e.message} ({e.code})")


class IssueBody(BaseModel):
    auth_key: str = Field(..., min_length=1, max_length=300)
    customer_key: str = Field(..., min_length=2, max_length=300)


@router.post("/billing/sandbox/issue")
async def sandbox_issue(body: IssueBody, current_user: dict = Depends(get_admin_user)):
    """authKey → 빌링키 발급 (API-131)"""
    _test_only()
    try:
        res = toss.client.issue_billing_key(body.auth_key, body.customer_key)
        token = crypto.seal(res["billingKey"])
    except toss.TossError as e:
        raise _toss_error(e)
    except crypto.CryptoUnavailable as e:
        raise HTTPException(status_code=409, detail=str(e))
    company, number = toss.card_info(res)
    key = res["billingKey"]
    return {"token": token, "customer_key": body.customer_key, "card_company": company, "card_number_masked": number,
            "billing_key_hint": f"****{key[-2:]}"}


class ChargeBody(BaseModel):
    token: str = Field(..., min_length=1, max_length=2000)
    customer_key: str = Field(..., min_length=2, max_length=300)


@router.post("/billing/sandbox/charge")
async def sandbox_charge(body: ChargeBody, current_user: dict = Depends(get_admin_user)):
    """100원 1회 청구 (API-132). 멱등키는 요청마다 새로 만든다"""
    _test_only()
    order_id = f"sandbox_{uuid.uuid4().hex[:24]}"
    idem = str(uuid.uuid4())
    try:
        billing_key = crypto.unseal(body.token)
    except Exception:
        raise HTTPException(status_code=400, detail="빌링키 토큰을 읽을 수 없습니다. 카드 등록부터 다시 해 주세요")
    try:
        res = toss.client.charge(billing_key, customer_key=body.customer_key, amount=SANDBOX_AMOUNT, order_id=order_id,
                                 order_name="티업링크 샌드박스 테스트", idempotency_key=idem)
    except toss.TossError as e:
        raise _toss_error(e)
    return {"payment_key": res.get("paymentKey"), "order_id": order_id, "amount": res.get("totalAmount", SANDBOX_AMOUNT),
            "status": res.get("status"), "approved_at": res.get("approvedAt"), "idempotency_key": idem}


class CancelBody(BaseModel):
    payment_key: str = Field(..., min_length=1, max_length=300)


@router.post("/billing/sandbox/cancel")
async def sandbox_cancel(body: CancelBody, current_user: dict = Depends(get_admin_user)):
    """샌드박스 청구 전액 취소"""
    _test_only()
    try:
        res = toss.client.cancel(body.payment_key, reason="샌드박스 테스트 취소", amount=None, idempotency_key=str(uuid.uuid4()))
    except toss.TossError as e:
        raise _toss_error(e)
    return {"payment_key": body.payment_key, "status": res.get("status")}
