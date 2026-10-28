"""
OAuth 인증 라우터
"""

import secrets
import logging
import json
from urllib.parse import urlencode
from typing import Any, cast
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, status, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session
from database import get_db
from models import User
from schemas import GoogleOAuthBody, AppleOAuthBody, UserStatus, Provider
from schemas import OAuthLoginRequest, OAuthCallbackRequest, OAuthUserInfo
from services.push_token_service import sync_user_push_token
from utils import activity
from utils.google_oauth import google_oauth
from utils.apple_oauth import apple_oauth
from config import settings
from utils.jwt_auth import jwt_auth
from utils.cuid import generate_cuid
import hashlib

from routers.auth import set_auth_cookies

router = APIRouter(prefix="/auth/oauth", tags=["OAuth"])
logger = logging.getLogger(__name__)
CLIENT_TYPES = {"web", "android", "ios"}


def _detect_client_type(request: Request, redirect_uri: str | None) -> str:
    client_type = request.headers.get("x-client-type")
    if client_type not in CLIENT_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효하지 않은 X-Client-Type 헤더입니다.")

    expected_redirect_uri = {
        "web": settings.GOOGLE_WEB_REDIRECT_URI,
        "android": settings.GOOGLE_ANDROID_REDIRECT_URI,
        "ios": settings.GOOGLE_IOS_REDIRECT_URI,
    }[client_type]

    if client_type == "web" and redirect_uri != expected_redirect_uri:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="클라이언트 타입과 redirectUri가 일치하지 않습니다.")

    if client_type != "web" and redirect_uri and redirect_uri != expected_redirect_uri:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="클라이언트 타입과 redirectUri가 일치하지 않습니다.")

    return client_type


def _is_app_client(client_type: str) -> bool:
    return client_type in ("android", "ios")


def _get_refresh_expire_delta(client_type: str) -> timedelta:
    if _is_app_client(client_type):
        return timedelta(days=settings.JWT_APP_REFRESH_EXPIRE_DAYS)
    return timedelta(days=settings.JWT_WEB_REFRESH_EXPIRE_DAYS)


@router.get("/google")
async def google_oauth_login():
    """Google OAuth 로그인 시작"""
    try:
        # # Google OAuth 설정 검증
        # if not google_oauth.client_id or not google_oauth.client_secret:
        #     logger.error("Google OAuth 클라이언트 ID 또는 시크릿이 설정되지 않았습니다")
        #     raise HTTPException(
        #         status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        #         detail="Google OAuth 설정이 완료되지 않았습니다. 관리자에게 문의하세요."
        #     )

        # 상태 값 생성 (CSRF 보호용)
        state = secrets.token_urlsafe(32)

        # Google OAuth 인증 URL 생성
        auth_url = google_oauth.get_authorization_url(state)

        logger.info("Google OAuth 로그인 시작")
        return {"auth_url": auth_url, "state": state}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Google OAuth 로그인 시작 실패: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OAuth 로그인 시작에 실패했습니다",
        )


# @router.get("/google/callback")
# async def google_oauth_callback(
#     code: str = Query(..., description="OAuth 인증 코드"),
#     state: str = Query(None, description="OAuth 상태 값"),
#     db: Session = Depends(get_db),
# ):
#     """Google OAuth 콜백 처리"""
#     try:
#         # 인증 코드를 액세스 토큰으로 교환
#         oauth_token_data = google_oauth.exchange_code_for_token_web(code)
#         access_token = oauth_token_data.get("access_token")
#         id_token = oauth_token_data.get("id_token")

#         if not access_token:
#             raise HTTPException(
#                 status_code=status.HTTP_400_BAD_REQUEST,
#                 detail="액세스 토큰을 받지 못했습니다",
#             )

#         # 사용자 정보 조회
#         google_user = google_oauth.get_user_info(access_token)

#         # 사용자 생성 또는 조회
#         user, is_new_user = await create_or_get_oauth_user(google_user, db)
        _log_oauth_login(user, is_new_user, "GOOGLE")

#         # JWT 토큰 생성
#         jwt_payload = {
#             "id": user.id,
#             "email": user.email,
#             "nickname": user.nickname,
#             "role": "user",
#             "provider": user.provider.value if user.provider is not None else None,
#         }

#         access_token = jwt_auth.create_access_token(jwt_payload)
#         refresh_token = jwt_auth.create_refresh_token(jwt_payload)

#         logger.info(f"Google OAuth 로그인 성공: {user.email}")

#         payload = {
#             "access_token": access_token,
#             "refresh_token": refresh_token,
#             "token_type": "bearer",
#             "expires_in": jwt_auth.expire_minutes * 60,
#             "user": {
#                 "id": user.id,
#                 "email": user.email,
#                 "nickname": user.nickname,
#                 "role": "user",
#                 "status": user.status.value,
#                 "provider": user.provider.value if user.provider is not None else None,
#                 "created_at": (
#                     user.created_at.isoformat() if user.created_at is not None else None
#                 ),
#                 "updated_at": (
#                     user.updated_at.isoformat() if user.updated_at is not None else None
#                 ),
#                 "profile_image": user.profile_image,
#                 "phone": user.phone_number,
#             },
#             "is_new_user": is_new_user,
#         }

#         query_params = {
#             "payload": json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
#         }
#         if state:
#             query_params["state"] = state

#         redirect_url = (
#             f"{settings.FRONTEND_BASE_URL}/api/{settings.API_VERSION}/auth/oauth/google/callback?"
#             f"{urlencode(query_params)}"
#         )
#         return RedirectResponse(url=redirect_url)

#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error(f"Google OAuth 콜백 처리 실패: {str(e)}")
#         raise HTTPException(
#             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
#             detail="OAuth 콜백 처리에 실패했습니다",
#         )


@router.get("/drive/callback", response_class=HTMLResponse)
async def drive_token_callback(code: str, state: str):
    """드라이브 전용 토큰 발급을 위한 간단한 콜백 페이지.
    브라우저 주소창의 code 값을 복사해 콘솔 스크립트 2번 단계에 붙여넣으면 됩니다.
    """
    html = """
    <html>
      <head><meta charset='utf-8'><title>Google Drive Token Callback</title></head>
      <body style="font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; padding: 24px;">
        <h2>드라이브 토큰 발급 콜백</h2>
        <p>아래 <b>code</b> 값을 복사해 터미널의 스크립트(2번 단계)에 붙여넣으세요.</p>
        <pre style="background:#f5f5f5;padding:12px;border-radius:8px;">code: {code}</pre>
        <pre style="background:#f5f5f5;padding:12px;border-radius:8px;">state: {state}</pre>
      </body>
    </html>
    """.format(code=code or "(없음)", state=state or "(없음)")
    return HTMLResponse(content=html)


@router.post("/google/callback")
async def google_oauth_callback_post(request: GoogleOAuthBody, http_request: Request, response: Response, db: Session = Depends(get_db)):
    """Google OAuth 콜백 처리 (POST)"""
    try:
        authorizationCode = request.authorizationCode
        codeVerifier = request.codeVerifier

        redirect_uri = request.redirectUri
        client_type = _detect_client_type(http_request, redirect_uri)
        if not redirect_uri:
            redirect_uri = {
                "web": settings.GOOGLE_WEB_REDIRECT_URI,
                "android": "",
                "ios": "",
            }[client_type]

        if client_type == "web" and not codeVerifier:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="웹 OAuth 요청에는 codeVerifier가 필요합니다",
            )

        session_policy = "app_persistent" if _is_app_client(client_type) else "web_default"
        refresh_expire_delta = _get_refresh_expire_delta(client_type)

        logger.info(
            f"Google OAuth callback using redirect_uri: {redirect_uri}, "
            f"client_type={client_type}, session_policy={session_policy}"
        )

        logger.info(f"Google OAuth callback: "
                    f"code={authorizationCode[:10]}..., "
                    f"verifier_len={len(codeVerifier) if codeVerifier else 0}")

        oauth_token_data = google_oauth.exchange_code_for_token(
            authorizationCode=authorizationCode,
            codeVerifier=codeVerifier,
            redirect_uri=redirect_uri,
            client_type=client_type,
        )

        print("!!!!!!!!!!!!!token_data:", oauth_token_data)

        # 사용자 정보 조회
        google_user = google_oauth.get_user_info(oauth_token_data["access_token"])

        # 사용자 생성 또는 조회
        user, is_new_user = await create_or_get_oauth_user(google_user, db)

        # # 필수 약관 동의 여부 확인
        # terms_agreed = getattr(user, 'terms_agreement', False)
        # privacy_agreed = getattr(user, 'privacy_policy', False)
        # collection_agreed = getattr(user, 'privacy_collection', False)

        # if not (terms_agreed and privacy_agreed and collection_agreed):
        #     # 약관 미동의 시 약관 동의 전용 토큰 발급 (약관 동의 API 접근용)
        #     logger.warning(
        #         f"필수 약관 미동의로 정상 로그인 차단: {user.email}, "
        #         f"is_new_user={is_new_user}, "
        #         f"terms_agreement={terms_agreed}, "
        #         f"privacy_policy={privacy_agreed}, "
        #         f"privacy_collection={collection_agreed}"
        #     )

        #     # 약관 동의 전용 토큰 생성 (약관 동의 API에만 사용 가능)
        #     terms_agreement_token_payload = {
        #         "id": user.id,
        #         "email": user.email,
        #         "nickname": user.nickname,
        #         "role": "USER",
        #         "provider": user.provider.value if user.provider else None,
        #         "terms_agreement_only": True  # 약관 동의 전용 토큰 플래그
        #     }

        #     terms_agreement_token = jwt_auth.create_access_token(terms_agreement_token_payload)

        #     # 약관 미동의 시 약관 동의 전용 토큰을 헤더에 포함하여 반환
        #     from fastapi.responses import JSONResponse

        #     response = JSONResponse(
        #         status_code=status.HTTP_403_FORBIDDEN,
        #         content={
        #             "detail": "필수 약관에 동의하지 않아 로그인할 수 없습니다. 약관 동의를 완료해주세요.",
        #             "requires_terms_agreement": True,
        #             "terms_agreement_token": terms_agreement_token,  # 프론트엔드 호환성을 위해 본문에도 포함
        #             "user_id": user.id
        #         }
        #     )
        #     response.headers["X-Terms-Agreement-Token"] = terms_agreement_token
        #     return response

        # if not (terms_agreed and privacy_agreed and collection_agreed):
        #     raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
        #                         detail={
        #                             "code": "TERMS_NOT_AGREED",
        #                             "message": "필수 약관 동의 필요"
        #                         })

        # JWT 토큰 생성
        jwt_payload = {
            "id": user.id,
            "email": user.email,
            "nickname": user.nickname,
            "role": "USER",  # User 모델에는 role이 없으므로 항상 USER로 설정
            "provider": user.provider.value if user.provider else None,
            "client_type": client_type,
            "session_policy": session_policy,
        }

        access_token = jwt_auth.create_access_token(jwt_payload)
        refresh_token = jwt_auth.create_refresh_token(jwt_payload, expires_delta=refresh_expire_delta)

        logger.info(f"Google OAuth 로그인 성공: {user.email}")

        if request.push_token:
            sync_user_push_token(db=db,
                                 user_id=user.id,
                                 push_token=request.push_token,
                                 token_type=request.token_type or "FCM",
                                 enabled=request.enabled if request.enabled is not None else True)

        if not _is_app_client(client_type):
            set_auth_cookies(response,
                             access_token=access_token,
                             refresh_token=refresh_token,
                             refresh_max_age=int(refresh_expire_delta.total_seconds()))
            # 웹 SPA도 Bearer 토큰으로 API 호출할 수 있도록 본문에 토큰 포함
            return {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "bearer",
                "expires_in": jwt_auth.expire_minutes * 60,
                "refresh_expires_in": int(refresh_expire_delta.total_seconds()),
                "session_policy": session_policy,
                "client_type": client_type,
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "nickname": user.nickname,
                    "role": "USER",  # User 모델에는 role이 없으므로 항상 USER로 설정
                    "status": user.status.value if user.status else "ACTIVE",
                    "provider": user.provider.value,
                    "created_at": (user.created_at.isoformat() if user.created_at is not None else None),
                    "updated_at": (user.updated_at.isoformat() if user.updated_at is not None else None),
                    "profile_image": user.profile_image,
                    "phone": user.phone_number,
                    "needs_terms_agreement":
                    user.needs_terms_agreement if hasattr(user, 'needs_terms_agreement') else False,
                    "terms_agreement": user.terms_agreement if hasattr(user, 'terms_agreement') else False,
                    "privacy_policy": user.privacy_policy if hasattr(user, 'privacy_policy') else False,
                    "privacy_collection": user.privacy_collection if hasattr(user, 'privacy_collection') else False,
                    "marketing_consent": user.marketing_consent if hasattr(user, 'marketing_consent') else False,
                },
                "is_new_user": is_new_user,
            }

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": jwt_auth.expire_minutes * 60,
            "refresh_expires_in": int(refresh_expire_delta.total_seconds()),
            "session_policy": session_policy,
            "client_type": client_type,
            "user": {
                "id": user.id,
                "email": user.email,
                "nickname": user.nickname,
                "role": "USER",  # User 모델에는 role이 없으므로 항상 USER로 설정
                "status": user.status.value if user.status else "ACTIVE",
                "provider": user.provider.value,
                "created_at": (user.created_at.isoformat() if user.created_at is not None else None),
                "updated_at": (user.updated_at.isoformat() if user.updated_at is not None else None),
                "profile_image": user.profile_image,
                "phone": user.phone_number,
                "needs_terms_agreement":
                user.needs_terms_agreement if hasattr(user, 'needs_terms_agreement') else False,
                "terms_agreement": user.terms_agreement if hasattr(user, 'terms_agreement') else False,
                "privacy_policy": user.privacy_policy if hasattr(user, 'privacy_policy') else False,
                "privacy_collection": user.privacy_collection if hasattr(user, 'privacy_collection') else False,
                "marketing_consent": user.marketing_consent if hasattr(user, 'marketing_consent') else False,
            },
            "is_new_user": is_new_user,
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error(f"Google OAuth 콜백 처리 실패: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OAuth 콜백 처리에 실패했습니다",
        )


def _build_user_payload(user: User) -> dict[str, Any]:
    """로그인 응답의 user 블록 생성"""
    return {
        "id": user.id,
        "email": user.email,
        "nickname": user.nickname,
        "role": "USER",  # User 모델에는 role이 없으므로 항상 USER로 설정
        "status": user.status.value if user.status else "ACTIVE",
        "provider": user.provider.value if user.provider else None,
        "created_at": (user.created_at.isoformat() if user.created_at is not None else None),
        "updated_at": (user.updated_at.isoformat() if user.updated_at is not None else None),
        "profile_image": user.profile_image,
        "phone": user.phone_number,
        "needs_terms_agreement": getattr(user, 'needs_terms_agreement', False),
        "terms_agreement": getattr(user, 'terms_agreement', False),
        "privacy_policy": getattr(user, 'privacy_policy', False),
        "privacy_collection": getattr(user, 'privacy_collection', False),
        "marketing_consent": getattr(user, 'marketing_consent', False),
    }


@router.post("/apple/callback")
async def apple_oauth_callback_post(request: AppleOAuthBody,
                                    http_request: Request,
                                    response: Response,
                                    db: Session = Depends(get_db)):
    """
    Sign in with Apple 콜백 처리 (POST)

    네이티브 앱이 Apple SDK로 받은 identity token을 그대로 전달하면
    서버가 서명을 검증하고 자체 JWT를 발급한다.
    """
    try:
        client_type = _detect_client_type(http_request, None)
        session_policy = "app_persistent" if _is_app_client(client_type) else "web_default"
        refresh_expire_delta = _get_refresh_expire_delta(client_type)

        try:
            apple_user = apple_oauth.get_user_info(
                identity_token=request.identityToken,
                full_name=request.fullName,
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

        user, is_new_user = await create_or_get_oauth_user(apple_user, db, provider=Provider.APPLE)
        _log_oauth_login(user, is_new_user, "APPLE")

        jwt_payload = {
            "id": user.id,
            "email": user.email,
            "nickname": user.nickname,
            "role": "USER",
            "provider": user.provider.value if user.provider else None,
            "client_type": client_type,
            "session_policy": session_policy,
        }

        access_token = jwt_auth.create_access_token(jwt_payload)
        refresh_token = jwt_auth.create_refresh_token(jwt_payload, expires_delta=refresh_expire_delta)

        logger.info(f"Apple 로그인 성공: user_id={user.id}, is_new_user={is_new_user}")

        if request.push_token:
            sync_user_push_token(db=db,
                                 user_id=user.id,
                                 push_token=request.push_token,
                                 token_type=request.token_type or "FCM",
                                 enabled=request.enabled if request.enabled is not None else True)

        if not _is_app_client(client_type):
            set_auth_cookies(response,
                             access_token=access_token,
                             refresh_token=refresh_token,
                             refresh_max_age=int(refresh_expire_delta.total_seconds()))

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": jwt_auth.expire_minutes * 60,
            "refresh_expires_in": int(refresh_expire_delta.total_seconds()),
            "session_policy": session_policy,
            "client_type": client_type,
            "user": _build_user_payload(user),
            "is_new_user": is_new_user,
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error(f"Apple 로그인 처리 실패: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Apple 로그인 처리에 실패했습니다",
        )


def _log_oauth_login(user: User, is_new_user: bool, provider: str) -> None:
    if is_new_user:
        activity.log(user.id, "SIGNUP", "회원 가입", meta={"provider": provider})
    activity.log(user.id, "LOGIN", "로그인", meta={"provider": provider})


async def create_or_get_oauth_user(oauth_user: OAuthUserInfo,
                                   db: Session,
                                   provider: Provider = Provider.GOOGLE) -> tuple[User, bool]:
    """OAuth 사용자 생성 또는 조회"""
    try:
        # 기존 사용자 조회 (provider_id 우선, 없으면 이메일로)
        # Apple은 이메일 가리기(private relay)를 쓸 수 있어 provider_id가 더 안정적인 식별자다.
        existing_user = (db.query(User).filter(User.provider_id == oauth_user.provider_id,
                                               User.provider == provider).first())

        if not existing_user and oauth_user.email:
            existing_user = db.query(User).filter(User.email == oauth_user.email).first()

        if existing_user:
            provider = cast(Provider, existing_user.provider)
            # 기존 사용자가 있는 경우
            if provider == Provider.LOCAL:
                # 로컬 계정이 있는 경우 - 자동 변환하지 않고 에러 반환
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="이미 일반 계정으로 가입된 이메일입니다. 일반 로그인을 이용하세요.",
                )

                # [주석 처리] 자동 계정 변환 로직 (향후 필요시 활성화)
                # existing_user.provider = Provider.GOOGLE
                # existing_user.provider_id = oauth_user.provider_id
                # existing_user.email_verified = oauth_user.email_verified
                # if oauth_user.profile_image:
                #     existing_user.profile_image = oauth_user.profile_image
                #
                # # 약관 동의가 없는 경우 기본 동의 처리
                # if existing_user.needs_terms_agreement:
                #     existing_user.needs_terms_agreement = False
                #     existing_user.terms_agreement = True
                #     existing_user.privacy_policy = True
                #     existing_user.privacy_collection = True
                #     # marketing_consent는 기존 값 유지
                #
                # db.commit()
                # db.refresh(existing_user)
                # logger.info(f"기존 로컬 계정을 Google OAuth와 연결: {existing_user.email}")
            else:
                # 이미 OAuth 계정인 경우
                # 상태 값을 안전하게 문자열로 변환
                try:
                    user_status_str = (existing_user.status.value
                                       if hasattr(existing_user.status, "value") else str(existing_user.status))
                except:
                    user_status_str = str(existing_user.status)

                logger.info(f"OAuth 사용자 상태 확인: {existing_user.email}, status={user_status_str}")

                # 비활성화된 사용자 체크
                if user_status_str == "DEACTIVATED":
                    logger.warning(f"비활성화된 사용자 로그인 시도: {existing_user.email}")
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="관리자에 의해 비활성화 처리된 회원입니다.",
                    )

                # 삭제된 사용자 체크
                if user_status_str == "DELETED":
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="탈퇴한 계정입니다.",
                    )

                # 활성 사용자만 로그인 가능
                if user_status_str != "ACTIVE":
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="로그인할 수 없는 계정 상태입니다.",
                    )

                # 정보 업데이트
                if oauth_user.picture:
                    existing_user.profile_image = oauth_user.picture
                if oauth_user.verified_email:
                    existing_user.email_verified = datetime.now()

                db.commit()
                db.refresh(existing_user)

                # 약관 동의 상태 확인 및 로깅
                needs_terms = getattr(existing_user, 'needs_terms_agreement', False)
                terms_agreed = getattr(existing_user, 'terms_agreement', False)
                privacy_agreed = getattr(existing_user, 'privacy_policy', False)
                collection_agreed = getattr(existing_user, 'privacy_collection', False)

                logger.info(f"기존 OAuth 계정 정보 업데이트: {existing_user.email}, "
                            f"needs_terms_agreement={needs_terms}, "
                            f"terms_agreement={terms_agreed}, "
                            f"privacy_policy={privacy_agreed}, "
                            f"privacy_collection={collection_agreed}")

                # 필수 약관 동의 여부 확인 - 로그인 차단
                # (약관 미동의는 상위 함수에서 통합 처리하므로 여기서는 체크만 하고 예외는 발생시키지 않음)
                if not (terms_agreed and privacy_agreed and collection_agreed):
                    logger.warning(f"필수 약관 미동의 감지 (상위에서 처리): {existing_user.email}, "
                                   f"terms_agreement={terms_agreed}, "
                                   f"privacy_policy={privacy_agreed}, "
                                   f"privacy_collection={collection_agreed}")
                    # 약관 미동의는 상위 함수에서 통합 처리하므로 여기서는 그냥 넘어감

            return existing_user, False  # 기존 사용자

        # 새 사용자 생성
        # Apple 로그인은 이메일 가리기 사용 시에도 relay 주소가 내려오지만,
        # 만약 비어 있으면 provider_id 기반 placeholder로 unique 제약을 지킨다.
        email = oauth_user.email or f"{oauth_user.provider_id}@{oauth_user.provider}.local"

        # OAuth 사용자는 닉네임 유효성 검사 면제, 원본 그대로 사용
        base_nickname = oauth_user.name or "user"

        # 닉네임이 비어있거나 너무 짧으면 기본값 사용
        if not base_nickname or len(base_nickname.strip()) < 1:
            base_nickname = "user"

        nickname = base_nickname.strip()
        counter = 1

        # 중복만 확인하고 유효성 검사는 면제
        while db.query(User).filter(User.nickname == nickname).first():
            nickname = f"{base_nickname}{counter}"
            counter += 1

        new_user = User(
            email=email,
            nickname=nickname,
            provider=provider,
            provider_id=oauth_user.provider_id,
            email_verified=datetime.now() if oauth_user.verified_email else None,
            profile_image=oauth_user.picture,
            status=UserStatus.ACTIVE,
            # OAuth 사용자 약관 동의 처리 (회원가입 후 약관 동의 방식)
            needs_terms_agreement=True,  # 약관 동의 필요 (아직 동의 안 함)
            terms_agreement=False,  # 서비스이용약관 동의 (미동의)
            privacy_policy=False,  # 개인정보처리방침 동의 (미동의)
            privacy_collection=False,  # 개인정보 수집 및 이용동의 (미동의)
            marketing_consent=False,  # 마케팅정보 수신동의 (선택 약관, 미동의)
        )

        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        logger.info(f"새 {provider.value} OAuth 사용자 생성: {new_user.email}")
        return new_user, True  # 새 사용자

    except HTTPException as he:
        # HTTPException은 그대로 전달
        logger.info(f"HTTPException 전달: status_code={he.status_code}, detail={he.detail}")
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"OAuth 사용자 생성/조회 실패: {type(e).__name__}: {str(e)}")
        # HTTPException이 아닌 다른 예외인 경우에만 일반 에러 메시지 반환
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="사용자 생성/조회에 실패했습니다",
        )


# @router.get("/providers")
# async def get_oauth_providers():
#     """지원하는 OAuth 제공자 목록 조회"""
#     return {
#         "providers": [
#             {
#                 "name": "google",
#                 "display_name": "Google",
#                 "enabled": bool(google_oauth.client_id),
#                 "auth_url": "/auth/oauth/google" if google_oauth.client_id else None,
#             }
#         ]
#     }
