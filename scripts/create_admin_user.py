#!/usr/bin/env python3
"""
관리자 계정 생성 스크립트 (첫 최고 관리자 만들기용)

    python scripts/create_admin_user.py --email owner@teeup.kr --name 대표 [--role SUPER_ADMIN]

비밀번호는 화면에 보이지 않게 입력받는다. 같은 이메일의 관리자가 있으면 아무것도 바꾸지 않고 끝낸다.
(예전 스크립트는 admin@teeup.kr 을 지우고 고정 비밀번호 admin123 으로 다시 만들었다)
"""
import argparse
import getpass
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal  # noqa: E402
from models import Admin, AdminRole, Provider, UserStatus  # noqa: E402
from routers.admin.admins import _check_password  # noqa: E402
from utils.passwords import hash_password  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="관리자 계정 생성")
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--role", default="SUPER_ADMIN", choices=[r.value for r in AdminRole])
    args = ap.parse_args()

    password = getpass.getpass("비밀번호: ")
    if password != getpass.getpass("비밀번호 확인: "):
        print("비밀번호가 일치하지 않습니다.")
        return 1
    try:
        _check_password(password, "password")
    except Exception as e:
        print(getattr(e, "detail", [{}])[0].get("message", str(e)))
        return 1

    db = SessionLocal()
    try:
        email = args.email.strip().lower()
        if db.query(Admin).filter(Admin.email == email, Admin.deleted_at.is_(None)).first():
            print(f"이미 있는 관리자입니다: {email} — 바꾸지 않았습니다.")
            return 1
        admin = Admin(email=email, password=hash_password(password), name=args.name.strip(), provider=Provider.LOCAL,
                      status=UserStatus.ACTIVE, role=AdminRole(args.role))
        db.add(admin)
        db.commit()
        print(f"관리자를 만들었습니다: {email} ({args.role}, id={admin.id})")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
