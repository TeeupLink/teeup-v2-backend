# -*- coding: utf-8 -*-
"""앱 강제 업데이트 정책 조회.

스토어에 새 버전을 올려도 옛 버전을 쓰는 기기를 막을 방법이 없었다.
2026-09-27 빌드를 쓰는 기기에 노션 주소 변경이 반영되지 않았던 게 그
예다. OTA(expo-updates)로도 못 고친다 - 그 빌드엔 업데이트 설정 자체가
없어서 확인을 안 한다.

앱은 켜질 때와 포그라운드로 돌아올 때 이 엔드포인트를 부른다. 자기
네이티브 버전이 minimum_version 보다 낮으면 닫을 수 없는 안내를 띄우고
스토어로 보낸다.

값은 전부 환경변수다. 코드 재배포 없이 Cloud Run 환경변수만 바꿔서
차단 시점을 정한다. 기본값 "0.0.0" 은 아무도 막지 않는다 - 이 기능을
배포하는 것만으로 누가 잠기는 일은 없어야 한다.

주의: 이 차단은 "이 코드가 들어간 앱 버전부터" 동작한다. 그보다 낮은
버전에는 확인하는 코드가 없어 영원히 막을 수 없다.
"""
from fastapi import APIRouter, Query

from config import settings
from utils.app_version import is_outdated

router = APIRouter(prefix="/app", tags=["app"])

IOS = "ios"
ANDROID = "android"


def _policy(platform: str) -> dict:
    """플랫폼별 정책. 아는 플랫폼이 아니면 빈 정책(=차단 없음)."""
    if platform == IOS:
        return {
            "minimum_version": settings.APP_MIN_VERSION_IOS,
            "latest_version": settings.APP_LATEST_VERSION_IOS,
            "store_url": settings.APP_STORE_URL_IOS,
            "store_web_url": settings.APP_STORE_WEB_URL_IOS,
        }
    if platform == ANDROID:
        return {
            "minimum_version": settings.APP_MIN_VERSION_ANDROID,
            "latest_version": settings.APP_LATEST_VERSION_ANDROID,
            "store_url": settings.APP_STORE_URL_ANDROID,
            "store_web_url": settings.APP_STORE_WEB_URL_ANDROID,
        }

    # 웹은 항상 최신이 서빙되므로 막을 이유가 없다. 모르는 값도 같이 통과.
    return {
        "minimum_version": "",
        "latest_version": "",
        "store_url": "",
        "store_web_url": "",
    }


@router.get("/version")
def get_version_policy(
    platform: str = Query(..., description="ios | android | web"),
    version: str | None = Query(
        None, description="호출하는 앱의 현재 네이티브 버전. 주면 판정까지 돌려준다."
    ),
):
    """업데이트 정책을 돌려준다. 인증 없이 호출 가능하다.

    차단된 사용자는 로그인도 못 하는 상태일 수 있어서 인증을 걸면 안 된다.
    담고 있는 정보는 스토어 주소와 공개 버전뿐이라 숨길 것도 없다.
    """
    normalized = (platform or "").strip().lower()
    policy = _policy(normalized)

    # 판정은 서버에서 한다. 앱마다 비교 로직을 다시 구현하면 어긋난다.
    update_required = is_outdated(version, policy["minimum_version"])

    return {
        "platform": normalized,
        "current_version": version,
        "minimum_version": policy["minimum_version"],
        "latest_version": policy["latest_version"],
        "store_url": policy["store_url"],
        "store_web_url": policy["store_web_url"],
        "update_required": update_required,
        "message": settings.APP_UPDATE_MESSAGE,
    }
