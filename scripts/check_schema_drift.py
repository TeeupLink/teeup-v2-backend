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

안전장치:
    - 수행하는 DDL 은 `ALTER TABLE ... ADD COLUMN` 뿐이다.
    - NOT NULL 이면서 기본값이 없는 컬럼은 기존 행을 채울 수 없으므로 건너뛰고 경고만 남긴다.
      (그런 컬럼은 사람이 기본값을 정해 직접 추가해야 한다)
    - 모델에 없고 DB 에만 있는 컬럼은 보고만 하고 건드리지 않는다.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.schema import CreateColumn  # noqa: E402

from database import Base, engine  # noqa: E402
import models  # noqa: F401,E402  (모든 모델을 Base.metadata 에 등록)


def column_ddl(column) -> str:
    """ALTER TABLE ADD COLUMN 에 쓸 컬럼 정의를 현재 DB 방언으로 만든다."""
    return str(CreateColumn(column).compile(dialect=engine.dialect))


def main() -> int:
    parser = argparse.ArgumentParser(description="모델과 DB 스키마 차이 확인")
    parser.add_argument("--apply", action="store_true", help="누락 컬럼을 실제로 추가한다")
    parser.add_argument("--table", action="append", default=None, help="검사할 테이블 (여러 번 지정 가능)")
    args = parser.parse_args()

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
        f"ALTER TABLE {table_name} ADD COLUMN {column_ddl(column)}"
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
