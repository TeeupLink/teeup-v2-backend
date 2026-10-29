"""
백오피스 대시보드 API
"""
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
import logging

from database import get_db
from models import User, Meeting, Club, ClubMembership
from utils.datetime_utils import get_kst_now
from .deps import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-dashboard"])


@router.get("/dashboard")
async def get_admin_dashboard(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자 대시보드 데이터"""
    try:
        from models import Payment, Notice
        from models import UserStatus, PaymentStatus, ClubStatus
        from schemas import MeetingType

        total_users = db.query(User).filter(User.deleted_at.is_(None), ~User.nickname.like("guest_%")).count()
        active_users = db.query(User).filter(
            User.deleted_at.is_(None), User.status == UserStatus.ACTIVE, ~User.nickname.like("guest_%")
        ).count()
        total_clubs = db.query(Club).filter(Club.deleted_at.is_(None)).count()
        total_meetings = db.query(Meeting).count()
        total_socials = db.query(Meeting).filter(Meeting.meeting_type == MeetingType.SOCIAL).count()
        total_payments = db.query(Payment).count()
        total_notices = db.query(Notice).count()

        payment_stats = {"total_amount": 0, "successful_count": 0, "failed_count": 0, "pending_count": 0}
        for p in db.query(Payment).all():
            payment_stats["total_amount"] += p.amount or 0
            if p.status == PaymentStatus.SUCCEEDED:
                payment_stats["successful_count"] += 1
            elif p.status == PaymentStatus.FAILED:
                payment_stats["failed_count"] += 1
            elif p.status == PaymentStatus.PENDING:
                payment_stats["pending_count"] += 1

        club_stats = {"approved_count": 0, "pending_count": 0, "rejected_count": 0, "total_members": 0}
        for club in db.query(Club).filter(Club.deleted_at.is_(None)).all():
            if club.status == ClubStatus.ACTIVE:
                club_stats["approved_count"] += 1
            elif club.status == ClubStatus.PENDING:
                club_stats["pending_count"] += 1
            elif club.status == ClubStatus.REJECTED:
                club_stats["rejected_count"] += 1
            club_stats["total_members"] += db.query(ClubMembership).filter(
                ClubMembership.club_id == club.id, ClubMembership.status == "ACTIVE"
            ).count()

        recent_activities = []
        for u in db.query(User).filter(User.deleted_at.is_(None), ~User.nickname.like("guest_%")).order_by(User.created_at.desc()).limit(5).all():
            recent_activities.append({
                "id": f"user_{u.id}", "type": "user", "title": "새 사용자 가입",
                "message": f"{u.nickname}님이 새로 가입했습니다.",
                "timestamp": u.created_at.isoformat() if u.created_at else None, "user_name": u.nickname,
            })
        for m in db.query(Meeting).order_by(Meeting.created_at.desc()).limit(5).all():
            recent_activities.append({
                "id": f"meeting_{m.id}", "type": "meeting", "title": "새 모임 생성",
                "message": f"{m.name} 모임이 생성되었습니다.",
                "timestamp": m.created_at.isoformat() if m.created_at else None, "meeting_title": m.name,
            })
        for s in db.query(Meeting).filter(Meeting.meeting_type == MeetingType.SOCIAL).order_by(Meeting.created_at.desc()).limit(5).all():
            recent_activities.append({
                "id": f"social_{s.id}", "type": "social", "title": "새 소셜 모임 생성",
                "message": f"{s.name} 소셜 모임이 생성되었습니다.",
                "timestamp": s.created_at.isoformat() if s.created_at else None, "social_name": s.name,
            })
        recent_activities.sort(key=lambda x: x["timestamp"] or "", reverse=True)
        recent_activities = recent_activities[:10]

        return {
            "stats": {
                "total_users": total_users, "active_users": active_users, "total_clubs": total_clubs,
                "total_meetings": total_meetings, "total_socials": total_socials,
                "total_payments": total_payments, "total_notices": total_notices,
                "admin_users": current_user.get("role"),
            },
            "payment_stats": payment_stats,
            "club_stats": club_stats,
            "recent_activities": recent_activities,
            "user": {"id": current_user.get("id"), "email": current_user.get("email"), "role": current_user.get("role")},
        }
    except Exception as e:
        logger.error(f"관리자 대시보드 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="대시보드 데이터 조회 중 오류가 발생했습니다",
        )


@router.get("/dashboard/todo")
async def get_dashboard_todo(db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """
    오늘 처리할 일 (API-020). 호출자 권한 밖 항목은 null.
    각 숫자는 대시보드 카드가 여는 목록의 total 과 같아야 한다.
    """
    from models import ContentReport, Inquiry, InquiryStatus, MembershipStatus, ReportStatus
    from schemas import MeetingType

    from .permissions import can

    role = current_user.get("role")
    result: dict[str, int | None] = {
        "join_requests": None,
        "unanswered_inquiries": None,
        "today_rounds": None,
        "pending_reports": None,
    }
    if can(role, "joinRequests"):
        result["join_requests"] = (
            db.query(ClubMembership)
            .join(Club, Club.id == ClubMembership.club_id)
            .join(User, User.id == ClubMembership.user_id)
            .filter(ClubMembership.status == MembershipStatus.PENDING, Club.deleted_at.is_(None), User.deleted_at.is_(None))
            .count()
        )
    if can(role, "inquiries"):
        result["unanswered_inquiries"] = (
            db.query(Inquiry).filter(Inquiry.status.in_([InquiryStatus.PENDING, InquiryStatus.SUBMITTED])).count()
        )
    if can(role, "meetings"):
        now = get_kst_now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        result["today_rounds"] = (
            db.query(Meeting)
            .filter(
                Meeting.meeting_type == MeetingType.ROUND,
                Meeting.meeting_time >= start,
                Meeting.meeting_time < start + timedelta(days=1),
                Meeting.status != "CANCELED",
            )
            .count()
        )
    if can(role, "reports"):
        result["pending_reports"] = db.query(ContentReport).filter(ContentReport.status == ReportStatus.PENDING).count()
    return result


@router.get("/dashboard/stats")
async def get_dashboard_stats(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """대시보드 통계 조회"""
    try:
        from schemas import MeetingType

        total_users = db.query(User).filter(User.deleted_at.is_(None), ~User.nickname.like("guest_%")).count()
        total_clubs = db.query(Club).filter(Club.deleted_at.is_(None)).count()
        total_meetings = db.query(Meeting).count()
        today = get_kst_now()
        start_of_week = (today - timedelta(days=today.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        weekly_rounds = db.query(Meeting).filter(
            Meeting.meeting_type == MeetingType.ROUND,
            Meeting.meeting_time >= start_of_week,
            Meeting.meeting_time < start_of_week + timedelta(days=7),
        ).count()
        return {"total_users": total_users, "total_clubs": total_clubs, "total_meetings": total_meetings, "weekly_rounds": weekly_rounds}
    except Exception as e:
        logger.error(f"대시보드 통계 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="통계 데이터 조회 중 오류가 발생했습니다",
        )


@router.get("/dashboard/activities")
async def get_dashboard_activities(
    db: Session = Depends(get_db),
    limit: int = Query(default=10, ge=1, le=50),
    current_user: dict = Depends(get_admin_user),
):
    """최근 활동 조회"""
    try:
        from schemas import MeetingType

        recent_activities = []
        for u in db.query(User).filter(User.deleted_at.is_(None), ~User.nickname.like("guest_%")).order_by(User.created_at.desc()).limit(5).all():
            recent_activities.append({
                "id": f"user_{u.id}", "type": "user_joined",
                "title": f"{u.nickname}님이 새로 가입했습니다.",
                "timestamp": u.created_at.isoformat() if u.created_at else None,
            })
        for m in db.query(Meeting).order_by(Meeting.created_at.desc()).limit(5).all():
            at = "meeting_created" if m.meeting_type != MeetingType.SOCIAL else "post_created"
            recent_activities.append({
                "id": f"meeting_{m.id}", "type": at,
                "title": f"{m.name} 모임이 생성되었습니다.",
                "timestamp": m.created_at.isoformat() if m.created_at else None,
            })
        recent_activities.sort(key=lambda x: x["timestamp"] or "", reverse=True)
        return {"data": recent_activities[:limit]}
    except Exception as e:
        logger.error(f"최근 활동 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="최근 활동 조회 중 오류가 발생했습니다",
        )


@router.get("/debug/sessions")
async def get_session_debug_info(current_user: dict = Depends(get_admin_user)):
    """세션 디버깅 정보 조회"""
    return {"message": "세션 관리 없음", "debug_info": {"session_count": 0, "session_ids": []}, "current_user": current_user}
