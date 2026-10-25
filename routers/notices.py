from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc
from datetime import datetime

from database import get_db
from models import Notice, Notification, User
from schemas import NotificationType, NotificationStatus
from schemas import NoticeResponse, NoticeListResponse
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notices", tags=["notices"])


@router.get("/", response_model=NoticeListResponse)
async def get_notices(
    page: int = Query(1, ge=1, description="페이지 번호"),
    size: int = Query(20, ge=1, le=100, description="페이지 크기"),
    type: str | None = Query(None, description="공지사항 타입"),
    is_published: bool | None = Query(None, description="발행 여부"),
    is_important: bool | None = Query(None, description="중요 공지 여부"),
    search: str | None = Query(None, description="제목 검색"),
    db: Session = Depends(get_db)
):
    """공지사항 목록 조회"""
    try:
        query = db.query(Notice)
        
        # 필터 적용
        if type:
            query = query.filter(Notice.type == type)
        if is_published is not None:
            # is_published 필드가 있을 때만 필터링
            if hasattr(Notice, 'is_published'):
                query = query.filter(Notice.is_published == is_published)
        if is_important is not None:
            query = query.filter(Notice.is_important == is_important)
        if search:
            query = query.filter(Notice.title.contains(search))
        
        # 총 개수 조회
        total = query.count()
        
        # 정렬 및 페이징
        # published_at이 NULL일 수 있으므로 안전하게 처리
        try:
            # published_at이 있으면 사용, 없으면 created_at 사용
            from sqlalchemy import case
            notices = query.order_by(
                desc(Notice.is_important),
                desc(case((Notice.published_at.isnot(None), Notice.published_at), else_=Notice.created_at))
            ).offset((page - 1) * size).limit(size).all()
        except Exception as e:
            # published_at 필드가 없거나 에러 발생 시 created_at으로 정렬
            logger.warning(f"published_at 정렬 실패, created_at 사용: {e}")
            notices = query.order_by(desc(Notice.is_important), desc(Notice.created_at))\
                          .offset((page - 1) * size)\
                          .limit(size)\
                          .all()
        
        # 응답 데이터 변환
        notice_responses = []
        for notice in notices:
            try:
                view_count = getattr(notice, 'view_count', 0)
                
                # type 처리
                notice_type = notice.type.value if hasattr(notice.type, 'value') else str(notice.type)
                
                notice_responses.append(NoticeResponse(
                    id=notice.id,                    title=notice.title,
                    content=notice.content,
                    type=notice_type,
                    is_important=getattr(notice, 'is_important', False),
                    is_published=getattr(notice, 'is_published', False),
                    view_count=view_count,
                    published_at=getattr(notice, 'published_at', None),
                    attachment_file=getattr(notice, 'attachment_file', None),
                    web_view_link=getattr(notice, 'web_view_link', None),
                    created_at=getattr(notice, 'created_at', datetime.now()),
                    updated_at=getattr(notice, 'updated_at', datetime.now())
                ))
            except Exception as e:
                logger.error(f"공지사항 응답 변환 실패 (id: {notice.id}): {e}")
                import traceback
                traceback.print_exc()
                raise
        
        # total_pages 계산
        total_pages = (total + size - 1) // size if total > 0 else 1
        
        return NoticeListResponse(
            notices=notice_responses,
            total=total,
            page=page,
            size=size,
            total_pages=total_pages
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"공지사항 목록 조회 실패: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"공지사항 목록 조회 실패: {str(e)}")


# 공지 생성/수정/삭제/업로드는 /api/v1/admin/notices 에서 가능


@router.get("/{notice_id}", response_model=NoticeResponse)
async def get_notice(
    notice_id: int,
    db: Session = Depends(get_db)
):
    """공지사항 상세 조회"""
    try:
        notice = db.query(Notice).filter(Notice.id == notice_id).first()
        if not notice:
            raise HTTPException(status_code=404, detail="공지사항을 찾을 수 없습니다")
        
        # 조회수 증가
        notice.view_count = (notice.view_count or 0) + 1
        db.commit()
        
        view_count = getattr(notice, 'view_count', 0)
        
        return NoticeResponse(
            id=notice.id,            title=notice.title,
            content=notice.content,
            type=notice.type.value,
            is_important=notice.is_important,
            is_published=getattr(notice, 'is_published', False),
            view_count=view_count,
            published_at=getattr(notice, 'published_at', None),
            attachment_file=getattr(notice, 'attachment_file', None),
            web_view_link=getattr(notice, 'web_view_link', None),
            created_at=notice.created_at,
            updated_at=notice.updated_at
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"공지사항 조회 실패: {str(e)}")


@router.get("/types/", response_model=list[dict])
async def get_notice_types():
    """공지사항 타입 목록 조회"""
    return [
        {"id": "GENERAL", "name": "일반"},
        {"id": "SYSTEM", "name": "시스템"},
        {"id": "EVENT", "name": "이벤트"},
        {"id": "MAINTENANCE", "name": "점검"}
    ]


async def send_new_notice_notification(notice_id: int, db: Session):
    """새 공지사항 알림 전송"""
    try:
        from utils import generate_cuid
        from services.push_delivery_service import send_push_to_user
        
        # 공지사항 정보 조회
        notice = db.query(Notice).filter(Notice.id == notice_id).first()
        if not notice:
            logger.error(f"공지사항을 찾을 수 없습니다: {notice_id}")
            return
        
        # 모든 활성 사용자 조회
        users = db.query(User).filter(
            User.status == 'ACTIVE',
            User.deleted_at.is_(None)
        ).all()
        
        if not users:
            logger.info("알림을 받을 사용자가 없습니다.")
            return
        
        # 각 사용자에게 알림 전송
        sent_count = 0
        for user in users:
            try:
                notification = Notification(
                    user_id=user.id,
                    type=NotificationType.NEW_NOTICE.value,
                    title=f"새 공지사항 - {notice.title}",
                    content=f"""
새로운 공지사항이 등록되었습니다.

📢 제목: {notice.title}
📅 등록일: {getattr(notice, 'published_at', None).strftime('%Y년 %m월 %d일 %H:%M') if getattr(notice, 'published_at', None) else '알 수 없음'}
{'⭐ 중요 공지사항입니다.' if getattr(notice, 'is_important', False) else ''}

자세한 내용은 공지사항에서 확인해주세요.
                    """.strip(),
                    status=NotificationStatus.UNREAD.value
                )
                
                db.add(notification)
                send_push_to_user(db=db,
                                  user_id=user.id,
                                  title=notification.title,
                                  content=notification.content,
                                  category="notice",
                                  target_id=notice.id)
                sent_count += 1
                
            except Exception as e:
                logger.error(f"새 공지사항 알림 생성 실패 - user_id: {user.id}, error: {str(e)}")
                continue
        
        db.commit()
        logger.info(f"새 공지사항 알림 전송 완료 - notice_id: {notice_id}, sent_count: {sent_count}")
        
    except Exception as e:
        logger.error(f"새 공지사항 알림 전송 실패: {e}")
        db.rollback()
        raise
