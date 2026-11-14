# -*- coding: utf-8 -*-
"""앱 버전 문자열 비교.

"1.0.2" 같은 점 구분 숫자 버전을 비교한다. 문자열 비교로는 안 된다 -
"1.0.10" < "1.0.9" 로 뒤집힌다. 그래서 자리별 정수로 끊어서 본다.

숫자가 아닌 꼬리(1.0.2-beta.1 의 "-beta.1")는 버린다. 스토어에 올라가는
버전은 전부 숫자라 이 단순화로 충분하고, 파싱이 실패해 사용자를 잠그는
쪽이 훨씬 비싸다.
"""
import re

_NUMERIC = re.compile(r"^\d+")


def parse_version(value: str | None) -> tuple[int, ...] | None:
    """버전 문자열을 정수 튜플로 바꾼다. 못 읽으면 None."""
    if not value or not isinstance(value, str):
        return None

    parts: list[int] = []
    for chunk in value.strip().split("."):
        match = _NUMERIC.match(chunk)
        if not match:
            break
        parts.append(int(match.group()))

    return tuple(parts) if parts else None


def compare_versions(left: str | None, right: str | None) -> int | None:
    """left 가 right 보다 작으면 -1, 같으면 0, 크면 1. 못 읽으면 None.

    자리 수가 다르면 짧은 쪽을 0 으로 채운다. "1.0" == "1.0.0".
    """
    a = parse_version(left)
    b = parse_version(right)
    if a is None or b is None:
        return None

    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))

    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def is_outdated(current: str | None, minimum: str | None) -> bool:
    """current 가 minimum 보다 낮을 때만 True.

    버전을 못 읽으면 False 를 준다 - 판단이 안 서면 막지 않는다.
    차단을 잘못 걸면 사용자는 앱을 아예 못 쓰고, 우리는 그걸
    원격으로 되돌릴 방법이 (환경변수를 고치기 전까지) 없다.
    """
    result = compare_versions(current, minimum)
    return result == -1
