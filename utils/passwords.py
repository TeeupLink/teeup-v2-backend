"""관리자·회원 비밀번호 해시 (PBKDF2-SHA256, 표준 라이브러리만 사용).

저장 형식: pbkdf2_sha256$<반복 횟수>$<salt base64>$<hash base64>
예전 형식(솔트 없는 SHA-256 hex 64자)도 검증하고, 로그인에 성공하면 새 형식으로 바꿔 저장한다(needs_rehash).
관리자(admins)와 회원(users) 모두 같은 방식이다. 앱은 비밀번호 원문을 보내므로 앱 수정 없이 서버에서만 바뀐다.
"""
import base64
import hashlib
import hmac
import re
import secrets

ALGO = "pbkdf2_sha256"
ITERATIONS = 600_000  # OWASP 2023 권고 (PBKDF2-HMAC-SHA256)
_LEGACY = re.compile(r"^[0-9a-f]{64}$")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGO}${iterations}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or password is None:
        return False
    if stored.startswith(f"{ALGO}$"):
        try:
            _, iters, salt, expected = stored.split("$")
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _unb64(salt), int(iters))
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(_b64(dk), expected)
    if _LEGACY.match(stored):
        return hmac.compare_digest(hashlib.sha256(password.encode("utf-8")).hexdigest(), stored)
    return False


def needs_rehash(stored: str | None) -> bool:
    """예전 SHA-256 이거나 반복 횟수가 지금 기준보다 적으면 True"""
    if not stored or not stored.startswith(f"{ALGO}$"):
        return True
    try:
        return int(stored.split("$")[1]) < ITERATIONS
    except (IndexError, ValueError):
        return True
