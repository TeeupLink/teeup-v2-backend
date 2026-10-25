# 클럽 통계 API
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import Literal

from database import get_db
from models import Club, ClubMembership, Meeting
from models.enums import MeetingType
from schemas import MembershipStatus
from routers.auth import get_current_active_user
from models import User

router = APIRouter(prefix="/clubs", tags=["클럽 통계"])


def _resolve_club(db: Session, club_id: str):
    club = db.query(Club).filter(
        Club.display_id == club_id,
        Club.deleted_at.is_(None)
    ).first()
    if not club:
        try:
            cid = int(club_id)
            club = db.query(Club).filter(
                Club.id == cid,
                Club.deleted_at.is_(None)
            ).first()
        except ValueError:
            pass
    return club


def _aggregate_by_period(records: list, period: str, date_attr: str = "created_at"):
    """레코드 리스트를 기간별로 집계"""
    groups = defaultdict(int)
    for r in records:
        dt = getattr(r, date_attr, None)
        if not dt:
            continue
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        if period == "daily":
            key = dt.strftime("%Y-%m-%d")
        elif period == "weekly":
            iso = dt.isocalendar()
            key = f"{iso[0]}-W{iso[1]:02d}"
        else:  # monthly
            key = dt.strftime("%Y-%m")
        groups[key] += 1
    # 정렬 후 리스트로 변환
    keys = sorted(groups.keys(), reverse=True)
    return [{"label": k, "count": groups[k]} for k in keys]


@router.get("/{club_id}/stats")
async def get_club_stats(
    club_id: str,
    period: Literal["daily", "weekly", "monthly"] = Query("monthly", description="집계 단위: daily, weekly, monthly"),
    days: int = Query(90, ge=7, le=365, description="조회 기간(일) - start_date/end_date 미지정 시 사용"),
    start_date: str | None = Query(None, description="시작일 (YYYY-MM-DD) - 클럽 생성일 이후"),
    end_date: str | None = Query(None, description="종료일 (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    클럽 통계 조회 (멤버만 가능)
    - 가입수: 일별/주별/월별
    - 라운딩 수: 일별/주별/월별
    - 소셜 수: 일별/주별/월별
    - start_date, end_date 지정 시 해당 구간 조회 (클럽 생성일 기준)
    """
    club = _resolve_club(db, club_id)
    if not club:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="클럽을 찾을 수 없습니다."
        )

    # 멤버십 확인 (멤버만 조회 가능)
    membership = db.query(ClubMembership).filter(
        ClubMembership.club_id == club.id,
        ClubMembership.user_id == current_user.id,
        or_(
            ClubMembership.status == MembershipStatus.ACTIVE,
            ClubMembership.status == "APPROVED"
        )
    ).first()
    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="클럽 멤버만 통계를 조회할 수 있습니다."
        )

    # 클럽 생성일 (YYYY-MM-DD)
    club_created = club.created_at
    if club_created:
        club_created_str = club_created.strftime("%Y-%m-%d") if hasattr(club_created, "strftime") else str(club_created)[:10]
    else:
        club_created_str = "2000-01-01"

    # 날짜 범위 결정
    if start_date and end_date:
        try:
            since = datetime.strptime(start_date, "%Y-%m-%d")
            until = datetime.strptime(end_date, "%Y-%m-%d")
            if since > until:
                since, until = until, since
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="날짜 형식은 YYYY-MM-DD여야 합니다.")
    else:
        until = datetime.utcnow()
        since = until - timedelta(days=days)

    # 1. 가입수 (승인된 멤버의 created_at 기준)
    membership_q = db.query(ClubMembership).filter(
        ClubMembership.club_id == club.id,
        ClubMembership.created_at >= since,
        or_(
            ClubMembership.status == MembershipStatus.ACTIVE,
            ClubMembership.status == "APPROVED"
        )
    )
    if start_date and end_date:
        membership_q = membership_q.filter(ClubMembership.created_at <= until)
    membership_records = membership_q.all()
    memberships = _aggregate_by_period(membership_records, period)

    # 2. 라운딩 수 (meeting_type == ROUND)
    rounding_q = db.query(Meeting).filter(
        Meeting.club_id == club.id,
        Meeting.meeting_type == MeetingType.ROUND,
        Meeting.created_at >= since
    )
    if start_date and end_date:
        rounding_q = rounding_q.filter(Meeting.created_at <= until)
    rounding_records = rounding_q.all()
    roundings = _aggregate_by_period(rounding_records, period)

    # 3. 소셜 수 (meeting_type == SOCIAL)
    social_q = db.query(Meeting).filter(
        Meeting.club_id == club.id,
        Meeting.meeting_type == MeetingType.SOCIAL,
        Meeting.created_at >= since
    )
    if start_date and end_date:
        social_q = social_q.filter(Meeting.created_at <= until)
    social_records = social_q.all()
    socials = _aggregate_by_period(social_records, period)

    # 총합 (기존 호환용)
    total_members = sum(m["count"] for m in memberships)
    total_roundings = sum(r["count"] for r in roundings)
    total_socials = sum(s["count"] for s in socials)

    return {
        "period": period,
        "days": days,
        "start_date": start_date,
        "end_date": end_date,
        "club_created_at": club_created_str,
        "memberships": memberships,
        "roundings": roundings,
        "socials": socials,
        "total_memberships": total_members,
        "total_roundings": total_roundings,
        "total_socials": total_socials,
        # 기존 필드 호환
        "active_members": db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            or_(
                ClubMembership.status == MembershipStatus.ACTIVE,
                ClubMembership.status == "APPROVED"
            )
        ).count(),
        "total_meetings": total_roundings + total_socials,
    }
