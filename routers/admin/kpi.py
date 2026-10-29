"""
백오피스 KPI API (backoffice #32, SCR-011, F-130) — 최고 관리자 전용(permissions "kpi")

GET /admin/kpi?from=YYYY-MM-DD&to=YYYY-MM-DD&interval=day|week|month

- 활성 회원(DAU·MAU)은 활동 로그의 LOGIN 으로 센다. 활동 로그를 모으기 전 구간은 0 이 아니라 null("데이터 없음")
- 신규 가입·클럽·라운딩·소셜은 각 테이블의 날짜로 센다 (라운딩·소셜은 모임일 기준)
- 매출은 결제(빌링) 연동 전이라 null
- 4주 리텐션: 최근 8주 가입 주차별로, 가입 후 1~4주차에 로그인한 비율
날짜 묶음은 DB 함수 대신 파이썬에서 한다 (MySQL·SQLite 공통). 기간은 최대 366일.
"""
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import distinct, func
from sqlalchemy.orm import Session

from database import get_db
from models import Club, Meeting, User, UserActivityLog
from schemas import MeetingType
from utils.datetime_utils import get_kst_now

from .deps import get_admin_user

router = APIRouter(tags=["admin-kpi"])

MAX_DAYS = 366


def _start(d: date, interval: str) -> date:
    if interval == "week":
        return d - timedelta(days=d.weekday())
    if interval == "month":
        return d.replace(day=1)
    return d


def _next(d: date, interval: str) -> date:
    if interval == "week":
        return d + timedelta(days=7)
    if interval == "month":
        return (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return d + timedelta(days=1)


def _buckets(frm: date, to: date, interval: str) -> list[date]:
    out, d = [], _start(frm, interval)
    while d <= to:
        out.append(d)
        d = _next(d, interval)
    return out


def _dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def _real_users():
    return [User.deleted_at.is_(None), ~User.nickname.like("guest_%")]


def _count_by_day(db: Session, column, frm: date, to: date, *filters) -> dict[date, int]:
    rows = (db.query(func.date(column), func.count()).filter(column >= _dt(frm), column < _dt(to + timedelta(days=1)), *filters)
            .group_by(func.date(column)).all())
    return {(r if isinstance(r, date) else date.fromisoformat(str(r)[:10])): n for r, n in rows}


def _login_pairs(db: Session, frm: date, to: date) -> set[tuple[date, int]]:
    """(날짜, 회원) 쌍 — 같은 날 여러 번 로그인해도 한 번"""
    rows = (db.query(func.date(UserActivityLog.created_at), UserActivityLog.user_id)
            .filter(UserActivityLog.event_type == "LOGIN", UserActivityLog.created_at >= _dt(frm),
                    UserActivityLog.created_at < _dt(to + timedelta(days=1)))
            .distinct().all())
    return {((d if isinstance(d, date) else date.fromisoformat(str(d)[:10])), u) for d, u in rows}


def _sum(by_day: dict[date, int], lo: date, hi: date) -> int:
    return sum(n for d, n in by_day.items() if lo <= d < hi)


def _period(db: Session, frm: date, to: date, interval: str, data_since: date | None) -> dict:
    signups = _count_by_day(db, User.created_at, frm, to, *_real_users())
    clubs = _count_by_day(db, Club.created_at, frm, to, Club.deleted_at.is_(None))
    rounds = _count_by_day(db, Meeting.meeting_time, frm, to, Meeting.meeting_type == MeetingType.ROUND, Meeting.status != "CANCELED")
    socials = _count_by_day(db, Meeting.meeting_time, frm, to, Meeting.meeting_type == MeetingType.SOCIAL, Meeting.status != "CANCELED")
    logins = _login_pairs(db, frm, to) if data_since else set()

    series = []
    for b in _buckets(frm, to, interval):
        lo, hi = max(b, frm), min(_next(b, interval), to + timedelta(days=1))
        covered = data_since is not None and lo >= data_since
        days = (hi - lo).days or 1
        active = {u for d, u in logins if lo <= d < hi}
        daily = sum(1 for d, _ in logins if lo <= d < hi)
        series.append({
            "start": b.isoformat(),
            # 기간 중 일부만 활동 로그가 있으면 그 구간도 "데이터 없음"으로 본다 (과소 집계 방지)
            "active_users": len(active) if covered else None,
            "dau": round(daily / days, 1) if covered else None,
            "signups": _sum(signups, lo, hi),
            "clubs": _sum(clubs, lo, hi),
            "rounds": _sum(rounds, lo, hi),
            "socials": _sum(socials, lo, hi),
            "revenue": None,
        })

    covered_all = data_since is not None and frm >= data_since
    days_total = (to - frm).days + 1
    mau_from = max(frm, to - timedelta(days=29))
    totals = {
        "dau": round(len(logins) / days_total, 1) if covered_all else None,
        "mau": len({u for d, u in logins if d >= mau_from}) if data_since is not None and mau_from >= data_since else None,
        "signups": sum(signups.values()),
        "clubs": sum(clubs.values()),
        "rounds": sum(rounds.values()),
        "socials": sum(socials.values()),
        "revenue": None,
    }
    return {"series": series, "totals": totals}


def _retention(db: Session, today: date, data_since: date | None) -> list[dict]:
    """최근 8주(이번 주 제외) 가입 코호트의 1~4주차 로그인 비율. 아직 지나지 않은 주차나 로그 이전 주차는 null"""
    this_week = _start(today, "week")
    cohorts = []
    for k in range(8, 0, -1):
        ws = this_week - timedelta(weeks=k)
        members = [u for (u,) in db.query(User.id).filter(User.created_at >= _dt(ws), User.created_at < _dt(ws + timedelta(days=7)), *_real_users()).all()]
        weeks = []
        for n in range(1, 5):
            lo, hi = ws + timedelta(weeks=n), ws + timedelta(weeks=n + 1)
            if hi > today + timedelta(days=1) or data_since is None or lo < data_since or not members:
                weeks.append(None)
                continue
            back = (db.query(func.count(distinct(UserActivityLog.user_id)))
                    .filter(UserActivityLog.event_type == "LOGIN", UserActivityLog.user_id.in_(members),
                            UserActivityLog.created_at >= _dt(lo), UserActivityLog.created_at < _dt(hi)).scalar() or 0)
            weeks.append(round(back * 100 / len(members), 1))
        cohorts.append({"week_start": ws.isoformat(), "size": len(members), "weeks": weeks})
    return cohorts


@router.get("/kpi")
async def get_kpi(
    from_: str | None = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    interval: str = Query("day", pattern="^(day|week|month)$"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    today = get_kst_now().date()
    end = date.fromisoformat(to) if to else today
    start = date.fromisoformat(from_) if from_ else end - timedelta(days=29)
    if start > end:
        raise HTTPException(status_code=422, detail="시작일이 종료일보다 늦습니다")
    if (end - start).days + 1 > MAX_DAYS:
        raise HTTPException(status_code=422, detail=f"기간은 최대 {MAX_DAYS}일입니다")

    first = db.query(func.min(UserActivityLog.created_at)).filter(UserActivityLog.event_type == "LOGIN").scalar()
    data_since = first.date() if first else None
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev = _period(db, prev_end - timedelta(days=length - 1), prev_end, interval, data_since)["totals"]
    cur = _period(db, start, end, interval, data_since)
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "interval": interval,
        "data_since": data_since.isoformat() if data_since else None,
        "totals": cur["totals"],
        "previous": prev,
        "clubs_total": db.query(Club).filter(Club.deleted_at.is_(None)).count(),
        "series": cur["series"],
        "retention": _retention(db, today, data_since),
    }
