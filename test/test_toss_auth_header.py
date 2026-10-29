"""토스페이먼츠 요청의 Authorization 헤더가 올바르게 인코딩되는지 확인한다.

막으려는 버그 (backend #48):
    routers/payments.py 의 결제 조회(GET /{payment_key})와 결제 취소
    (POST /cancel)가 시크릿 키를 base64 인코딩 없이 "Basic {키}" 로
    보냈다. 토스는 "{키}:" 를 base64 로 인코딩한 값을 요구하므로 두
    API 가 항상 401 로 실패했다. 승인(confirm)만 올바르게 인코딩했다.

이 파일은 서버를 띄우지 않고 TestClient 로만 돌며, 토스 호출은
requests 를 가짜로 바꿔 보낸 헤더만 검사한다.
"""

import base64
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# DB 접속을 피한다
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("BACKEND_PORT", "7777")

from database import get_db  # noqa: E402
from routers import payments  # noqa: E402
from utils.toss_payments import TossPaymentsAPI, toss_auth_headers  # noqa: E402

SECRET = "test_sk_dummy_1234"
EXPECTED = "Basic " + base64.b64encode(f"{SECRET}:".encode()).decode()


def test_helper_encodes_secret_with_colon():
    headers = toss_auth_headers(SECRET)
    assert headers["Authorization"] == EXPECTED
    assert headers["Content-Type"] == "application/json"


def test_client_class_uses_helper(monkeypatch):
    monkeypatch.setenv("TOSS_PAYMENTS_SECRET_KEY", SECRET)
    assert TossPaymentsAPI().headers["Authorization"] == EXPECTED


class _FakeResponse:
    status_code = 200

    def json(self):
        return {"status": "DONE", "paymentKey": "pk", "orderId": "o", "totalAmount": 0}


@pytest.fixture
def client(monkeypatch):
    sent = []

    def fake_request(url, headers=None, **kwargs):
        sent.append(headers)
        return _FakeResponse()

    monkeypatch.setattr(payments, "TOSS_SECRET_KEY", SECRET)
    monkeypatch.setattr(payments.requests, "get", fake_request)
    monkeypatch.setattr(payments.requests, "post", fake_request)

    app = FastAPI()
    app.include_router(payments.router)
    app.dependency_overrides[payments.get_current_user] = lambda: {"user_id": 1}
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app, raise_server_exceptions=False), sent


def test_get_payment_sends_encoded_header(client):
    c, sent = client
    c.get("/pk_123")
    assert sent, "토스 조회 API 를 호출하지 않았다"
    assert sent[0]["Authorization"] == EXPECTED


def test_cancel_payment_sends_encoded_header(client):
    c, sent = client
    c.post("/cancel", params={"payment_key": "pk_123", "cancel_reason": "테스트"})
    assert sent, "토스 취소 API 를 호출하지 않았다"
    assert sent[0]["Authorization"] == EXPECTED
