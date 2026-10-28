"""
백오피스 목록 공통 계약 (backoffice docs/02.기획문서/API스펙.md 1.1·1.2)

요청: page(1-base), size(20·50·100), sort(`field` 또는 `-field`), q
응답: {"items": [...], "total": n, "page": p, "size": s}
일괄: {"ids": [...], "action": "...", "reason": "..."} → {"succeeded": [...], "failed": [{"id", "message"}]}
"""
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Query, status
from pydantic import BaseModel, Field

from utils import audit

PAGE_SIZES = (20, 50, 100)
BULK_MAX = 100


class PageParams:
    """FastAPI 의존성. 정규화 전 화면이 보내는 limit 도 받아 준다"""

    def __init__(
        self,
        page: int = Query(1, ge=1),
        size: int | None = Query(None),
        limit: int | None = Query(None),
        sort: str | None = Query(None, max_length=50),
        q: str | None = Query(None, max_length=100),
    ):
        chosen = size or limit or PAGE_SIZES[0]
        if chosen not in PAGE_SIZES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"size 는 {PAGE_SIZES} 중 하나여야 합니다")
        self.page = page
        self.size = chosen
        self.sort = sort
        self.q = q.strip() if q and q.strip() else None

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


def apply_sort(query, sort: str | None, allowed: dict[str, Any], default):
    """허용 목록에 없는 정렬 필드는 422 (임의 컬럼 정렬 방지)"""
    if not sort:
        return query.order_by(*default)
    desc = sort.startswith("-")
    key = sort.lstrip("-")
    column = allowed.get(key)
    if column is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"정렬할 수 없는 필드입니다: {key}")
    return query.order_by(column.desc() if desc else column.asc(), *default[1:])


def paginate(query, params: PageParams, serialize: Callable[[Any], dict]) -> dict:
    total = query.order_by(None).count()
    rows = query.offset(params.offset).limit(params.size).all()
    return {"items": [serialize(r) for r in rows], "total": total, "page": params.page, "size": params.size}


class BulkRequest(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=BULK_MAX)
    action: str
    reason: str | None = Field(None, max_length=500)
    status: str | None = None


def bulk_run(ids: list[int], fn: Callable[[int], None], audit_as: tuple[str, str] | None = None) -> dict:
    """건별 독립 처리. fn 이 HTTPException 이나 ValueError 를 던지면 실패 목록에 담는다.
    audit_as=(action, target_type) 를 주면 실패 건도 감사 로그에 FAIL 로 남긴다 (성공 건은 fn 이 남긴다)"""
    succeeded: list[int] = []
    failed: list[dict] = []
    for i in dict.fromkeys(ids):
        try:
            fn(i)
            succeeded.append(i)
        except HTTPException as e:
            failed.append({"id": i, "message": str(e.detail)})
        except ValueError as e:
            failed.append({"id": i, "message": str(e)})
        else:
            continue
        if audit_as:
            audit.record(audit_as[0], audit_as[1], i, result="FAIL", meta={"bulk": True, "message": failed[-1]["message"]})
    return {"succeeded": succeeded, "failed": failed}
