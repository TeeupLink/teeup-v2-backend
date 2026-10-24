"""응답 스키마를 만들 때 '필수 필드'를 빠뜨린 코드를 찾는다.

배경:
    라우터는 대부분 `except Exception` 으로 감싸여 있어서,
    응답 스키마 생성 시 pydantic ValidationError 가 나면
    그대로 500 "서버 내부 오류가 발생했습니다" 로 바뀐다.
    테스트로 안 잡히면 운영에서야 드러난다.
    (2026-10-24: ExpenseListResponse.total_pages 누락 → 정산 API 가 전부 500,
     MessageResponse.success 누락 → 회원탈퇴 API 가 500)

사용법:
    python scripts/check_response_fields.py          # 문제가 있으면 종료코드 1

주의:
    `**kwargs` 로 넘기거나 위치 인자를 쓰는 생성 지점은 정적으로 알 수 없어 건너뛴다.
    schemas 와 models 에 같은 이름이 있으면(예: InquiryResponse) 오탐이 날 수 있으므로,
    해당 파일이 그 이름을 models 에서 가져왔는지 확인해 제외한다.
"""

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schemas"
SCAN_DIRS = ["routers", "services", "utils"]


def collect_schemas():
    """스키마 클래스 이름 -> (직접 필수 필드, 부모 클래스 이름들)"""
    required, bases = {}, {}
    for path in SCHEMA_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            required[node.name] = {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.value is None  # 기본값이 없으면 pydantic v2 에서는 필수
            }
            bases[node.name] = [b.id for b in node.bases if isinstance(b, ast.Name)]
    return required, bases


def all_required(cls, required, bases, seen=None):
    """상속받은 필수 필드까지 합친다."""
    seen = seen or set()
    if cls in seen or cls not in required:
        return set()
    seen.add(cls)
    out = set(required[cls])
    for base in bases.get(cls, []):
        out |= all_required(base, required, bases, seen)
    return out


def names_imported_from_models(tree):
    """이 파일이 models 에서 가져온 이름들 (스키마와 이름이 겹치면 오탐이므로 제외)."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("models"):
            out |= {a.asname or a.name for a in node.names}
    return out


def main() -> int:
    required, bases = collect_schemas()
    findings = []

    for sub in SCAN_DIRS:
        for path in (ROOT / sub).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(), str(path))
            shadowed = names_imported_from_models(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                cls = node.func.id
                if cls not in required or cls in shadowed:
                    continue
                if node.args or any(k.arg is None for k in node.keywords):
                    continue  # 위치 인자 / **kwargs 는 정적으로 판단 불가
                missing = all_required(cls, required, bases) - {k.arg for k in node.keywords}
                if missing:
                    findings.append((str(path.relative_to(ROOT)), node.lineno, cls, sorted(missing)))

    if not findings:
        print("필수 필드를 빠뜨린 응답 생성 지점: 없음")
        return 0

    print(f"필수 필드를 빠뜨린 응답 생성 지점: {len(findings)}건")
    print("(이대로 호출되면 pydantic ValidationError → 500 이 된다)\n")
    for path, line, cls, missing in sorted(findings):
        print(f"  {path}:{line}  {cls}  누락={missing}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
