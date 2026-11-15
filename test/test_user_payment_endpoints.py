"""없는 모델·없는 컬럼을 읽던 /users 엔드포인트들을 지킨다.

막으려는 버그 (2026-10-29):
    routers/users.py 에 호출되면 반드시 500 이 나는 엔드포인트가 넷 있었다.
    넷 다 "모델에 없는 것"을 읽는다는 같은 원인이었고, 함수 안의 넓은
    except 가 그 사실을 서버 오류 문자열로 덮어써서 겉으로는 평범한
    500 으로만 보였다.

      1) GET  /users/notification-settings
      2) PUT  /users/notification-settings
         -> 함수 안에서 `from models import NotificationSettings` 를 하는데
            그런 모델이 없다. ImportError -> 500. 테이블도 DDL 도 없어서
            고칠 수가 없으므로 두 엔드포인트를 지웠다.

      3) GET  /users/payments
         -> `Payment.user_id` 로 거르는데 payments 모델에 user_id 가 없다.
            그 외에도 없는 컬럼을 8개 더 읽는다. 사용자와 결제를 잇는 실제
            컬럼이 무엇인지 모르는 채로 고치면 틀린 데이터를 쓰게 되므로
            이것도 지웠다.

      4) GET  /users/subscriptions
         -> subscriptions.status 는 Enum 이 아니라 String(50) 인데 `.value`
            를 읽었고, 모델에도 스키마에도 없는 canceled_at / cancel_reason
            을 넘겼다. 구독이 하나라도 있는 사용자만 터지고 없는 사용자는
            빈 배열을 받아서 오래 숨어 있었다. 이쪽은 고쳤다.

이 파일은 DB 도 서버도 띄우지 않는다. 모델·스키마·라우트 표만 본다.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENVIRONMENT", "development")

import pytest  # noqa: E402


def _paths():
    import main

    return set(main.app.openapi()["paths"])


def test_지운_엔드포인트는_다시_생기지_않는다():
    paths = _paths()
    assert "/api/v1/users/notification-settings" not in paths
    assert "/api/v1/users/payments" not in paths


def test_알림설정_모델은_아직_없다():
    """있으면 위 엔드포인트를 되살릴 수 있다는 뜻이므로 알려 준다."""
    with pytest.raises(ImportError):
        from models import NotificationSettings  # noqa: F401


def test_구독_상태는_Enum_이_아니라_문자열이다():
    """Enum 으로 바뀌면 status=subscription.status 를 .value 로 되돌려야 한다."""
    from models import Subscription

    assert Subscription.__table__.columns["status"].type.python_type is str


def test_구독_모델에_없는_컬럼을_응답에_넣지_않는다():
    """canceled_at / cancel_reason 은 backend #51 에서 모델에 생겼지만 앱 응답 스키마에는 여전히 없다"""
    from models import Subscription

    cols = {c.name for c in Subscription.__table__.columns}
    assert {"canceled_at", "cancel_reason"} <= cols

    src = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "routers", "users.py"),
        encoding="utf-8",
    ).read()
    assert "subscription.status.value" not in src
    assert "canceled_at=subscription.canceled_at" not in src
    assert "cancel_reason=subscription.cancel_reason" not in src


def test_구독_응답을_실제로_만들어_본다():
    import datetime as dt

    from models import Plan, Subscription
    from schemas.payment import SubscriptionResponse

    sub = Subscription(
        id=1, user_id=7, plan_id=2, status="ACTIVE",
        start_date=dt.datetime(2026, 10, 1),
        next_billing_date=dt.datetime(2026, 11, 1),
        end_date=None,
        created_at=dt.datetime(2026, 10, 1),
        updated_at=dt.datetime(2026, 10, 1),
    )
    plan = Plan(id=2, name="프리미엄")

    res = SubscriptionResponse(
        id=sub.id, user_id=sub.user_id, plan_id=sub.plan_id,
        plan_name=plan.name, status=sub.status,
        start_date=sub.start_date, end_date=sub.end_date,
        next_billing_date=sub.next_billing_date,
        created_at=sub.created_at, updated_at=sub.updated_at,
    )
    assert res.status == "ACTIVE"
    assert res.plan_name == "프리미엄"
