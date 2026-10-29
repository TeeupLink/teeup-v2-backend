"""
감사 로그 조회·내보내기·월간 점검 (API스펙 API-150~154, 화면 SCR-090). 권한: auditLogs (SUPER_ADMIN)
수정·삭제 API 는 두지 않는다.
"""
import csv
import io
import re
from datetime import datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import AdminAuditLog, AuditMonthlyReview
from utils import audit
from utils.datetime_utils import get_kst_now

from .deps import get_admin_user
from .pagination import PageParams, apply_sort, paginate

router = APIRouter(tags=["admin-audit-logs"])

SORTABLE = {"created_at": AdminAuditLog.created_at}
DEFAULT_DAYS = 7
MAX_DAYS = 366
EXPORT_MAX = 10000
VIEW_PII_DAILY_LIMIT = 100
# 월간 집계에서 '변경'으로 세지 않는 행위
_NON_CHANGE = ("VIEW_PII", "EXPORT", "LOGIN", "LOGOUT", "LOGIN_FAIL", "REVIEW")

_DATE = r"^\d{4}-\d{2}-\d{2}$"
_MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"


def _now() -> datetime:
    return get_kst_now().replace(tzinfo=None)


def _period(date_from: str | None, date_to: str | None) -> tuple[datetime, datetime]:
    """기간 미지정이면 최근 7일. 최대 1년"""
    end = datetime.combine(datetime.fromisoformat(date_to), time.max) if date_to else _now()
    start = datetime.combine(datetime.fromisoformat(date_from), time.min) if date_from else end - timedelta(days=DEFAULT_DAYS)
    if start > end:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="시작일이 종료일보다 늦습니다")
    if end - start > timedelta(days=MAX_DAYS):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="조회 기간은 최대 1년입니다")
    return start, end


def _filtered(
    db: Session,
    actor_id: int | None,
    action: list[str] | None,
    target_type: str | None,
    target_id: str | None,
    date_from: str | None,
    date_to: str | None,
    result: str | None,
    q: str | None,
):
    start, end = _period(date_from, date_to)
    query = db.query(AdminAuditLog).filter(AdminAuditLog.created_at >= start, AdminAuditLog.created_at <= end)
    if actor_id:
        query = query.filter(AdminAuditLog.actor_admin_id == actor_id)
    if action:
        query = query.filter(AdminAuditLog.action.in_(action))
    if target_type:
        query = query.filter(AdminAuditLog.target_type == target_type)
    if target_id:
        query = query.filter(AdminAuditLog.target_id == target_id)
    if result:
        query = query.filter(AdminAuditLog.result == result)
    if q:
        query = query.filter(AdminAuditLog.actor_email.like(f"%{q}%") | AdminAuditLog.path.like(f"%{q}%"))
    return query


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="milliseconds") if value else None


def _item(log: AdminAuditLog) -> dict:
    return {
        "id": log.id,
        "request_id": log.request_id,
        "actor": {"id": log.actor_admin_id, "email": log.actor_email, "role": log.actor_role},
        "action": log.action,
        "target_type": log.target_type,
        "target_id": log.target_id,
        "reason": log.reason,
        "method": log.method,
        "path": log.path,
        "status_code": log.status_code,
        "result": log.result,
        "ip": log.ip,
        "created_at": _iso(log.created_at),
    }


class _Filters:
    def __init__(
        self,
        actor_id: int | None = Query(None),
        action: list[str] | None = Query(None),
        target_type: str | None = Query(None, max_length=40),
        target_id: str | None = Query(None, max_length=64),
        date_from: str | None = Query(None, pattern=_DATE),
        date_to: str | None = Query(None, pattern=_DATE),
        result: str | None = Query(None, pattern="^(SUCCESS|FAIL)$"),
    ):
        self.values = {
            "actor_id": actor_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "date_from": date_from,
            "date_to": date_to,
            "result": result,
        }


@router.get("/audit-logs")
async def list_audit_logs(
    params: PageParams = Depends(),
    filters: _Filters = Depends(),
    db: Session = Depends(get_db),
    _: dict = Depends(get_admin_user),
):
    query = _filtered(db, q=params.q, **filters.values)
    query = apply_sort(query, params.sort, SORTABLE, default=(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc()))
    return paginate(query, params, _item)


@router.get("/audit-logs/export")
async def export_audit_logs(
    reason: str = Query(..., min_length=1, max_length=500),
    q: str | None = Query(None, max_length=100),
    filters: _Filters = Depends(),
    db: Session = Depends(get_db),
    _: dict = Depends(get_admin_user),
):
    """CSV 내보내기. 사유가 필요하고, 내보내기 자체도 감사 로그(EXPORT)에 남는다"""
    rows = (
        _filtered(db, q=q, **filters.values)
        .order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc())
        .limit(EXPORT_MAX + 1)
        .all()
    )
    if len(rows) > EXPORT_MAX:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"한 번에 {EXPORT_MAX}건까지 내보낼 수 있습니다. 기간을 줄여 주세요"
        )
    audit.record("EXPORT", "AUDIT", None, reason=reason, meta={"count": len(rows), "filters": {**filters.values, "q": q}})

    buf = io.StringIO()
    buf.write("﻿")  # 엑셀이 UTF-8 로 읽도록
    w = csv.writer(buf)
    w.writerow(["id", "일시", "관리자", "역할", "행위", "대상", "대상 ID", "사유", "메서드", "경로", "응답", "결과", "IP", "요청 ID"])
    for r in rows:
        w.writerow([
            r.id, _iso(r.created_at), r.actor_email, r.actor_role, r.action, r.target_type, r.target_id, r.reason,
            r.method, r.path, r.status_code, r.result, r.ip, r.request_id,
        ])
    filename = f"audit-logs-{_now():%Y%m%d-%H%M}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _month_range(month: str) -> tuple[datetime, datetime]:
    year, mon = (int(x) for x in month.split("-"))
    start = datetime(year, mon, 1)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1)
    return start, end


def monthly_summary(db: Session, month: str) -> dict:
    start, end = _month_range(month)
    in_month = (AdminAuditLog.created_at >= start, AdminAuditLog.created_at < end)

    def count(*conds) -> int:
        return db.query(func.count(AdminAuditLog.id)).filter(*in_month, *conds).scalar() or 0

    totals = {
        "view_pii": count(AdminAuditLog.action == "VIEW_PII"),
        "export": count(AdminAuditLog.action == "EXPORT"),
        "login_fail": count(AdminAuditLog.action == "LOGIN_FAIL"),
        "changes": count(AdminAuditLog.action.notin_(_NON_CHANGE)),
    }

    by_admin: dict[int, dict] = {}
    rows = (
        db.query(AdminAuditLog.actor_admin_id, AdminAuditLog.actor_email, AdminAuditLog.action, func.count(AdminAuditLog.id))
        .filter(*in_month, AdminAuditLog.actor_admin_id.isnot(None))
        .group_by(AdminAuditLog.actor_admin_id, AdminAuditLog.actor_email, AdminAuditLog.action)
        .all()
    )
    for admin_id, email, action, n in rows:
        a = by_admin.setdefault(
            admin_id, {"admin_id": admin_id, "email": email, "view_pii": 0, "export": 0, "changes": 0, "max_daily_view_pii": 0}
        )
        if action == "VIEW_PII":
            a["view_pii"] += n
        elif action == "EXPORT":
            a["export"] += n
        elif action not in _NON_CHANGE:
            a["changes"] += n

    day = func.date(AdminAuditLog.created_at)
    daily = (
        db.query(AdminAuditLog.actor_admin_id, day, func.count(AdminAuditLog.id))
        .filter(*in_month, AdminAuditLog.action == "VIEW_PII", AdminAuditLog.actor_admin_id.isnot(None))
        .group_by(AdminAuditLog.actor_admin_id, day)
        .all()
    )
    for admin_id, _, n in daily:
        if admin_id in by_admin:
            by_admin[admin_id]["max_daily_view_pii"] = max(by_admin[admin_id]["max_daily_view_pii"], n)
    for a in by_admin.values():
        a["flagged"] = a["max_daily_view_pii"] > VIEW_PII_DAILY_LIMIT

    return {
        "month": month,
        "totals": totals,
        "by_admin": sorted(by_admin.values(), key=lambda a: (-a["view_pii"], a["admin_id"])),
    }


def _review_dict(r: AuditMonthlyReview | None) -> dict | None:
    if r is None:
        return None
    return {"reviewer_admin_id": r.reviewer_admin_id, "memo": r.memo, "reviewed_at": _iso(r.reviewed_at)}


@router.get("/audit-logs/monthly")
async def get_monthly(
    month: str = Query(..., pattern=_MONTH),
    db: Session = Depends(get_db),
    _: dict = Depends(get_admin_user),
):
    review = db.query(AuditMonthlyReview).filter(AuditMonthlyReview.month == month).first()
    return {**monthly_summary(db, month), "review": _review_dict(review)}


class ReviewBody(BaseModel):
    memo: str | None = Field(None, max_length=2000)


@router.post("/audit-logs/monthly/{month}/review")
async def review_month(
    month: str,
    body: ReviewBody,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """지난 달만 점검 완료로 기록할 수 있다. 한 달에 한 번"""
    if not re.match(_MONTH, month):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month 는 YYYY-MM 형식입니다")
    if month >= f"{_now():%Y-%m}":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="끝난 달만 점검할 수 있습니다")
    if db.query(AuditMonthlyReview).filter(AuditMonthlyReview.month == month).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 점검 완료된 달입니다")
    summary = monthly_summary(db, month)
    review = AuditMonthlyReview(
        month=month, reviewer_admin_id=current_user["id"], summary_json=summary, memo=body.memo, reviewed_at=_now()
    )
    db.add(review)
    db.commit()
    audit.record("REVIEW", "AUDIT", month, after={"totals": summary["totals"]}, reason=body.memo)
    return {**summary, "review": _review_dict(review)}


@router.get("/audit-logs/{log_id}")
async def get_audit_log(log_id: int, db: Session = Depends(get_db), _: dict = Depends(get_admin_user)):
    log = db.query(AdminAuditLog).filter(AdminAuditLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="감사 로그를 찾을 수 없습니다")
    related = (
        db.query(AdminAuditLog)
        .filter(AdminAuditLog.request_id == log.request_id, AdminAuditLog.id != log.id)
        .order_by(AdminAuditLog.id)
        .limit(100)
        .all()
    )
    return {
        **_item(log),
        "before": log.before_json,
        "after": log.after_json,
        "meta": log.meta_json,
        "user_agent": log.user_agent,
        "related": [_item(r) for r in related],
    }

