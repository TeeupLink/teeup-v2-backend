import json
import logging
import os
from typing import Any

import jwt
import requests
from fastapi import HTTPException, status
from jwt import PyJWKClient

from config import settings
from schemas.oauth import OAuthUserInfo

logger = logging.getLogger(__name__)


class GoogleOAuthError(Exception):
    pass


def _read_token_file(path: str) -> dict:
    if not os.path.exists(path):
        raise GoogleOAuthError(
            "Google OAuth token.json 파일을 찾을 수 없습니다. 토큰 발급 스크립트를 먼저 실행하세요."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_token_file(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def load_access_token() -> str:
    """token.json을 로드하고 refresh_token으로 갱신하여 Access Token을 반환.

    유효기간(expiry) 체크 없이 항상 refresh_token으로 새 토큰을 발급받습니다.

    반환: 유효한 Access Token 문자열
    예외: GoogleOAuthError
    """
    token_path = settings.GOOGLE_TOKEN_FILE
    token = _read_token_file(token_path)

    # google-auth 포맷 또는 단순 포맷 모두 허용
    refresh_token = token.get("refresh_token")
    token_uri = token.get("token_uri") or "https://oauth2.googleapis.com/token"

    if not refresh_token:
        raise GoogleOAuthError(
            "refresh_token이 없어 토큰 갱신을 할 수 없습니다. 토큰을 재발급하세요."
        )

    data = {
        "client_id": settings.GOOGLE_WEB_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    resp = requests.post(token_uri, data=data, timeout=20)
    if resp.status_code != 200:
        error_text = resp.text
        # invalid_grant 오류인 경우 더 명확한 메시지 제공
        if "invalid_grant" in error_text:
            raise GoogleOAuthError(
                "리프레시 토큰이 만료되었거나 무효합니다. "
                "토큰을 재발급해야 합니다. "
                "스크립트를 실행하세요: python scripts/renew_google_drive_token.py"
            )
        raise GoogleOAuthError(f"토큰 갱신 실패: {resp.status_code} {resp.text}")

    refreshed = resp.json()
    new_access_token = refreshed.get("access_token")

    if not new_access_token:
        raise GoogleOAuthError("갱신 응답에 access_token이 없습니다.")

    # 파일 포맷 보존 + 업데이트 (expiry 제거)
    token["access_token"] = new_access_token
    token["token"] = new_access_token
    # expiry 필드 제거 (있는 경우)
    if "expiry" in token:
        del token["expiry"]

    # 되쓰기는 있으면 좋은 정도다. 이 함수는 위에서 보듯 만료를 따지지 않고
    # 매번 refresh_token 으로 새 access_token 을 받아 온다. 즉 파일에 적힌
    # access_token 을 다시 읽어 쓰는 경로가 없다.
    #
    # Cloud Run 에서는 이 파일이 Secret Manager 볼륨으로 들어와 읽기 전용이다.
    # 여기서 예외가 올라가면 토큰은 멀쩡히 발급됐는데 업로드가 통째로 실패한다.
    try:
        _write_token_file(token_path, token)
    except OSError as e:
        logger.warning("토큰 파일 갱신 실패(무시하고 진행): %s", e)

    return new_access_token


def get_mime_type(file_ext: str) -> str:
    ext = (file_ext or "").lower()
    if ext == ".pdf":
        return "application/pdf"
    if ext in (".png",):
        return "image/png"
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    return "application/octet-stream"



# class GoogleUserInfo(TypedDict, total=False):
#     id: str
#     email: str
#     name: str
#     picture: Optional[str]
#     verified_email: bool


def upload_to_drive(
    file_bytes: bytes,
    filename: str,
    folder_id: str,
    share: str | None = None,
) -> dict:
    """파일을 Google Drive 폴더에 올리고 메타데이터를 돌려준다.

    로컬 디스크에 쓰지 않는다. 컨테이너·Cloud Run 은 파일시스템이 휘발이라
    저장해도 재기동 때 사라지고, 서빙 경로도 없다.

    share="link" 를 주면 링크를 아는 사람이 볼 수 있게 권한을 연다.
    권한 부여가 실패해도 업로드 자체는 성공으로 둔다. 파일은 이미 올라갔고,
    권한은 나중에 Drive 에서 손으로 열 수 있다.
    """
    access_token = load_access_token()
    mime_type = get_mime_type(os.path.splitext(filename)[1])

    boundary = "teeuplink_boundary"
    metadata = {"name": filename, "parents": [folder_id]}
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    resp = requests.post(
        "https://www.googleapis.com/upload/drive/v3/files",
        params={"uploadType": "multipart", "fields": "id,name,mimeType"},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        data=body,
        timeout=60,
    )
    if resp.status_code not in (200, 201):
        raise GoogleOAuthError(f"Drive 업로드 실패: {resp.status_code} {resp.text}")

    file_id = resp.json().get("id")
    if not file_id:
        raise GoogleOAuthError("Drive 응답에 파일 ID가 없습니다.")

    if share == "link":
        try:
            requests.post(
                f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                params={"fields": "id"},
                json={"role": "reader", "type": "anyone"},
                timeout=20,
            )
        except requests.RequestException:
            pass

    meta_resp = requests.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        params={"fields": "id,webViewLink,webContentLink,name,size,mimeType"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    meta = meta_resp.json() if meta_resp.status_code == 200 else {}

    return {
        "file_id": file_id,
        "filename": filename,
        "mime_type": meta.get("mimeType", mime_type),
        "web_view_link": meta.get("webViewLink"),
        "web_content_link": meta.get("webContentLink"),
    }


def _coerce_user_info(raw: Any) -> OAuthUserInfo:
    if not isinstance(raw, dict):
        raise ValueError("user_info is not an object")

    user_id = raw.get("id")
    email = raw.get("email")
    name = raw.get("name")

    if (
        not isinstance(user_id, str)
        or not isinstance(email, str)
        or not isinstance(name, str)
    ):
        raise ValueError("required user_info fields missing or invalid")

    picture = raw.get("picture")
    if picture is not None and not isinstance(picture, str):
        raise ValueError("picture must be a string if present")

    verified_email = raw.get("verified_email", False)
    if not isinstance(verified_email, bool):
        raise ValueError("verified_email must be a bool if present")

    return OAuthUserInfo(
        provider="google",
        provider_id=user_id,
        email=email,
        name=name,
        picture=picture,
        verified_email=verified_email,
    )


class GoogleOAuth:
    """Google OAuth 인증 클래스"""

    def __init__(self):
        self.client_types = {
            settings.GOOGLE_WEB_REDIRECT_URI: "web",
            settings.GOOGLE_ANDROID_REDIRECT_URI: "android",
            settings.GOOGLE_IOS_REDIRECT_URI: "ios",
        }

        self.redirect_uris = {
            "web": settings.GOOGLE_WEB_REDIRECT_URI,
            "android": settings.GOOGLE_ANDROID_REDIRECT_URI,
            "ios": settings.GOOGLE_IOS_REDIRECT_URI,
        }

        self.client_ids = {
            "web": settings.GOOGLE_WEB_CLIENT_ID,
            "android": settings.GOOGLE_ANDROID_CLIENT_ID,
            "ios": settings.GOOGLE_IOS_CLIENT_ID,
        }

        self.client_secret = settings.GOOGLE_CLIENT_SECRET
        self.scope = "openid email profile"

        # Google OAuth 엔드포인트
        self.auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
        self.token_url = "https://oauth2.googleapis.com/token"
        self.user_info_url = "https://www.googleapis.com/oauth2/v2/userinfo"

    def _resolve_redirect_uri(
        self,
        client_type: str,
        redirect_uri: str,
        authorizationCode: str,
        codeVerifier: str | None,
    ) -> dict[str, str]:
        if client_type not in self.client_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="유효하지 않은 클라이언트 타입입니다",
            )

        if client_type == "web":
            if not redirect_uri or redirect_uri != self.redirect_uris["web"]:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="유효하지 않은 리디렉션 URI입니다",
                )
            if not codeVerifier:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="웹 OAuth 요청에는 codeVerifier가 필요합니다",
                )
            client_id = self.client_ids["web"]
        else:
            if redirect_uri not in ("", None):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="앱 OAuth 요청의 redirectUri가 유효하지 않습니다",
                )
            client_id = self.client_ids["web"]

        if not client_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="OAuth 클라이언트 ID가 설정되지 않았습니다",
            )

        # 요청 페이로드 생성
        data = {
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": authorizationCode,
            "redirect_uri": redirect_uri,
        }

        if not settings.GOOGLE_CLIENT_SECRET:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="OAuth 클라이언트 시크릿이 설정되지 않았습니다",
            )

        data["client_secret"] = settings.GOOGLE_CLIENT_SECRET

        if client_type == "web":
            data["code_verifier"] = codeVerifier

        return data

    def get_authorization_url(self, state: str) -> str:
        """Google OAuth 인증 URL 생성"""
        try:
            params = {
                "client_id": self.client_ids["web"],
                "redirect_uri": self.redirect_uris["web"],
                "scope": self.scope,
                "response_type": "code",
                "access_type": "offline",
                "prompt": "consent",
            }

            if state:
                params["state"] = state

            # URL 파라미터 생성
            param_string = "&".join([f"{key}={value}" for key, value in params.items()])
            auth_url = f"{self.auth_url}?{param_string}"

            logger.info("Google OAuth 인증 URL 생성 완료")
            return auth_url

        except Exception as e:
            logger.error(f"Google OAuth 인증 URL 생성 실패: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="OAuth 인증 URL 생성에 실패했습니다",
            )

    def exchange_code_for_token(
        self,
        authorizationCode: str,
        codeVerifier: str | None,
        redirect_uri: str,
        client_type: str,
    ) -> dict[str, Any]:
        response = None
        """인증 코드를 액세스 토큰으로 교환"""
        try:
            # # 환경 변수 검증
            # if not self.client_id or not self.client_secret:
            #     logger.error("Google OAuth 클라이언트 ID 또는 시크릿이 설정되지 않았습니다")
            #     raise HTTPException(
            #         status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            #         detail="Google OAuth 설정이 완료되지 않았습니다"
            #     )

            # 리디렉션 URI로 요청 페이로드 생성
            data = self._resolve_redirect_uri(
                client_type, redirect_uri, authorizationCode, codeVerifier
            )

            logger.info(f"Google OAuth 토큰 교환 요청: {data}")

            response = requests.post(self.token_url, data=data)
            if not response:
                logger.error("Google OAuth 토큰 교환 응답이 없습니다")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="OAuth 토큰 교환에 실패했습니다",
                )

            # 응답 로깅
            logger.info(f"Google OAuth 응답 상태: {response.status_code}")
            if response.status_code != 200:
                logger.error(f"Google OAuth 응답 내용: {response.text}")
                logger.error(f"요청 데이터 (client_secret 제외): { {k: v for k, v in data.items() if k != 'client_secret'} }")
            
            response.raise_for_status()

            token_data = response.json()
            logger.info("Google OAuth 토큰 교환 성공")
            return token_data

        except requests.exceptions.HTTPError as e:
            error_text = e.response.text if e.response is not None else "N/A"
            logger.error(f"Google OAuth HTTP 에러: {str(e)}, 응답: {error_text}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"OAuth 토큰 교환에 실패했습니다: {str(e)}",
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"Google OAuth 요청 실패: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="OAuth 토큰 교환에 실패했습니다",
            )
        except Exception as e:
            logger.error(f"Google OAuth 토큰 교환 중 오류: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="OAuth 토큰 교환 중 오류가 발생했습니다",
            )

    def get_user_info(self, access_token: str) -> OAuthUserInfo:
        """액세스 토큰으로 사용자 정보 조회"""
        try:
            headers = {"Authorization": f"Bearer {access_token}"}

            response = requests.get(self.user_info_url, headers=headers)
            response.raise_for_status()

            user_info = _coerce_user_info(response.json())
            logger.info(f"Google 사용자 정보 조회 성공: {user_info.email}")
            return user_info

        except requests.exceptions.RequestException as e:
            logger.error(f"Google 사용자 정보 조회 실패: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="사용자 정보 조회에 실패했습니다",
            )
        except Exception as e:
            logger.error(f"Google 사용자 정보 조회 중 오류: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="사용자 정보 조회 중 오류가 발생했습니다",
            )

    def verify_id_token(self, id_token: str) -> dict[str, Any]:
        """Google ID 토큰 검증"""
        try:
            # Google의 공개 키를 사용하여 ID 토큰 검증
            # 실제 구현에서는 google-auth 라이브러리를 사용하는 것이 좋습니다

            # Google의 JWKS 엔드포인트
            jwks_client = PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")

            # JWT 헤더에서 키 ID 추출
            unverified_header = jwt.get_unverified_header(id_token)
            rsa_key = jwks_client.get_signing_key(unverified_header["kid"]).key

            # 토큰 검증
            audiences = [cid for cid in self.client_ids.values() if cid]
            payload = jwt.decode(
                id_token,
                rsa_key,
                algorithms=["RS256"],
                audience=audiences,
                issuer="https://accounts.google.com",
            )

            logger.info(f"Google ID 토큰 검증 성공: {payload.get('email')}")
            return payload

        except jwt.ExpiredSignatureError:
            logger.warning("Google ID 토큰이 만료되었습니다")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="토큰이 만료되었습니다"
            )
        except jwt.InvalidTokenError as e:
            logger.warning(f"유효하지 않은 Google ID 토큰: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="유효하지 않은 토큰입니다",
            )
        except Exception as e:
            logger.error(f"Google ID 토큰 검증 중 오류: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="토큰 검증 중 오류가 발생했습니다",
            )


# 전역 Google OAuth 인스턴스
google_oauth = GoogleOAuth()
