"""Internal loadtest-only APIs."""

import ipaddress
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import Gungu, Sido, Terms, TermsType
from routers.oauth import create_or_get_oauth_user
from schemas import OAuthUserInfo
from services.push_token_service import sync_user_push_token
from utils.jwt_auth import jwt_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/loadtest", tags=["internal-loadtest"])


DEFAULT_LOADTEST_SIDO_CODE = "11"
DEFAULT_LOADTEST_SIDO_NAME = "서울특별시"
DEFAULT_LOADTEST_GUNGU_CODE = "11010"
DEFAULT_LOADTEST_GUNGU_NAME = "종로구"

DEFAULT_TERMS_SEEDS = [
    {
        "type": TermsType.SERVICE,
        "title": "Service Terms (LOADTEST)",
        "content": "Default service terms for loadtest bootstrap",
        "is_required": True,
    },
    {
        "type": TermsType.PRIVACY,
        "title": "Privacy Policy (LOADTEST)",
        "content": "Default privacy policy for loadtest bootstrap",
        "is_required": True,
    },
    {
        "type": TermsType.PRIVACY_COLLECTION,
        "title": "Privacy Collection Consent (LOADTEST)",
        "content": "Default privacy collection consent for loadtest bootstrap",
        "is_required": True,
    },
    {
        "type": TermsType.MARKETING,
        "title": "Marketing Consent (LOADTEST)",
        "content": "Default marketing consent for loadtest bootstrap",
        "is_required": False,
    },
]


class LoadtestOAuthMockRequest(BaseModel):
    provider_id: str = Field(..., min_length=1, max_length=255)
    email: str = Field(..., min_length=3, max_length=255)
    name: str = Field(..., min_length=1, max_length=255)
    picture: str | None = None
    verified_email: bool = True
    push_token: str | None = None
    token_type: str | None = Field(default="FCM", max_length=32)
    enabled: bool | None = True


class LoadtestBootstrapResponse(BaseModel):
    regions: dict
    terms: dict


def _extract_client_ips(request: Request) -> list[str]:
    ips: list[str] = []

    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        for raw in forwarded_for.split(","):
            ip = raw.strip()
            if ip:
                ips.append(ip)

    client_host = request.client.host if request.client else None
    if client_host:
        ips.append(client_host)

    # Deduplicate while preserving order
    unique_ips: list[str] = []
    seen = set()
    for ip in ips:
        if ip in seen:
            continue
        seen.add(ip)
        unique_ips.append(ip)

    return unique_ips


def _is_allowed_client(request: Request) -> bool:
    allowed_ranges = settings.loadtest_auth_mock_allowed_ips_list
    if not allowed_ranges:
        return False

    client_ips = _extract_client_ips(request)
    if not client_ips:
        return False

    networks = []
    for raw in allowed_ranges:
        try:
            networks.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            logger.warning(f"Invalid LOADTEST_AUTH_MOCK_ALLOWED_IPS entry skipped: {raw}")

    if not networks:
        return False

    for client_ip in client_ips:
        try:
            ip_obj = ipaddress.ip_address(client_ip)
        except ValueError:
            continue

        for network in networks:
            if ip_obj in network:
                return True

    return False


def _ensure_loadtest_guard(request: Request) -> None:
    if not settings.ENABLE_LOADTEST_AUTH_MOCK:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Loadtest OAuth mock is disabled.",
        )

    if not _is_allowed_client(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client IP is not allowed for loadtest OAuth mock.",
        )


def _ensure_regions_seed(db: Session) -> dict:
    now = datetime.utcnow()

    created = 0
    activated = 0

    sido = db.query(Sido).filter(Sido.code == DEFAULT_LOADTEST_SIDO_CODE).first()
    if not sido:
        db.add(
            Sido(
                code=DEFAULT_LOADTEST_SIDO_CODE,
                name=DEFAULT_LOADTEST_SIDO_NAME,
                adpt_de=None,
                is_active=True,
                updated_at=now,
            )
        )
        created += 1
    else:
        if not sido.is_active:
            activated += 1
        sido.name = DEFAULT_LOADTEST_SIDO_NAME
        sido.is_active = True
        sido.updated_at = now

    gungu = db.query(Gungu).filter(Gungu.code == DEFAULT_LOADTEST_GUNGU_CODE).first()
    if not gungu:
        db.add(
            Gungu(
                code=DEFAULT_LOADTEST_GUNGU_CODE,
                name=DEFAULT_LOADTEST_GUNGU_NAME,
                adpt_de=None,
                is_active=True,
                updated_at=now,
            )
        )
        created += 1
    else:
        if not gungu.is_active:
            activated += 1
        gungu.name = DEFAULT_LOADTEST_GUNGU_NAME
        gungu.is_active = True
        gungu.updated_at = now

    return {
        "created": created,
        "activated": activated,
        "sido_code": DEFAULT_LOADTEST_SIDO_CODE,
        "gungu_code": DEFAULT_LOADTEST_GUNGU_CODE,
    }


def _ensure_terms_seed(db: Session) -> dict:
    created = 0
    existing = 0
    now = datetime.utcnow()

    for seed in DEFAULT_TERMS_SEEDS:
        existing_active = db.query(Terms).filter(
            Terms.type == seed["type"],
            Terms.is_active == True,
        ).first()
        if existing_active:
            existing += 1
            continue

        db.add(
            Terms(
                type=seed["type"],
                title=seed["title"],
                content=seed["content"],
                is_active=True,
                is_required=seed["is_required"],
                published_at=now,
                created_by=None,
            )
        )
        created += 1

    return {"created": created, "existing": existing}


@router.post("/oauth-mock")
async def oauth_mock_login(request_data: LoadtestOAuthMockRequest,
                           request: Request,
                           db: Session = Depends(get_db)):
    """Issue user tokens by mocking OAuth for load testing only."""
    _ensure_loadtest_guard(request)

    oauth_user = OAuthUserInfo(
        provider="google",
        provider_id=request_data.provider_id,
        email=request_data.email,
        name=request_data.name,
        picture=request_data.picture,
        verified_email=request_data.verified_email,
    )

    user, is_new_user = await create_or_get_oauth_user(oauth_user, db)

    jwt_payload = {
        "id": user.id,
        "email": user.email,
        "nickname": user.nickname,
        "role": "USER",
        "provider": user.provider.value if user.provider else None,
    }

    access_token = jwt_auth.create_access_token(jwt_payload)
    refresh_token = jwt_auth.create_refresh_token(jwt_payload)

    if request_data.push_token:
        sync_user_push_token(
            db=db,
            user_id=user.id,
            push_token=request_data.push_token,
            token_type=request_data.token_type or "FCM",
            enabled=request_data.enabled if request_data.enabled is not None else True,
        )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": jwt_auth.expire_minutes * 60,
        "user": {
            "id": user.id,
            "email": user.email,
            "nickname": user.nickname,
            "role": "USER",
            "status": user.status.value if user.status else "ACTIVE",
            "provider": user.provider.value if user.provider else None,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "updated_at": user.updated_at.isoformat() if user.updated_at else None,
            "profile_image": user.profile_image,
            "phone": user.phone_number,
            "needs_terms_agreement": getattr(user, "needs_terms_agreement", False),
            "terms_agreement": getattr(user, "terms_agreement", False),
            "privacy_policy": getattr(user, "privacy_policy", False),
            "privacy_collection": getattr(user, "privacy_collection", False),
            "marketing_consent": getattr(user, "marketing_consent", False),
        },
        "is_new_user": is_new_user,
    }


@router.post("/bootstrap", response_model=LoadtestBootstrapResponse)
async def bootstrap_loadtest(request: Request, db: Session = Depends(get_db)):
    """Prepare minimal seed data for load tests."""
    _ensure_loadtest_guard(request)

    regions_result = _ensure_regions_seed(db)
    terms_result = _ensure_terms_seed(db)
    db.commit()

    return {
        "regions": regions_result,
        "terms": terms_result,
    }
