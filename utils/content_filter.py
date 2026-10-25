"""
사용자 생성 콘텐츠(UGC) 금칙어 필터.

App Store Guideline 1.2 — "A method for filtering objectionable material from being posted".
클럽·모임·공지·규정·프로필 등 사용자가 작성하는 텍스트에 금칙어가 포함되면
저장 전에 400 으로 거부한다.

사용법 (라우터 데코레이터):
    @router.post("/", dependencies=[Depends(reject_banned_content)])
"""

import re
import unicodedata
from typing import Any
from collections.abc import Iterable

from fastapi import HTTPException, Request, status

# 금칙어 목록. 단순 부분 문자열 매칭이므로 오탐 가능성이 큰 단어("보지", "자지", "마약" 등)는 넣지 않는다.
# 운영 중 필요하면 여기에 추가한다 (재시작 필요).
BANNED_WORDS: list[str] = [
    # 욕설·비하
    "씨발", "시발", "씨팔", "씨1발", "ㅅㅂ", "ㅆㅂ", "개새끼", "개새기", "개색", "개쌔끼",
    "병신", "븅신", "ㅂㅅ", "좆", "존나", "졸라", "지랄", "ㅈㄹ", "미친놈", "미친년",
    "느금마", "니애미", "엠창", "새끼야", "썅", "닥쳐", "죽어라", "뒤져라", "장애인새끼",
    "한남충", "김치녀", "된장녀", "틀딱", "급식충", "맘충", "일베", "쪽바리", "짱깨",
    # 음란
    "섹스", "성관계", "야동", "포르노", "딸딸이", "야사", "성매매",
    "조건만남", "원조교제", "출장안마", "출장마사지",
    # 불법·사기
    "토토사이트", "사설토토", "카지노사이트", "바카라사이트", "먹튀", "대출문의",
    "작업대출", "대마초", "필로폰", "히로뽕",
    # 영문
    "fuck", "shit", "bitch", "asshole", "nigger", "faggot", "porn", "sex video",
]

# 문자 사이에 끼워 넣은 공백·기호·숫자를 무시하기 위한 정규화
_STRIP_RE = re.compile(r"[\s\.\,\-\_\*\!\?\~\^\#\@\$\%\&\(\)\[\]\{\}\/\\\|\'\"`+=:;<>]+")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = _STRIP_RE.sub("", text)
    return text.lower()


_NORMALIZED_BANNED = [(w, _normalize(w)) for w in BANNED_WORDS]


def find_banned_words(text: str) -> list[str]:
    """텍스트에 포함된 금칙어 목록을 반환한다 (없으면 빈 리스트)."""
    if not text:
        return []
    normalized = _normalize(text)
    if not normalized:
        return []
    return [orig for orig, norm in _NORMALIZED_BANNED if norm and norm in normalized]


def contains_banned_words(text: str) -> bool:
    return bool(find_banned_words(text))


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_strings(v)


def find_banned_words_in_payload(payload: Any) -> list[str]:
    """JSON 페이로드(dict/list) 안의 모든 문자열 값에서 금칙어를 찾는다."""
    found: list[str] = []
    for s in _iter_strings(payload):
        for w in find_banned_words(s):
            if w not in found:
                found.append(w)
    return found


async def reject_banned_content(request: Request) -> None:
    """
    FastAPI 의존성. 요청 JSON 본문에 금칙어가 있으면 400 을 반환한다.
    본문이 JSON 이 아니면(멀티파트 등) 검사하지 않는다.
    """
    if request.method not in ("POST", "PUT", "PATCH"):
        return
    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type:
        return
    try:
        payload = await request.json()
    except Exception:
        return
    found = find_banned_words_in_payload(payload)
    if found:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"부적절한 표현이 포함되어 있어 등록할 수 없습니다: {', '.join(found[:3])}",
        )
