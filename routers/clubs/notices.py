# 클럽별 공지사항 CRUD API들
from fastapi import APIRouter, Depends, HTTPException, status
from utils.content_filter import reject_banned_content
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime
from utils.datetime_utils import get_kst_now

from database import get_db
from models import User, Club, ClubMembership, ClubRole, ClubNotice
from schemas import ClubNoticeCreate, ClubNoticeUpdate, ClubNoticeResponse, PaginatedResponse, MessageResponse
from routers.auth import get_current_user, get_current_active_user

router = APIRouter(prefix="/clubs", tags=["클럽 공지사항"])

@router.get("/{club_id}/notices", response_model=PaginatedResponse)
async def get_club_notices(
    club_id: str,
    page: int = 1,
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽별 공지사항 조회"""
    try:
        # 클럽 존재 여부 확인 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()

        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None

        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )

        # 실제 클럽 ID 사용
        actual_club_id = club.id

        # 클럽 멤버인지 확인
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == actual_club_id,
            ClubMembership.user_id == current_user.id
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 멤버만 공지사항을 조회할 수 있습니다."
            )
        
        # 페이지네이션 계산
        offset = (page - 1) * limit
        
        # 사용자 권한 확인 (리더/매니저인지)
        is_manager_or_leader = membership.role in [ClubRole.LEADER, ClubRole.MANAGER]
        
        # 공지사항 조회 (중요 공지 먼저, 최신순)
        # 일반 멤버는 비공개 공지사항 제외
        notices_query = db.query(ClubNotice).filter(ClubNotice.club_id == actual_club_id)
        
        if not is_manager_or_leader:
            # 일반 멤버는 비공개 공지사항 제외
            notices_query = notices_query.filter(ClubNotice.is_private == False)
        
        total = notices_query.count()
        
        notices = notices_query.order_by(
            ClubNotice.is_important.desc(),
            ClubNotice.created_at.desc()
        ).offset(offset).limit(limit).all()
        
        # 응답 데이터 구성
        notice_list = []
        for notice in notices:
            author = db.query(User).filter(User.id == notice.author_id).first()
            notice_list.append({
                "id": notice.id,                "club_id": notice.club_id,
                "title": notice.title,
                "content": notice.content,
                "is_important": notice.is_important,
                "is_private": notice.is_private,
                "author_id": notice.author_id,
                "author_name": author.realname if author else "Unknown",
                "view_count": notice.view_count,
                "created_at": notice.created_at,
                "updated_at": notice.updated_at
            })
        
        total_pages = (total + limit - 1) // limit
        
        return {
            "data": notice_list,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"공지사항 조회 에러: {str(e)}")
        print(f"에러 타입: {type(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )

@router.post("/{club_id}/notices", response_model=ClubNoticeResponse, dependencies=[Depends(reject_banned_content)])
async def create_club_notice(
    club_id: str,
    notice_data: ClubNoticeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽별 공지사항 작성 (리더/매니저만 가능)"""
    try:
        print(f"DEBUG: 클럽 조회 시작 - club_id: {club_id}")
        
        # 클럽 존재 여부 확인 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()
        
        print(f"DEBUG: display_id로 조회 결과: {club}")

        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None
            
            print(f"DEBUG: id로 조회 결과: {club}")

        if not club:
            print(f"DEBUG: 클럽을 찾을 수 없음 - club_id: {club_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )
            
        print(f"DEBUG: 클럽 찾음 - id: {club.id}, display_id: {club.display_id}, name: {club.name}")

        # 실제 클럽 ID 사용
        actual_club_id = club.id

        # 클럽 멤버 권한 확인
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == actual_club_id,
            ClubMembership.user_id == current_user.id,
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER])
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 리더나 매니저만 공지사항을 작성할 수 있습니다."
            )
        
        # 공지사항 생성
        notice = ClubNotice(
            club_id=actual_club_id,
            title=notice_data.title,
            content=notice_data.content,
            is_important=notice_data.is_important,
            is_private=notice_data.is_private,
            author_id=current_user.id,
            view_count=0
        )
        
        db.add(notice)
        db.commit()
        db.refresh(notice)
        
        # 클럽 멤버들에게 새 공지사항 알림 전송
        try:
            from utils.notification_service import create_notice_notification
            
            # 클럽의 모든 멤버 조회 (작성자 제외)
            club_members = db.query(ClubMembership).filter(
                ClubMembership.club_id == actual_club_id,
                ClubMembership.user_id != current_user.id  # 작성자 제외
            ).all()
            club_name = club.name if club else "알 수 없는 클럽"
            
            for member in club_members:
                create_notice_notification(
                    db=db,
                    user_id=member.user_id,
                    notice_title=notice_data.title,
                    club_name=club_name,
                    club_id=actual_club_id,
                    notice_id=notice.id
                )
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"클럽 공지사항 알림 전송 실패: {str(e)}")
        
        # 작성자 정보 조회
        author = db.query(User).filter(User.id == notice.author_id).first()
        
        return {
            "id": notice.id,
            "club_id": notice.club_id,
            "title": notice.title,
            "content": notice.content,
            "is_important": notice.is_important,
            "is_private": notice.is_private,
            "author_id": notice.author_id,
            "author_name": author.nickname if author else "Unknown",
            "view_count": notice.view_count,
            "created_at": notice.created_at,
            "updated_at": notice.updated_at
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"공지사항 작성 에러: {str(e)}")
        print(f"에러 타입: {type(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )

@router.get("/{club_id}/notices/{notice_id}", response_model=ClubNoticeResponse)
async def get_club_notice_detail(
    club_id: str,
    notice_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽별 공지사항 상세 조회"""
    try:
        # 클럽 존재 여부 확인 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()

        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None

        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )

        # 실제 클럽 ID 사용
        actual_club_id = club.id

        # 클럽 멤버인지 확인
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == actual_club_id,
            ClubMembership.user_id == current_user.id
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 멤버만 공지사항을 조회할 수 있습니다."
            )
        
        # 공지사항 조회
        notice = db.query(ClubNotice).filter(
            ClubNotice.id == notice_id,
            ClubNotice.club_id == actual_club_id
        ).first()
        
        if not notice:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="공지사항을 찾을 수 없습니다."
            )
        
        # 비공개 공지사항 권한 체크
        if notice.is_private:
            is_manager_or_leader = membership.role in [ClubRole.LEADER, ClubRole.MANAGER]
            if not is_manager_or_leader:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="비공개 공지사항은 리더나 매니저만 조회할 수 있습니다."
                )
        
        # 조회수 증가
        notice.view_count = (notice.view_count or 0) + 1
        db.commit()
        
        # 작성자 정보 조회
        author = db.query(User).filter(User.id == notice.author_id).first()
        
        return {
            "id": notice.id,            "club_id": notice.club_id,
            "title": notice.title,
            "content": notice.content,
            "is_important": notice.is_important,
            "is_private": notice.is_private,
            "author_id": notice.author_id,
            "author_name": author.nickname if author else "Unknown",
            "view_count": notice.view_count,
            "created_at": notice.created_at,
            "updated_at": notice.updated_at
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.put("/{club_id}/notices/{notice_id}", response_model=ClubNoticeResponse, dependencies=[Depends(reject_banned_content)])
async def update_club_notice(
    club_id: str,
    notice_id: int,
    notice_data: ClubNoticeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽별 공지사항 수정 (작성자만 가능)"""
    try:
        # 클럽 존재 여부 확인 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()

        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None

        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )

        # 실제 클럽 ID 사용
        actual_club_id = club.id

        # 공지사항 조회
        notice = db.query(ClubNotice).filter(
            ClubNotice.id == notice_id,
            ClubNotice.club_id == actual_club_id
        ).first()
        
        if not notice:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="공지사항을 찾을 수 없습니다."
            )
        
        # 작성자 권한 확인
        if notice.author_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="공지사항 작성자만 수정할 수 있습니다."
            )
        
        # 공지사항 수정
        if notice_data.title is not None:
            notice.title = notice_data.title
        if notice_data.content is not None:
            notice.content = notice_data.content
        if notice_data.is_important is not None:
            notice.is_important = notice_data.is_important
        if notice_data.is_private is not None:
            notice.is_private = notice_data.is_private
        
        notice.updated_at = get_kst_now()
        db.commit()
        db.refresh(notice)
        
        # 작성자 정보 조회
        author = db.query(User).filter(User.id == notice.author_id).first()
        
        return {
            "id": notice.id,            "club_id": notice.club_id,
            "title": notice.title,
            "content": notice.content,
            "is_important": notice.is_important,
            "is_private": notice.is_private,
            "author_id": notice.author_id,
            "author_name": author.nickname if author else "Unknown",
            "view_count": notice.view_count,
            "created_at": notice.created_at,
            "updated_at": notice.updated_at
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.delete("/{club_id}/notices/{notice_id}", response_model=MessageResponse)
async def delete_club_notice(
    club_id: str,
    notice_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽별 공지사항 삭제 (작성자만 가능)"""
    try:
        # 클럽 존재 여부 확인 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()

        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None

        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )

        # 실제 클럽 ID 사용
        actual_club_id = club.id

        # 공지사항 조회
        notice = db.query(ClubNotice).filter(
            ClubNotice.id == notice_id,
            ClubNotice.club_id == actual_club_id
        ).first()
        
        if not notice:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="공지사항을 찾을 수 없습니다."
            )
        
        # 작성자 권한 확인
        if notice.author_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="공지사항 작성자만 삭제할 수 있습니다."
            )
        
        # 공지사항 삭제
        db.delete(notice)
        db.commit()
        
        return {
            "message": "공지사항이 삭제되었습니다.",
            "success": True
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )
