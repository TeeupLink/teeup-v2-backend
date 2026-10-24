"""
SQLAlchemy 모델과 실제 DB 스키마를 비교해 누락된 테이블·컬럼을 찾는다.

배경:
    이 프로젝트는 기동 시 `Base.metadata.create_all()` 로 테이블을 만든다.
    create_all 은 **없는 테이블만 만들고, 기존 테이블에 컬럼을 추가하지 않는다.**
    그래서 모델에 컬럼을 추가한 뒤 배포하면 운영 DB 에는 그 컬럼이 없고,
    해당 테이블을 조회하는 API 가 통째로 500 을 뱉는다.
    (2026-09-24: `/api/v1/meetings/{id}/expenses` 가 모든 모임에서 500)

사용법:
    # 1) 진단만 (아무것도 바꾸지 않음)
    .venv/bin/python scripts/check_schema_drift.py

    # 2) 누락 컬럼 추가 (ADD COLUMN 만 수행. DROP·타입변경은 절대 하지 않는다)
    .venv/bin/python scripts/check_schema_drift.py --apply

    # 3) 특정 테이블만
    .venv/bin/python scripts/check_schema_drift.py --table expenses --table expense_items

접속 호스트:
    운영 서버의 .env 는 DATABASE_URL 의 호스트가 `host.docker.internal` 이다.
    이 이름은 컨테이너 안에서만 풀리므로 호스트 쉘에서 그냥 돌리면 연결이 실패한다.
    그래서 호스트명이 풀리지 않으면 127.0.0.1 로 한 번 더 시도한다.
    직접 지정하려면 `--db-url` 을 쓴다. (비밀번호가 쉘 히스토리에 남는 점 유의)

안전장치:
    - 수행하는 DDL 은 `ALTER TABLE ... ADD COLUMN` 뿐이다.
    - NOT NULL 이면서 기본값이 없는 컬럼은 기존 행을 채울 수 없으므로 건너뛰고 경고만 남긴다.
      (그런 컬럼은 사람이 기본값을 정해 직접 추가해야 한다)
    - 모델에 없고 DB 에만 있는 컬럼은 보고만 하고 건드리지 않는다.
"""

import argparse
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402
from sqlalchemy.schema import CreateColumn  # noqa: E402

from config import settings  # noqa: E402
from database import Base  # noqa: E402
import models  # noqa: F401,E402  (모든 모델을 Base.metadata 에 등록)

# 호스트명이 풀리지 않을 때 대신 시도해 볼 주소
HOST_FALLBACKS = ["127.0.0.1"]


def resolvable(host: str) -> bool:
    if not host:
        return True  # 소켓·파일 기반 (sqlite 등)
    try:
        socket.getaddrinfo(host, None)
        return True
    except socket.gaierror:
        return False


def build_engine(db_url: str):
    """DB 엔진을 만든다. 호스트명이 풀리지 않으면 127.0.0.1 로 대체해 본다."""
    url = make_url(db_url)
    if not resolvable(url.host):
        for fallback in HOST_FALLBACKS:
            candidate = url.set(host=fallback)
            print(
                f"[알림] 호스트 '{url.host}' 를 찾을 수 없어 '{fallback}' 로 접속을 시도합니다.\n"
                f"       (.env 의 DATABASE_URL 은 컨테이너 기준입니다)"
            )
            engine = create_engine(candidate, poolclass=NullPool)
            try:
                with engine.connect():
                    return engine
            except Exception as exc:  # noqa: BLE001  연결 실패면 다음 후보로
                print(f"       '{fallback}' 접속 실패: {exc.__class__.__name__}")
                engine.dispose()
        print(
            f"\n접속할 수 있는 DB 호스트를 찾지 못했습니다.\n"
            f"  - MySQL 이 이 서버에서 돌고 있는지: sudo ss -lntp | grep 3306\n"
            f"  - 컨테이너 안에서 돌리려면:      docker ps  로 컨테이너명을 찾아\n"
            f"    docker exec -it <컨테이너> python scripts/check_schema_drift.py\n"
            f"  - 주소를 직접 주려면:            --db-url 'mysql+pymysql://...'",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return create_engine(url, poolclass=NullPool)


def column_ddl(column, dialect) -> str:
    """ALTER TABLE ADD COLUMN 에 쓸 컬럼 정의를 현재 DB 방언으로 만든다."""
    return str(CreateColumn(column).compile(dialect=dialect))


def main() -> int:
    parser = argparse.ArgumentParser(description="모델과 DB 스키마 차이 확인")
    parser.add_argument("--apply", action="store_true", help="누락 컬럼을 실제로 추가한다")
    parser.add_argument("--table", action="append", default=None, help="검사할 테이블 (여러 번 지정 가능)")
    parser.add_argument("--db-url", default=None, help="DATABASE_URL 대신 쓸 접속 주소")
    args = parser.parse_args()

    engine = build_engine(args.db_url or settings.database_url)

    inspector = inspect(engine)
    db_tables = set(inspector.get_table_names())

    missing_tables = []
    missing_columns = []   # (table, column)
    unsafe_columns = []    # (table, column) — NOT NULL + 기본값 없음
    extra_columns = []     # (table, column_name) — DB 에만 있음

    for table_name, table in Base.metadata.tables.items():
        if args.table and table_name not in args.table:
            continue

        if table_name not in db_tables:
            missing_tables.append(table_name)
            continue

        db_cols = {c["name"] for c in inspector.get_columns(table_name)}
        model_cols = {c.name for c in table.columns}

        for column in table.columns:
            if column.name in db_cols:
                continue
            has_default = (
                column.default is not None
                or column.server_default is not None
                or column.nullable
            )
            if has_default:
                missing_columns.append((table_name, column))
            else:
                unsafe_columns.append((table_name, column))

        for name in sorted(db_cols - model_cols):
            extra_columns.append((table_name, name))

    print("=" * 60)
    print(f"DB: {engine.url.render_as_string(hide_password=True)}")
    print("=" * 60)

    if missing_tables:
        print("\n[없는 테이블] — 서버를 재기동하면 create_all 이 만든다")
        for t in missing_tables:
            print(f"  - {t}")

    if missing_columns:
        print("\n[없는 컬럼] — 추가 필요")
        for table_name, column in missing_columns:
            print(f"  - {table_name}.{column.name}  ({column.type})")
    else:
        print("\n[없는 컬럼] 없음")

    if unsafe_columns:
        print("\n[자동 추가 불가] NOT NULL 인데 기본값이 없다. 사람이 기본값을 정해 직접 추가할 것")
        for table_name, column in unsafe_columns:
            print(f"  - {table_name}.{column.name}  ({column.type})")

    if extra_columns:
        print("\n[DB 에만 있는 컬럼] 참고용. 건드리지 않는다")
        for table_name, name in extra_columns:
            print(f"  - {table_name}.{name}")

    if not missing_columns:
        return 0

    statements = [
        f"ALTER TABLE {table_name} ADD COLUMN {column_ddl(column, engine.dialect)}"
        for table_name, column in missing_columns
    ]

    if not args.apply:
        print("\n[실행할 DDL] --apply 를 붙이면 아래를 실행한다")
        for s in statements:
            print(f"  {s};")
        return 1

    print("\n[적용]")
    with engine.begin() as conn:
        for s in statements:
            print(f"  {s};")
            conn.execute(text(s))
    print("\n완료. 애플리케이션을 재기동하세요: sudo systemctl restart teeup-v2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
