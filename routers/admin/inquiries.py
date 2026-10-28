"""
백오피스 문의 관리 API
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
import logging

from database import get_db
from models import User
from utils.datetime_utils import get_kst_now
from .deps import get_admin_user
from .pagination import BulkRequest, PageParams, apply_sort, bulk_run, paginate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-inquiries"])


@router.get("/inquiries")
async def get_admin_inquiries(
        params: PageParams = Depends(),
        status_filter: list[str] = Query([], alias="status"),
        type_filter: str | None = Query(None, alias="type"),
        date_from: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
        date_to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 문의 목록 (서버 페이징·필터 — 예전에는 최신 20건 고정이라 21번째 이후 문의를 볼 수 없었다)"""
    from datetime import datetime, time

    from sqlalchemy import func, or_

    from models import Inquiry, InquiryResponse, InquiryStatus, InquiryType

    try:
        statuses = [InquiryStatus(s) for s in status_filter]
        inquiry_type = InquiryType(type_filter) if type_filter else None
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"알 수 없는 값입니다: {e}") from e

    response_count = (
        db.query(InquiryResponse.inquiry_id, func.count(InquiryResponse.id).label("cnt"))
        .group_by(InquiryResponse.inquiry_id)
        .subquery()
    )
    query = (
        db.query(Inquiry, User, response_count.c.cnt)
        .join(User, User.id == Inquiry.user_id)
        .outerjoin(response_count, response_count.c.inquiry_id == Inquiry.id)
    )
    if statuses:
        query = query.filter(Inquiry.status.in_(statuses))
    if inquiry_type:
        query = query.filter(Inquiry.type == inquiry_type)
    if params.q:
        query = query.filter(or_(Inquiry.title.like(f"%{params.q}%"), User.nickname.like(f"%{params.q}%")))
    if date_from:
        query = query.filter(Inquiry.created_at >= datetime.combine(datetime.fromisoformat(date_from), time.min))
    if date_to:
        query = query.filter(Inquiry.created_at <= datetime.combine(datetime.fromisoformat(date_to), time.max))
    query = apply_sort(query, params.sort, {"created_at": Inquiry.created_at}, default=(Inquiry.created_at.desc(), Inquiry.id.desc()))

    def item(row) -> dict:
        i, u, cnt = row
        return {
            "id": i.id,
            "title": i.title,
            "type": i.type.value,
            "status": i.status.value,
            "user": {"id": u.id, "nickname": u.nickname},
            "user_nickname": u.nickname,
            "response_count": cnt or 0,
            "created_at": i.created_at.isoformat() if i.created_at else None,
        }

    page = paginate(query, params, item)
    # 교체 배포(BO#33) 전까지 기존 백오피스가 읽는 키. 교체 후 제거
    page["data"] = page["items"]
    return page


@router.post("/inquiries/bulk")
async def bulk_admin_inquiries(body: BulkRequest, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """일괄 상태 변경 (API-041)"""
    from models import Inquiry, InquiryStatus

    if body.action != "status" or not body.status:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="action=status 와 status 가 필요합니다")
    try:
        new_status = InquiryStatus(body.status)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"알 수 없는 상태입니다: {body.status}") from e

    def one(inquiry_id: int) -> None:
        inquiry = db.query(Inquiry).filter(Inquiry.id == inquiry_id).first()
        if not inquiry:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="문의를 찾을 수 없습니다")
        inquiry.status = new_status
        inquiry.updated_at = get_kst_now()
        db.commit()

    return bulk_run(body.ids, one)


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
        from models import Admin

        responses = (
            db.query(InquiryResponse, Admin.name)
            .outerjoin(Admin, Admin.id == InquiryResponse.admin_id)
            .filter(InquiryResponse.inquiry_id == inquiry_id)
            .order_by(InquiryResponse.created_at, InquiryResponse.id)
            .all()
        )
        response_list = []
        for r, admin_name in responses:
            response_list.append({
                "id": r.id,
                "content": r.content,
                "is_admin": True,
                "is_internal": bool(r.is_internal),
                "admin_name": admin_name,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        return {
            "inquiry": {
                "id": inquiry.id, "title": inquiry.title, "content": inquiry.content,
                "type": inquiry.type.value, "status": inquiry.status.value,
                "user_id": inquiry.user_id,
                "user_nickname": inquiry.user.nickname if inquiry.user else "알 수 없음",
                "created_at": inquiry.created_at.isoformat() if inquiry.created_at else None,
                "updated_at": inquiry.updated_at.isoformat() if inquiry.updated_at else None,
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
        from models import InquiryStatus

        content = (response_data.get("content") or "").strip()
        if not content:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="답변 내용을 입력해주세요")
        is_internal = bool(response_data.get("is_internal", False))
        set_status = response_data.get("set_status")
        if set_status:
            try:
                inquiry.status = InquiryStatus(set_status)
            except ValueError as e:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"알 수 없는 상태입니다: {set_status}") from e
            inquiry.updated_at = get_kst_now()
        new_response = InquiryResponse(
            inquiry_id=inquiry_id,
            admin_id=current_user["id"],
            content=content,
            is_internal=is_internal,
        )
        db.add(new_response)
        db.commit()
        # 내부 메모는 운영자끼리 보는 기록이다. 회원에게 알림을 보내면 메모 내용이 그대로 노출된다
        if not is_internal:
            try:
                from utils.notification_service import create_inquiry_response_notification
                create_inquiry_response_notification(
                    db=db,
                    user_id=inquiry.user_id,
                    inquiry_title=inquiry.title,
                    response_content=content,
                )
            except Exception as e:
                logger.error(f"문의 답변 알림 전송 실패: {str(e)}")
        return {"message": "답변이 성공적으로 생성되었습니다", "id": new_response.id, "status": inquiry.status.value}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 문의 답변 생성 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="문의 답변 생성 중 오류가 발생했습니다")
