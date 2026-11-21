"""코드에서 참조하는 열거형 멤버가 실제로 존재하는지 정적으로 검사한다.

`MeetingStatus.DELETED` 처럼 **없는 멤버**를 읽으면 파이썬은 import 시점이 아니라
그 줄이 실행되는 순간 AttributeError 를 던진다. 라우터가 그 예외를
`except Exception` 으로 삼키고 500 으로 바꾸면, 로그를 열어 보기 전까지는
원인이 전혀 드러나지 않는다.

실제로 그랬다. `GET /meetings/rounding` 은

    Meeting.status != MeetingStatus.DELETED

한 줄 때문에 호출될 때마다 500 이었다. MeetingStatus 의 멤버는
SCHEDULED / IN_PROGRESS / COMPLETED / CANCELED 네 개뿐이고 DELETED 는 없다.
모임 삭제는 하드 삭제(`db.delete(meeting)`)라 애초에 그런 상태가 필요하지 않다.

이 검사는 그 한 줄이 아니라 **같은 유형 전체**를 막는다. 열거형 이름과 같은
지역 변수가 섞일 수 있으므로, 이름이 같은 열거형이 여러 모듈에 있으면
멤버 집합을 합집합으로 본다 (거짓 양성보다 놓치는 쪽을 택한다).
"""
from __future__ import annotations

import ast
import enum
import importlib
import pkgutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 열거형이 모여 있는 모듈들. 여기 없는 곳에 열거형을 새로 만들면 이 목록에 추가한다.
ENUM_MODULES = ("models.enums", "schemas.enums")

# Enum 이 기본으로 가지고 있거나 흔히 쓰는 비(非)멤버 속성
ENUM_BUILTINS = {
    "value", "name", "_value_", "_name_", "_missing_", "_member_map_",
    "_member_names_", "_value2member_map_", "__members__", "mro",
}

# 검사 대상에서 뺄 디렉터리
SKIP_DIRS = {".venv", "venv", "__pycache__", "alembic", ".git", "node_modules"}


def _collect_enums() -> dict[str, set[str]]:
    """{열거형 이름: 허용되는 속성 집합}. 같은 이름이 여러 곳에 있으면 합친다."""
    allowed: dict[str, set[str]] = {}
    for mod_name in ENUM_MODULES:
        mod = importlib.import_module(mod_name)
        for obj_name in dir(mod):
            obj = getattr(mod, obj_name)
            if not (isinstance(obj, type) and issubclass(obj, enum.Enum)):
                continue
            names = set(obj.__members__)
            # 클래스 본문에 직접 정의된 메서드·프로퍼티·클래스메서드도 허용한다
            for klass in obj.__mro__:
                if klass in (enum.Enum, object, str, int):
                    continue
                names |= {k for k in vars(klass) if not k.startswith("__")}
            allowed.setdefault(obj_name, set()).update(names | ENUM_BUILTINS)
    return allowed


ALLOWED = _collect_enums()


def _python_files() -> list[Path]:
    out = []
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        out.append(path)
    return sorted(out)


def _looks_like_member(attr: str) -> bool:
    """멤버 이름처럼 생겼는지. 메서드 호출(.value, .from_str)과 구분한다."""
    return attr.isupper() or (attr.replace("_", "").isupper() and attr.replace("_", ""))


def _bad_references(source: str, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not isinstance(node.value, ast.Name):
            continue
        enum_name = node.value.id
        if enum_name not in ALLOWED:
            continue
        if not _looks_like_member(node.attr):
            continue
        if node.attr in ALLOWED[enum_name]:
            continue
        bad.append(f"{filename}:{node.lineno}  {enum_name}.{node.attr}")
    return bad


def test_모든_열거형_멤버_참조가_실재한다():
    assert ALLOWED, "열거형을 하나도 못 찾았다 — ENUM_MODULES 를 확인한다"
    assert "MeetingStatus" in ALLOWED

    bad: list[str] = []
    for path in _python_files():
        bad += _bad_references(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))

    assert not bad, (
        "존재하지 않는 열거형 멤버를 참조한다. 이 줄이 실행되면 AttributeError 가 나고,\n"
        "라우터가 그걸 삼키면 해당 경로는 항상 500 이 된다:\n  "
        + "\n  ".join(bad)
    )


def test_검사기가_예전_패턴을_실제로_잡는다():
    """검사기 자체가 동작하는지 — 고친 뒤에도 이 테스트가 썩지 않도록."""
    나쁜_코드 = "Meeting.status != MeetingStatus.DELETED\n"
    좋은_코드 = "Meeting.status != MeetingStatus.CANCELED\n"

    assert _bad_references(나쁜_코드, "<샘플>") == ["<샘플>:1  MeetingStatus.DELETED"]
    assert _bad_references(좋은_코드, "<샘플>") == []


# ---------------------------------------------------------------------------
# 토스 응답 → 내부 열거형 변환
#
# parse_payment_status / parse_payment_method 는 **문자열**을 돌려주고 호출 측이
# PaymentStatus(...) / PaymentMethod(...) 로 감싼다. 그래서 매핑 표에 열거형에
# 없는 값이 하나라도 있으면 그 결제만 ValueError 로 죽는다 - 타입으로는 안 잡힌다.
# 실제로 "계좌이체" 가 "BANK_TRANSFER" 로 매핑돼 있었고 PaymentMethod 에는
# TRANSFER 만 있었다.
# ---------------------------------------------------------------------------
def test_토스_상태_매핑이_모두_PaymentStatus_값이다():
    from schemas.enums import PaymentStatus
    from utils.toss_payments import parse_payment_status

    토스_상태 = ["DONE", "CANCELED", "PARTIAL_CANCELED", "ABORTED", "EXPIRED", "전혀_모르는_값"]
    for s in 토스_상태:
        PaymentStatus(parse_payment_status(s))  # ValueError 면 실패

    assert parse_payment_status("DONE") == PaymentStatus.SUCCEEDED.value
    # 취소는 실패와 구분된다
    assert parse_payment_status("CANCELED") == PaymentStatus.CANCELED.value
    assert parse_payment_status("ABORTED") == PaymentStatus.FAILED.value
    assert parse_payment_status("전혀_모르는_값") == PaymentStatus.PENDING.value


def test_토스_결제수단_매핑이_모두_PaymentMethod_값이다():
    from schemas.enums import PaymentMethod
    from utils.toss_payments import parse_payment_method

    for m in ["카드", "가상계좌", "계좌이체", "", "전혀_모르는_값"]:
        PaymentMethod(parse_payment_method(m))  # ValueError 면 실패

    assert parse_payment_method("카드") == PaymentMethod.CARD.value
    assert parse_payment_method("가상계좌") == PaymentMethod.VIRTUAL_ACCOUNT.value
    assert parse_payment_method("계좌이체") == PaymentMethod.TRANSFER.value
    # 모르는 수단은 CARD 로 떨어뜨린다 (결제 기록 자체를 잃는 것보다 낫다)
    assert parse_payment_method("") == PaymentMethod.CARD.value


def test_models_와_schemas_의_PaymentStatus_가_같다():
    """두 모듈에 같은 이름의 열거형이 있어서 한쪽만 고치면 조용히 어긋난다."""
    import models.enums as me
    import schemas.enums as se

    assert set(me.PaymentStatus.__members__) == set(se.PaymentStatus.__members__)


def test_결제수단_마스킹이_모든_PaymentMethod_를_구분한다():
    """`_mask_payment_info` 는 `payment_method.value` 를 받는다. 열거형에 없는
    문자열과 비교하면 그 수단만 조용히 기본값으로 떨어진다 - 예전에
    "BANK_TRANSFER" 와 비교해서 TRANSFER 결제가 그랬다."""
    from routers.users import _mask_payment_info
    from schemas.enums import PaymentMethod

    기본값 = _mask_payment_info("알 수 없는 수단")
    마스크 = {m: _mask_payment_info(m.value) for m in PaymentMethod}

    떨어진_것 = [m.value for m, v in 마스크.items() if v == 기본값]
    assert not 떨어진_것, f"기본값으로 떨어지는 결제 수단: {떨어진_것}"
    assert len(set(마스크.values())) == len(PaymentMethod), "수단별 마스킹이 겹친다"
