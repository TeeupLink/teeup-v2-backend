"""
권한 체크 유틸리티
"""
from fastapi import HTTPException, status, Depends, Request
from sqlalchemy.orm import Session
import logging

from database import get_db
from models import User, Admin, UserStatus, Meeting, ClubMembership
from utils.jwt_auth import jwt_auth
from routers.auth import get_current_user
from schemas import MembershipStatus, ClubRole

logger = logging.getLogger(__name__)

MEMBERSHIP_ACTIVE_STATUSES = {
    MembershipStatus.ACTIVE.value,
    "APPROVED",  # 레거시 호환성을 위해 문자열로 추가
}

def is_active_membership_status(status: str | None) -> bool:
    """멤버십이 활성 상태인지 확인"""
    return status in MEMBERSHIP_ACTIVE_STATUSES

class PermissionChecker:
    """권한 체크 클래스"""
    
    @staticmethod
    def check_admin_permission(current_user: dict, db: Session) -> bool:
        """관리자 권한 체크"""
        try:
            user_id = current_user.get('id')
            user_type = current_user.get('type', 'user')
            if not user_id:
                return False
            
            # JWT 토큰의 type이 "admin"인지 확인
            if user_type == "admin":
                admin = db.query(Admin).filter(
                    Admin.id == user_id,
                    Admin.deleted_at.is_(None)
                ).first()
                return admin is not None and admin.status == UserStatus.ACTIVE
            
            return False
            
        except Exception as e:
            logger.error(f"관리자 권한 체크 중 오류: {str(e)}")
            return False
    
    @staticmethod
    def check_user_permission(current_user: dict, target_user_id: int, db: Session) -> bool:
        """사용자 권한 체크 (본인 또는 관리자)"""
        try:
            user_id = current_user.get('id')
            if not user_id:
                return False
            
            # 본인인 경우
            if user_id == target_user_id:
                return True
            
            # 관리자 권한 확인
            return PermissionChecker.check_admin_permission(current_user, db)
            
        except Exception as e:
            logger.error(f"사용자 권한 체크 중 오류: {str(e)}")
            return False
    
    @staticmethod
    def check_club_permission(current_user: dict, club_id: int, db: Session) -> bool:
        """클럽 권한 체크 (멤버 또는 관리자)"""
        try:
            user_id = current_user.get('id')
            if not user_id:
                return False
            
            # 관리자 권한 확인
            if PermissionChecker.check_admin_permission(current_user, db):
                return True
            
            # 클럽 멤버십 확인
            from models import ClubMembership
            membership = db.query(ClubMembership).filter(
                ClubMembership.user_id == user_id,
                ClubMembership.club_id == club_id,
                ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES)
            ).first()
            
            return membership is not None
            
        except Exception as e:
            logger.error(f"클럽 권한 체크 중 오류: {str(e)}")
            return False

# 의존성 함수들
def require_admin():
    """관리자 권한 필요 (JWT 기반)"""
    from routers.admin import get_admin_user
    return get_admin_user

def require_user_or_admin(target_user_id: int, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """사용자 본인 또는 관리자 권한 필요"""
    if not PermissionChecker.check_user_permission(current_user, target_user_id, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="본인 또는 관리자 권한이 필요합니다"
        )
    return current_user

def require_club_member_or_admin(club_id: int, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """클럽 멤버 또는 관리자 권한 필요"""
    if not PermissionChecker.check_club_permission(current_user, club_id, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="클럽 멤버 또는 관리자 권한이 필요합니다"
        )
    return current_user

def is_meeting_organizer_or_manager(
    meeting_id: int,
    user_id: int,
    db: Session
) -> bool:
    """
    모임 생성자/리더 권한 체크 (모임 생성자 또는 리더/매니저)
    
    Args:
        meeting_id: 모임 ID
        user_id: 사용자 ID
        db: 데이터베이스 세션
    
    Returns:
        bool: 권한이 있으면 True
    """
    try:
        # 모임 조회
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            return False
        
        # 모임 생성자 권한
        if meeting.created_by == user_id:
            return True
        
        # 클럽 리더/매니저 권한
        club_membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == meeting.club_id,
            ClubMembership.user_id == user_id,
            ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES)
        ).first()
        
        if club_membership and club_membership.role in [ClubRole.LEADER, ClubRole.MANAGER]:
            return True
        
        return False
        
    except Exception as e:
        logger.error(f"모임 생성자/리더 권한 체크 중 오류: {str(e)}")
        return False
