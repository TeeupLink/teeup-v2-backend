# 신고 / 차단 라우터 (App Store Guideline 1.2 — 사용자 생성 콘텐츠 관리)
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import (User, Club, ClubNotice, Regulation, Meeting, Inquiry,
                    ContentReport, UserBlock)
from models.enums import InquiryType, ReportTargetType
from schemas import MessageResponse
from schemas.moderation import (ContentReportCreate, ContentReportResponse,
                                BlockedUsersResponse, BlockedUserItem)
from utils import activity
from routers.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/moderation", tags=["moderation"])

# 신고 대상 종류별 존재 확인용 모델 매핑
_TARGET_MODEL: dict[ReportTargetType, type] = {
    ReportTargetType.CLUB: Club,
    ReportTargetType.MEETING: Meeting,
    ReportTargetType.CLUB_NOTICE: ClubNotice,
    ReportTargetType.REGULATION: Regulation,
    ReportTargetType.USER: User,
}

_TARGET_LABEL = {
    ReportTargetType.CLUB: "클럽",
    ReportTargetType.MEETING: "모임",
    ReportTargetType.CLUB_NOTICE: "클럽 공지",
    ReportTargetType.REGULATION: "클럽 규정",
    ReportTargetType.USER: "사용자",
}

_REASON_LABEL = {
    "SPAM": "스팸·광고",
    "ABUSE": "욕설·비방·괴롭힘",
    "INAPPROPRIATE": "부적절한 콘텐츠",
    "FRAUD": "사기·허위 정보",
    "PRIVACY": "개인정보 노출",
    "OTHER": "기타",
}


@router.post("/reports", response_model=ContentReportResponse, status_code=201)
async def create_report(payload: ContentReportCreate,
                        current_user=Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """콘텐츠·사용자 신고 등록.

    신고 내역은 content_reports 에 저장하고, 운영자가 기존 백오피스 문의 목록에서
    바로 볼 수 있도록 [신고] 제목의 문의도 함께 생성한다.
    """
    model = _TARGET_MODEL[payload.target_type]
    if not db.query(model).filter(model.id == payload.target_id).first():
        raise HTTPException(status_code=404, detail="신고 대상을 찾을 수 없습니다.")

    if payload.target_type == ReportTargetType.USER and payload.target_id == current_user.id:
        raise HTTPException(status_code=400, detail="자기 자신은 신고할 수 없습니다.")

    try:
        report = ContentReport(
            reporter_id=current_user.id,
            target_type=payload.target_type,
            target_id=payload.target_id,
            reason=payload.reason,
            description=payload.description,
        )
        db.add(report)

        label = _TARGET_LABEL[payload.target_type]
        reason_label = _REASON_LABEL.get(payload.reason.value, payload.reason.value)
        body = (f"신고 대상: {label} #{payload.target_id}\n"
                f"신고 사유: {reason_label}\n"
                f"신고자: user #{current_user.id}\n\n"
                f"{payload.description or '(상세 설명 없음)'}")
        db.add(Inquiry(
            user_id=current_user.id,
            title=f"[신고] {label} #{payload.target_id} — {reason_label}",
            content=body,
            type=InquiryType.GENERAL,
        ))

        db.commit()
        db.refresh(report)
        activity.log(current_user, "REPORT", f"{label} 신고 ({reason_label})", target_type="REPORT", target_id=report.id)
        logger.info("Content report #%s by user %s: %s #%s (%s)",
                    report.id, current_user.id, payload.target_type.value,
                    payload.target_id, payload.reason.value)
        return report
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("신고 등록 실패: %s", e)
        raise HTTPException(status_code=500, detail="신고 등록에 실패했습니다.")


@router.get("/blocks", response_model=BlockedUsersResponse)
async def get_blocked_users(current_user=Depends(get_current_user),
                            db: Session = Depends(get_db)):
    """내가 차단한 사용자 목록"""
    rows = (db.query(UserBlock, User)
            .join(User, User.id == UserBlock.blocked_id)
            .filter(UserBlock.blocker_id == current_user.id)
            .order_by(UserBlock.created_at.desc())
            .all())
    items = [BlockedUserItem(user_id=u.id,
                             nickname=u.nickname,
                             profile_image=u.profile_image,
                             blocked_at=b.created_at) for b, u in rows]
    return BlockedUsersResponse(blocked_user_ids=[i.user_id for i in items],
                                blocked_users=items)


@router.post("/blocks/{user_id}", response_model=MessageResponse)
async def block_user(user_id: int,
                     current_user=Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """사용자 차단. 이미 차단된 경우에도 성공으로 응답한다(멱등)."""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="자기 자신은 차단할 수 없습니다.")
    if not db.query(User).filter(User.id == user_id).first():
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")

    exists = (db.query(UserBlock)
              .filter(UserBlock.blocker_id == current_user.id,
                      UserBlock.blocked_id == user_id)
              .first())
    if exists:
        return MessageResponse(success=True, message="이미 차단된 사용자입니다.")

    try:
        db.add(UserBlock(blocker_id=current_user.id, blocked_id=user_id))
        db.commit()
        logger.info("User %s blocked user %s", current_user.id, user_id)
        return MessageResponse(success=True, message="사용자를 차단했습니다.")
    except Exception as e:
        db.rollback()
        logger.error("차단 실패: %s", e)
        raise HTTPException(status_code=500, detail="차단에 실패했습니다.")


@router.delete("/blocks/{user_id}", response_model=MessageResponse)
async def unblock_user(user_id: int,
                       current_user=Depends(get_current_user),
                       db: Session = Depends(get_db)):
    """차단 해제"""
    row = (db.query(UserBlock)
           .filter(UserBlock.blocker_id == current_user.id,
                   UserBlock.blocked_id == user_id)
           .first())
    if not row:
        raise HTTPException(status_code=404, detail="차단 내역이 없습니다.")
    try:
        db.delete(row)
        db.commit()
        return MessageResponse(success=True, message="차단을 해제했습니다.")
    except Exception as e:
        db.rollback()
        logger.error("차단 해제 실패: %s", e)
        raise HTTPException(status_code=500, detail="차단 해제에 실패했습니다.")
