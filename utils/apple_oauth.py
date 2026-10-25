"""
Apple Sign In (Sign in with Apple) identity token 검증 유틸

네이티브 iOS 앱은 Apple로부터 identity token(JWT)을 직접 받아오므로
서버는 authorization code 교환 없이 이 토큰의 서명만 검증하면 된다.
"""

import logging
from typing import Any

import jwt
from jwt import PyJWKClient

from config import settings
from schemas import OAuthUserInfo

logger = logging.getLogger(__name__)

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"


class AppleOAuth:
    """Apple identity token 검증기"""

    def __init__(self) -> None:
        # PyJWKClient가 Apple 공개키를 내부적으로 캐싱한다 (키 롤링 대응).
        self._jwk_client: PyJWKClient | None = None

    @property
    def jwk_client(self) -> PyJWKClient:
        if self._jwk_client is None:
            self._jwk_client = PyJWKClient(APPLE_KEYS_URL, cache_keys=True)
        return self._jwk_client

    @property
    def allowed_audiences(self) -> list[str]:
        """허용 audience 목록 (네이티브 앱의 번들 ID + 웹용 Service ID)"""
        raw = [settings.APPLE_BUNDLE_ID, settings.APPLE_SERVICE_ID]
        return [value for value in raw if value]

    def verify_identity_token(self, identity_token: str) -> dict[str, Any]:
        """
        Apple identity token 서명 및 클레임 검증.

        검증 항목: 서명(RS256, Apple 공개키), iss, aud, exp
        """
        if not self.allowed_audiences:
            logger.error("APPLE_BUNDLE_ID / APPLE_SERVICE_ID가 설정되지 않았습니다")
            raise ValueError("Apple 로그인 설정이 완료되지 않았습니다")

        # 토큰 형식 오류(클라이언트 문제)와 공개키 조회 실패(네트워크/Apple 문제)를
        # 구분해야 심사 리젝 같은 상황에서 로그만 보고 원인을 짚을 수 있다.
        try:
            header = jwt.get_unverified_header(identity_token)
        except jwt.DecodeError as e:
            logger.error(f"Apple 토큰 형식 오류 (JWT 아님): {e} / 길이={len(identity_token or '')}")
            raise ValueError("Apple 토큰 형식이 올바르지 않습니다")

        try:
            signing_key = self.jwk_client.get_signing_key_from_jwt(identity_token)
        except jwt.PyJWKClientError as e:
            # kid 가 Apple JWKS 에 없음 — 키 롤링 직후이거나 위조 토큰
            logger.error(f"Apple 공개키에 일치하는 kid 없음: kid={header.get('kid')} / {e}")
            raise ValueError("Apple 토큰 서명 키를 찾을 수 없습니다")
        except Exception as e:
            logger.error(f"Apple 공개키 조회 실패 (네트워크?): {type(e).__name__}: {e}")
            raise ValueError("Apple 공개키를 가져오지 못했습니다")

        try:
            claims = jwt.decode(
                identity_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.allowed_audiences,
                issuer=APPLE_ISSUER,
                options={"require": ["exp", "iss", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            raise ValueError("만료된 Apple 토큰입니다")
        except jwt.InvalidAudienceError:
            raise ValueError("Apple 토큰의 audience가 일치하지 않습니다")
        except jwt.InvalidTokenError as e:
            logger.error(f"Apple 토큰 검증 실패: {e}")
            raise ValueError("유효하지 않은 Apple 토큰입니다")

        return claims

    def get_user_info(
        self,
        identity_token: str,
        full_name: str | None = None,
    ) -> OAuthUserInfo:
        """
        identity token을 검증하고 OAuthUserInfo로 변환한다.

        주의: Apple은 이름을 identity token에 담지 않는다. 이름은 사용자가
        '최초 1회' 인증할 때만 클라이언트 SDK로 전달되므로, 클라이언트가
        넘겨준 full_name을 그대로 사용한다.
        """
        claims = self.verify_identity_token(identity_token)

        provider_id = claims.get("sub")
        if not provider_id:
            raise ValueError("Apple 토큰에 sub 클레임이 없습니다")

        email = claims.get("email")

        # email_verified / is_private_email은 bool 또는 "true" 문자열로 올 수 있다.
        def _as_bool(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            return str(value).lower() == "true"

        verified_email = _as_bool(claims.get("email_verified"))

        return OAuthUserInfo(
            provider="apple",
            provider_id=provider_id,
            email=email or "",
            name=(full_name or "").strip(),
            picture=None,
            verified_email=verified_email,
        )


apple_oauth = AppleOAuth()
