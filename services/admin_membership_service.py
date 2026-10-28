"""
클럽 가입신청 승인·거절 — 행 단위 API(/clubs/{id}/members/{uid}/approve·reject)와
일괄 API(/join-requests/bulk)가 같은 규칙을 쓰도록 모은다.
"""
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from models import Club, ClubMembership, ClubStatus, MembershipStatus
from utils.datetime_utils import get_kst_now


def find_pending(db: Session, membership_id: int) -> ClubMembership:
    membership = db.query(ClubMembership).filter(ClubMembership.id == membership_id).first()
    if not membership:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="가입 신청을 찾을 수 없습니다")
    if membership.status != MembershipStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 처리된 신청입니다")
    return membership


def approve(db: Session, membership: ClubMembership) -> None:
    club = db.query(Club).filter(Club.id == membership.club_id, Club.deleted_at.is_(None)).first()
    if not club:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다")
    if club.status != ClubStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="비활성 클럽에는 가입을 승인할 수 없습니다")
    now = get_kst_now()
    membership.status = MembershipStatus.ACTIVE
    membership.updated_at = now
    club.member_count = (club.member_count or 0) + 1
    club.updated_at = now


def reject(db: Session, membership: ClubMembership) -> None:
    # 기존 동작 유지: 거절 시 멤버십 행을 지운다 (앱이 PENDING 이 아닌 행을 '가입됨'으로 읽을 수 있어 상태값을 새로 두지 않음).
    # 처리 이력은 감사 로그(BE#49)에 남긴다.
    db.delete(membership)
