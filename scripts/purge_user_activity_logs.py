"""회원 활동 로그 1년 보관 정리 (backend #50). 매일 1회 실행 (cron / Cloud Scheduler → Cloud Run Job).

    python scripts/purge_user_activity_logs.py
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal  # noqa: E402
from utils.activity import purge_expired  # noqa: E402

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    db = SessionLocal()
    try:
        n = purge_expired(db)
        logging.info(f"1년 지난 활동 로그 {n}건 삭제")
    finally:
        db.close()
