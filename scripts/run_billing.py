"""정기결제 일일 청구 (backend #51). 매일 04:00 KST 1회 (Cloud Scheduler → Cloud Run Job).

    python scripts/run_billing.py

BILLING_ENABLED=false 면 아무것도 청구하지 않고 끝난다. 운영에 등록하는 것은 정기결제를 켜기로 한 뒤에 한다.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal  # noqa: E402
from services.billing.engine import run_due  # noqa: E402

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    db = SessionLocal()
    try:
        result = run_due(db)
        if result.get("skipped"):
            logging.info("BILLING_ENABLED=false — 청구하지 않음")
        else:
            logging.info(f"정기결제 결과 {result}")
    finally:
        db.close()
