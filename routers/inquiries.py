from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc, asc
from typing import Optional
from datetime import datetime
import logging

from database import get_db
from models import Inquiry, InquiryResponse, User
from schemas import InquiryType, InquiryStatus
from schemas import (InquiryCreate, InquiryUpdate, InquiryResponse as InquiryResponseSchema, InquiryListResponse,
                     InquiryResponseCreate, InquiryResponseUpdate, InquiryResponseResponse, InquiryDetailResponse)
from routers.auth import get_current_user
from utils import generate_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inquiries", tags=["inquiries"])


@router.get("/", response_model=InquiryListResponse)
async def get_my_inquiries(
        page: int = Query(1, ge=1),
        size: int = Query(20, ge=1, le=100),
        current_user=Depends(get_current_user),
        db: Session = Depends(get_db),
):
    """내 문의 목록 조회 (본인 문의만)"""
    try:
        query = db.query(Inquiry).filter(Inquiry.user_id == current_user.id)
        total = query.count()
        inquiries = query.order_by(desc(Inquiry.created_at)).offset((page - 1) * size).limit(size).all()
        inquiry_responses = [
            InquiryResponseSchema(id=i.id,
                                  user_id=i.user_id,
                                  user_name=getattr(current_user, "realname", None)
                                  or getattr(current_user, "nickname", "나"),
                                  user_nickname=getattr(current_user, "nickname", "나"),
                                  title=i.title,
                                  content=i.content,
                                  type=i.type.value,
                                  status=i.status.value,
                                  created_at=i.created_at,
                                  updated_at=i.updated_at) for i in inquiries
        ]
        total_pages = max(1, (total + size - 1) // size) if size else 1
        return InquiryListResponse(inquiries=inquiry_responses,
                                   total=total,
                                   page=page,
                                   size=size,
                                   total_pages=total_pages)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"문의 목록 조회 실패: {str(e)}")


@router.post("/", response_model=InquiryResponseSchema)
async def create_inquiry(inquiry_data: InquiryCreate,
                         current_user=Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """문의 등록"""
    try:
        # 문의 생성
        type_val = inquiry_data.type.value if hasattr(inquiry_data.type, "value") else str(inquiry_data.type)
        inquiry = Inquiry(
            user_id=current_user.id,
            title=inquiry_data.title,
            content=inquiry_data.content,
            type=type_val,
        )

        db.add(inquiry)
        db.commit()
        db.refresh(inquiry)

        # 관리자에게 새 문의 등록 알림 전송
        try:
            from utils.notification_service import create_inquiry_admin_notification
            create_inquiry_admin_notification(
                db=db,
                inquiry_title=inquiry_data.title,
                inquiry_type=inquiry_data.type.value if hasattr(inquiry_data.type, "value") else str(inquiry_data.type),
                user_name=current_user.nickname,
                inquiry_id=inquiry.id)
        except Exception as e:
            logger.error(f"관리자 문의 등록 알림 전송 실패: {str(e)}")

        return InquiryResponseSchema(
            id=inquiry.id,
            user_id=inquiry.user_id,
            user_name=getattr(inquiry.user, "realname", None) or (inquiry.user.nickname if inquiry.user else "알 수 없음"),
            user_nickname=inquiry.user.nickname if inquiry.user else "알 수 없음",
            title=inquiry.title,
            content=inquiry.content,
            type=inquiry.type.value,
            status=inquiry.status.value,
            created_at=inquiry.created_at,
            updated_at=inquiry.updated_at,
        )

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"문의 등록 실패: {str(e)}")


@router.get("/{inquiry_id}", response_model=InquiryDetailResponse)
async def get_inquiry(
        inquiry_id: int,
        current_user=Depends(get_current_user),
        db: Session = Depends(get_db),
):
    """문의 상세 조회 (본인 문의만)"""
    try:
        inquiry = db.query(Inquiry).filter(Inquiry.id == inquiry_id).first()
        if not inquiry:
            raise HTTPException(status_code=404, detail="문의를 찾을 수 없습니다")
        if inquiry.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="본인의 문의만 조회할 수 있습니다")
        # 내부 메모(is_internal)는 운영자 전용이다. 회원에게 내려주지 않는다
        responses = db.query(InquiryResponse).filter(
            InquiryResponse.inquiry_id == inquiry_id,
            InquiryResponse.is_internal.isnot(True),
        ).order_by(InquiryResponse.created_at).all()
        inquiry_response = InquiryResponseSchema(
            id=inquiry.id,
            user_id=inquiry.user_id,
            user_name=getattr(inquiry.user, "realname", None) or (inquiry.user.nickname if inquiry.user else "알 수 없음"),
            user_nickname=inquiry.user.nickname if inquiry.user else "알 수 없음",
            title=inquiry.title,
            content=inquiry.content,
            type=inquiry.type.value,
            status=inquiry.status.value,
            created_at=inquiry.created_at,
            updated_at=inquiry.updated_at,
        )
        response_responses = [
            InquiryResponseResponse(id=r.id,
                                    inquiry_id=r.inquiry_id,
                                    admin_id=r.admin_id,
                                    admin_name=r.admin.name if r.admin else "알 수 없음",
                                    content=r.content,
                                    is_internal=r.is_internal,
                                    created_at=r.created_at) for r in responses
        ]
        return InquiryDetailResponse(inquiry=inquiry_response,
                                     responses=response_responses,
                                     total_responses=len(response_responses))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"문의 조회 실패: {str(e)}")


# 문의 수정/답변 등록·수정·삭제는 /api/v1/admin/inquiries 에서 가능


@router.get("/types/", response_model=list[dict])
async def get_inquiry_types():
    """문의 타입 목록 조회 (InquiryType enum 기준)"""
    return [
        {
            "id": "GENERAL",
            "name": "일반 문의"
        },
        {
            "id": "TECHNICAL",
            "name": "기술 문의"
        },
        {
            "id": "BILLING",
            "name": "결제/청구 문의"
        },
        {
            "id": "FEATURE_REQUEST",
            "name": "기능 요청"
        },
        {
            "id": "BUG_REPORT",
            "name": "버그 신고"
        },
        {
            "id": "ACCOUNT",
            "name": "계정 문의"
        },
        {
            "id": "PAYMENT",
            "name": "결제 문의"
        },
    ]


@router.get("/statuses/", response_model=list[dict])
async def get_inquiry_statuses():
    """문의 상태 목록 조회 (InquiryStatus enum 기준)"""
    return [
        {
            "id": "PENDING",
            "name": "대기중"
        },
        {
            "id": "SUBMITTED",
            "name": "접수됨"
        },
        {
            "id": "IN_PROGRESS",
            "name": "처리중"
        },
        {
            "id": "RESOLVED",
            "name": "해결됨"
        },
        {
            "id": "COMPLETED",
            "name": "완료"
        },
        {
            "id": "CLOSED",
            "name": "종료"
        },
    ]
