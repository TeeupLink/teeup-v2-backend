"""빌링키 암호화 (AES-GCM). 키는 환경변수 BILLING_KEY_ENC_KEY 의 SHA-256 으로 만든다.

키를 바꾸면 이미 저장한 빌링키를 풀 수 없다 — 운영에서는 한 번 정하고 바꾸지 않는다.
"""
import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config import settings

_NONCE = 12


class CryptoUnavailable(RuntimeError):
    pass


def _aes() -> AESGCM:
    secret = settings.BILLING_KEY_ENC_KEY
    if not secret:
        raise CryptoUnavailable("BILLING_KEY_ENC_KEY 가 설정되지 않아 빌링키를 다룰 수 없습니다")
    return AESGCM(hashlib.sha256(secret.encode()).digest())


def encrypt(plain: str) -> bytes:
    nonce = os.urandom(_NONCE)
    return nonce + _aes().encrypt(nonce, plain.encode(), None)


def decrypt(blob: bytes) -> str:
    return _aes().decrypt(blob[:_NONCE], blob[_NONCE:], None).decode()


def seal(plain: str) -> str:
    """브라우저에 잠시 맡길 수 있는 문자열 (샌드박스용). 서버만 풀 수 있다"""
    return base64.urlsafe_b64encode(encrypt(plain)).decode()


def unseal(token: str) -> str:
    return decrypt(base64.urlsafe_b64decode(token.encode()))
