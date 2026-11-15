"""토스페이먼츠 API 클라이언트 (빌링·취소·조회).

- 인증: toss_auth_headers() — 시크릿 키 뒤 콜론 포함 base64 (backend #48)
- 실패는 TossError(code, message) 로 올린다. 화면에는 토스 코드·메시지를 그대로 보여 준다
- 테스트는 모듈 변수 client 를 가짜로 바꾼다
"""
import logging
from typing import Any

import requests

from config import settings
from utils.toss_payments import toss_auth_headers

logger = logging.getLogger(__name__)

TIMEOUT = 15


class TossError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


def mode() -> str:
    """시크릿 키 종류. test_ 접두사면 test, 비어 있으면 unset, 그 외 live"""
    key = settings.TOSS_PAYMENTS_SECRET_KEY
    if not key:
        return "unset"
    return "test" if key.startswith("test_") else "live"


class TossClient:
    def _post(self, path: str, body: dict, idempotency_key: str | None = None) -> dict[str, Any]:
        return self._send("POST", path, body, idempotency_key)

    def _send(self, method: str, path: str, body: dict | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        if mode() == "unset":
            raise TossError("NOT_CONFIGURED", "토스 시크릿 키가 설정되지 않았습니다")
        headers = toss_auth_headers(settings.TOSS_PAYMENTS_SECRET_KEY)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        url = f"{settings.TOSS_PAYMENTS_BASE_URL.rstrip('/')}{path}"
        try:
            res = requests.request(method, url, headers=headers, json=body, timeout=TIMEOUT)
        except requests.RequestException as e:
            # 응답을 못 받은 경우 — 토스 쪽 처리 여부를 모르므로 같은 멱등키로만 다시 시도해야 한다
            logger.error(f"토스 요청 실패 {method} {path}: {type(e).__name__}")
            raise TossError("NETWORK_ERROR", "토스 서버에 연결하지 못했습니다") from e
        data = res.json() if res.content else {}
        if res.status_code >= 400:
            raise TossError(data.get("code", f"HTTP_{res.status_code}"), data.get("message", "토스 요청이 실패했습니다"), res.status_code)
        return data

    def issue_billing_key(self, auth_key: str, customer_key: str) -> dict[str, Any]:
        return self._post("/billing/authorizations/issue", {"authKey": auth_key, "customerKey": customer_key})

    def charge(self, billing_key: str, *, customer_key: str, amount: int, order_id: str, order_name: str,
               idempotency_key: str) -> dict[str, Any]:
        body = {"customerKey": customer_key, "amount": amount, "orderId": order_id, "orderName": order_name}
        return self._post(f"/billing/{billing_key}", body, idempotency_key)

    def cancel(self, payment_key: str, *, reason: str, amount: int | None, idempotency_key: str) -> dict[str, Any]:
        body: dict[str, Any] = {"cancelReason": reason}
        if amount is not None:
            body["cancelAmount"] = amount
        return self._post(f"/payments/{payment_key}/cancel", body, idempotency_key)

    def get_payment(self, payment_key: str) -> dict[str, Any]:
        return self._send("GET", f"/payments/{payment_key}")


client = TossClient()


def card_info(issued: dict) -> tuple[str | None, str | None]:
    """빌링키 발급 응답에서 카드사·마스킹 번호"""
    card = issued.get("card") or {}
    return issued.get("cardCompany") or card.get("issuerCode"), issued.get("cardNumber") or card.get("number")
