import logging

from firebase_admin import credentials, get_app, initialize_app, messaging
from sqlalchemy.orm import Session

from config import settings
from models import UserDeviceToken

logger = logging.getLogger(__name__)

_firebase_disabled = False


def _get_firebase_app():
    global _firebase_disabled

    if _firebase_disabled:
        return None

    try:
        return get_app()
    except ValueError:
        if not settings.FIREBASE_SERVICE_ACCOUNT_FILE:
            logger.warning("FIREBASE_SERVICE_ACCOUNT_FILE is not configured. Push delivery disabled.")
            _firebase_disabled = True
            return None

        try:
            cred = credentials.Certificate(settings.FIREBASE_SERVICE_ACCOUNT_FILE)
            app = initialize_app(cred)
            logger.info("Firebase app initialized for push delivery")
            return app
        except Exception as e:
            logger.error(f"Firebase initialization failed: {str(e)}")
            _firebase_disabled = True
            return None


def send_push_to_user(
    db: Session,
    user_id: int,
    title: str,
    content: str,
    category: str,
    target_id: int | None = None,
    extra_data: dict[str, str] | None = None,
) -> int:
    app = _get_firebase_app()
    if app is None:
        logger.warning(f"Push skipped: firebase app unavailable (user_id={user_id})")
        return 0

    token_rows = db.query(UserDeviceToken).filter(
        UserDeviceToken.user_id == user_id,
        UserDeviceToken.is_active.is_(True),
        UserDeviceToken.token_type == "FCM",
    ).all()

    if not token_rows:
        logger.info(f"Push skipped: no active FCM tokens (user_id={user_id})")
        return 0

    data = {"category": category}
    if target_id is not None:
        data["target_id"] = str(target_id)
    if extra_data:
        data.update({k: str(v) for k, v in extra_data.items()})

    logger.info(
        f"Push send requested (user_id={user_id}, token_count={len(token_rows)}, category={category}, target_id={target_id})"
    )

    sent_count = 0
    for token_row in token_rows:
        try:
            message = messaging.Message(
                token=token_row.push_token,
                notification=messaging.Notification(title=title, body=content),
                data=data,
            )
            message_id = messaging.send(message, app=app)
            sent_count += 1
            logger.info(
                f"Push sent (user_id={user_id}, token_id={token_row.id}, message_id={message_id})"
            )
        except Exception as e:
            error_text = str(e)
            logger.error(f"Push send failed (user_id={user_id}, token_id={token_row.id}): {error_text}")

    logger.info(
        f"Push send finished (user_id={user_id}, sent_count={sent_count}, attempted={len(token_rows)})"
    )
    return sent_count
