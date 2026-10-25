"""
백오피스 문의 관리 API
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
import logging

from database import get_db
from models import User
from utils.datetime_utils import get_kst_now
from .deps import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-inquiries"])


@router.get("/inquiries")
async def get_admin_inquiries(
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 문의 목록 조회"""
    try:
        from models import Inquiry
        from sqlalchemy import desc

        inquiries = db.query(Inquiry).join(User).order_by(desc(Inquiry.created_at)).limit(20).all()
        inquiry_list = []
        for i in inquiries:
            inquiry_list.append({
                "id": i.id, "title": i.title, "type": i.type.value, "status": i.status.value,
                "user_nickname": i.user.nickname if i.user else "알 수 없음",
                "created_at": i.created_at.isoformat() if i.created_at else None,
            })
        return {"data": inquiry_list, "total": len(inquiry_list)}
    except Exception as e:
        logger.error(f"관리자 문의 목록 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="문의 목록 조회 중 오류가 발생했습니다")


@router.get("/inquiries/{inquiry_id}")
async def get_admin_inquiry_detail(
        inquiry_id: int,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 문의 상세 조회"""
    try:
        from models import Inquiry, InquiryResponse

        inquiry = db.query(Inquiry).filter(Inquiry.id == inquiry_id).first()
        if not inquiry:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="문의를 찾을 수 없습니다")
        responses = db.query(InquiryResponse).filter(InquiryResponse.inquiry_id == inquiry_id).order_by(
            InquiryResponse.created_at).all()
        response_list = []
        for r in responses:
            response_list.append({
                "id": r.id,
                "content": r.content,
                "is_admin": True,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        return {
            "inquiry": {
                "id": inquiry.id, "title": inquiry.title, "content": inquiry.content,
                "type": inquiry.type.value, "status": inquiry.status.value,
                "user_nickname": inquiry.user.nickname if inquiry.user else "알 수 없음",
                "created_at": inquiry.created_at.isoformat() if inquiry.created_at else None,
            },
            "responses": response_list,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 문의 상세 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="문의 상세 조회 중 오류가 발생했습니다")


@router.put("/inquiries/{inquiry_id}/status")
async def update_admin_inquiry_status(
        inquiry_id: int,
        status_data: dict,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 문의 상태 업데이트"""
    try:
        from models import Inquiry, InquiryStatus

        inquiry = db.query(Inquiry).filter(Inquiry.id == inquiry_id).first()
        if not inquiry:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="문의를 찾을 수 없습니다")
        new_status = status_data.get("status")
        if new_status:
            inquiry.status = InquiryStatus(new_status)
            db.commit()
        return {"message": "문의 상태가 성공적으로 업데이트되었습니다"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 문의 상태 업데이트 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="문의 상태 업데이트 중 오류가 발생했습니다")


@router.put("/inquiries/{inquiry_id}")
async def update_admin_inquiry(
    inquiry_id: int,
    inquiry_data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 문의 수정"""
    try:
        from models import Inquiry, InquiryType, InquiryStatus

        inquiry = db.query(Inquiry).filter(Inquiry.id == inquiry_id).first()
        if not inquiry:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="문의를 찾을 수 없습니다")
        update_data = {k: v for k, v in inquiry_data.items() if k in ("title", "content", "type", "status") and v is not None}
        for field, value in update_data.items():
            if field == "type":
                inquiry.type = InquiryType(value)
            elif field == "status":
                inquiry.status = InquiryStatus(value)
            else:
                setattr(inquiry, field, value)
        inquiry.updated_at = get_kst_now()
        db.commit()
        db.refresh(inquiry)
        return {
            "id": inquiry.id, "user_id": inquiry.user_id, "title": inquiry.title, "content": inquiry.content,
            "type": inquiry.type.value, "status": inquiry.status.value,
            "created_at": inquiry.created_at.isoformat() if inquiry.created_at else None,
            "updated_at": inquiry.updated_at.isoformat() if inquiry.updated_at else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"문의 수정 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.put("/inquiries/{inquiry_id}/responses/{response_id}")
async def update_admin_inquiry_response(
    inquiry_id: int,
    response_id: int,
    response_data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 문의 답변 수정"""
    try:
        from models import InquiryResponse

        response = db.query(InquiryResponse).filter(InquiryResponse.id == response_id, InquiryResponse.inquiry_id == inquiry_id).first()
        if not response:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="답변을 찾을 수 없습니다")
        if "content" in response_data:
            response.content = response_data["content"]
        if "is_internal" in response_data:
            response.is_internal = response_data["is_internal"]
        db.commit()
        db.refresh(response)
        return {"message": "답변이 수정되었습니다", "id": response.id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"답변 수정 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.delete("/inquiries/{inquiry_id}/responses/{response_id}")
async def delete_admin_inquiry_response(
    inquiry_id: int,
    response_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 문의 답변 삭제"""
    try:
        from models import InquiryResponse

        response = db.query(InquiryResponse).filter(InquiryResponse.id == response_id, InquiryResponse.inquiry_id == inquiry_id).first()
        if not response:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="답변을 찾을 수 없습니다")
        db.delete(response)
        db.commit()
        return {"message": "답변이 삭제되었습니다"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"답변 삭제 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/inquiries/{inquiry_id}/responses")
async def create_admin_inquiry_response(
        inquiry_id: int,
        response_data: dict,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 문의 답변 생성"""
    try:
        from models import Inquiry, InquiryResponse

        inquiry = db.query(Inquiry).filter(Inquiry.id == inquiry_id).first()
        if not inquiry:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="문의를 찾을 수 없습니다")
        new_response = InquiryResponse(
            inquiry_id=inquiry_id,
            admin_id=current_user["id"],
            content=response_data.get("content", ""),
            is_internal=response_data.get("is_internal", False),
        )
        db.add(new_response)
        db.commit()
        try:
            from utils.notification_service import create_inquiry_response_notification
            create_inquiry_response_notification(
                db=db,
                user_id=inquiry.user_id,
                inquiry_title=inquiry.title,
                response_content=response_data.get("content", ""),
            )
        except Exception as e:
            logger.error(f"문의 답변 알림 전송 실패: {str(e)}")
        return {"message": "답변이 성공적으로 생성되었습니다"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 문의 답변 생성 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="문의 답변 생성 중 오류가 발생했습니다")
