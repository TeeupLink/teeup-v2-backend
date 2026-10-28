"""
토스페이먼츠 API 연동 유틸리티
"""
import os
import base64
import requests
import json
from typing import Any
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


def toss_auth_headers(secret_key: str) -> dict[str, str]:
    """토스페이먼츠 API 요청 헤더.

    시크릿 키 뒤에 콜론을 붙여 base64 로 인코딩한 값을 Basic 인증에 쓴다.
    키를 그대로 넣으면 토스가 401 을 돌려준다.
    """
    encoded = base64.b64encode(f"{secret_key}:".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json",
    }


class TossPaymentsAPI:
    """토스페이먼츠 API 클라이언트"""
    
    def __init__(self):
        self.secret_key = os.getenv("TOSS_PAYMENTS_SECRET_KEY", "test_gsk_docs_OaPz8L5KdmQXkzRz3y47BMw6")
        self.client_key = os.getenv("TOSS_PAYMENTS_CLIENT_KEY", "test_gck_docs_Ovk5rk1EwkEbP0W43n07xlzm")
        self.base_url = "https://api.tosspayments.com"
        self.headers = toss_auth_headers(self.secret_key)
    
    def confirm_payment(self, payment_key: str, order_id: str, amount: int) -> dict[str, Any]:
        """결제 승인"""
        try:
            url = f"{self.base_url}/v1/payments/confirm"
            data = {
                "paymentKey": payment_key,
                "orderId": order_id,
                "amount": amount
            }
            
            response = requests.post(url, headers=self.headers, json=data)
            response.raise_for_status()
            
            result = response.json()
            logger.info(f"결제 승인 성공: {order_id}")
            return result
            
        except requests.exceptions.RequestException as e:
            logger.error(f"결제 승인 실패: {e}")
            raise Exception(f"결제 승인 실패: {str(e)}")
    
    def cancel_payment(self, payment_key: str, cancel_reason: str, cancel_amount: int | None = None) -> dict[str, Any]:
        """결제 취소/환불"""
        try:
            url = f"{self.base_url}/v1/payments/{payment_key}/cancel"
            data = {
                "cancelReason": cancel_reason
            }
            
            if cancel_amount:
                data["cancelAmount"] = cancel_amount
            
            response = requests.post(url, headers=self.headers, json=data)
            response.raise_for_status()
            
            result = response.json()
            logger.info(f"결제 취소 성공: {payment_key}")
            return result
            
        except requests.exceptions.RequestException as e:
            logger.error(f"결제 취소 실패: {e}")
            raise Exception(f"결제 취소 실패: {str(e)}")
    
    def get_payment(self, payment_key: str) -> dict[str, Any]:
        """결제 조회"""
        try:
            url = f"{self.base_url}/v1/payments/{payment_key}"
            
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            
            result = response.json()
            logger.info(f"결제 조회 성공: {payment_key}")
            return result
            
        except requests.exceptions.RequestException as e:
            logger.error(f"결제 조회 실패: {e}")
            raise Exception(f"결제 조회 실패: {str(e)}")
    
    def create_payment_widget_url(self, order_id: str, order_name: str, amount: int, 
                                 customer_name: str, customer_email: str,
                                 success_url: str, fail_url: str) -> dict[str, Any]:
        """결제 위젯 URL 생성"""
        return {
            "orderId": order_id,
            "orderName": order_name,
            "amount": amount,
            "customerName": customer_name,
            "customerEmail": customer_email,
            "successUrl": success_url,
            "failUrl": fail_url,
            "clientKey": self.client_key
        }

# 전역 인스턴스
toss_payments = TossPaymentsAPI()

def generate_order_id(prefix: str = "order") -> str:
    """주문 ID 생성"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    import secrets
    random_suffix = secrets.token_hex(4)
    return f"{prefix}_{timestamp}_{random_suffix}"

def format_amount(amount: float) -> int:
    """금액을 토스페이먼츠 형식으로 변환 (원 단위)"""
    return int(amount)

def parse_payment_status(toss_status: str) -> str:
    """토스페이먼츠 상태를 내부 상태로 변환"""
    status_map = {
        "DONE": "SUCCEEDED",
        "CANCELED": "FAILED", 
        "PARTIAL_CANCELED": "SUCCEEDED",
        "ABORTED": "FAILED",
        "EXPIRED": "FAILED"
    }
    return status_map.get(toss_status, "PENDING")

def parse_payment_method(toss_method: str) -> str:
    """토스페이먼츠 결제 수단을 내부 형식으로 변환"""
    method_map = {
        "카드": "CARD",
        "가상계좌": "VIRTUAL_ACCOUNT",
        "계좌이체": "BANK_TRANSFER"
    }
    return method_map.get(toss_method, "CARD")






