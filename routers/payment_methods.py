"""
결제 수단 관리 API 라우터
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime
import logging
import secrets
import string

from database import get_db
from models import UserPaymentMethod, User
from schemas import PaymentMethod, PaymentMethodStatus
from schemas import (
    PaymentMethodCreate, PaymentMethodUpdate, PaymentMethodResponse, MessageResponse
)
from routers.auth import get_current_user

# 로깅 설정
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payment-methods", tags=["payment-methods"])

def generate_payment_method_id() -> str:
    """결제 수단 ID 생성"""
    chars = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(20))

@router.get("/", response_model=list[PaymentMethodResponse])
async def get_my_payment_methods(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """내 결제 수단 목록 조회"""
    try:
        logger.info(f"내 결제 수단 조회 시작 - user_id: {current_user.get('id')}")
        
        payment_methods = db.query(UserPaymentMethod).filter(
            UserPaymentMethod.user_id == current_user.get('id'),
            UserPaymentMethod.status == PaymentMethodStatus.ACTIVE
        ).order_by(UserPaymentMethod.is_default.desc(), UserPaymentMethod.created_at.desc()).all()
        
        result = []
        for method in payment_methods:
            result.append(PaymentMethodResponse(
                id=method.id,                user_id=method.user_id,
                method_type=method.method_type.value,
                method_name=method.method_name,
                masked_info=method.masked_info,
                is_default=method.is_default,
                status=method.status.value,
                expires_at=method.expires_at,
                last_used_at=method.last_used_at,
                extra_data=method.extra_data,
                created_at=method.created_at,
                updated_at=method.updated_at
            ))
        
        logger.info(f"내 결제 수단 조회 완료 - 총 {len(result)}개")
        return result
        
    except Exception as e:
        logger.error(f"내 결제 수단 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}"
        )

@router.post("/", response_model=PaymentMethodResponse)
async def create_payment_method(
    payment_method_data: PaymentMethodCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """결제 수단 등록"""
    try:
        logger.info(f"결제 수단 등록 시작 - user_id: {current_user.get('id')}")
        
        # 기본 결제 수단으로 설정하는 경우, 기존 기본 결제 수단 해제
        if payment_method_data.is_default:
            existing_default = db.query(UserPaymentMethod).filter(
                UserPaymentMethod.user_id == current_user.get('id'),
                UserPaymentMethod.is_default == True,
                UserPaymentMethod.status == PaymentMethodStatus.ACTIVE
            ).first()
            
            if existing_default:
                existing_default.is_default = False
                db.commit()
        
        # 결제 수단 생성
        payment_method = UserPaymentMethod(
            user_id=current_user.get('id'),
            method_type=PaymentMethod(payment_method_data.method_type),
            method_name=payment_method_data.method_name,
            masked_info=payment_method_data.masked_info,
            is_default=payment_method_data.is_default,
            status=PaymentMethodStatus.ACTIVE,
            expires_at=payment_method_data.expires_at,
            extra_data=payment_method_data.extra_data
        )
        
        db.add(payment_method)
        db.commit()
        db.refresh(payment_method)
        
        result = PaymentMethodResponse(
            id=payment_method.id,            user_id=payment_method.user_id,
            method_type=payment_method.method_type.value,
            method_name=payment_method.method_name,
            masked_info=payment_method.masked_info,
            is_default=payment_method.is_default,
            status=payment_method.status.value,
            expires_at=payment_method.expires_at,
            last_used_at=payment_method.last_used_at,
            extra_data=payment_method.extra_data,
            created_at=payment_method.created_at,
            updated_at=payment_method.updated_at
        )
        
        logger.info(f"결제 수단 등록 완료 - payment_method_id: {payment_method.id}")
        return result
        
    except Exception as e:
        logger.error(f"결제 수단 등록 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}"
        )

@router.get("/{payment_method_id}", response_model=PaymentMethodResponse)
async def get_payment_method(
    payment_method_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """결제 수단 상세 조회"""
    try:
        logger.info(f"결제 수단 상세 조회 시작 - payment_method_id: {payment_method_id}")
        
        payment_method = db.query(UserPaymentMethod).filter(
            UserPaymentMethod.id == payment_method_id,
            UserPaymentMethod.user_id == current_user.get('id')
        ).first()
        
        if not payment_method:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="결제 수단을 찾을 수 없습니다"
            )
        
        result = PaymentMethodResponse(
            id=payment_method.id,            user_id=payment_method.user_id,
            method_type=payment_method.method_type.value,
            method_name=payment_method.method_name,
            masked_info=payment_method.masked_info,
            is_default=payment_method.is_default,
            status=payment_method.status.value,
            expires_at=payment_method.expires_at,
            last_used_at=payment_method.last_used_at,
            extra_data=payment_method.extra_data,
            created_at=payment_method.created_at,
            updated_at=payment_method.updated_at
        )
        
        logger.info(f"결제 수단 상세 조회 완료 - payment_method_id: {payment_method_id}")
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"결제 수단 상세 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}"
        )

@router.put("/{payment_method_id}", response_model=PaymentMethodResponse)
async def update_payment_method(
    payment_method_id: int,
    payment_method_data: PaymentMethodUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """결제 수단 수정"""
    try:
        logger.info(f"결제 수단 수정 시작 - payment_method_id: {payment_method_id}")
        
        payment_method = db.query(UserPaymentMethod).filter(
            UserPaymentMethod.id == payment_method_id,
            UserPaymentMethod.user_id == current_user.get('id')
        ).first()
        
        if not payment_method:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="결제 수단을 찾을 수 없습니다"
            )
        
        # 기본 결제 수단으로 설정하는 경우, 기존 기본 결제 수단 해제
        if payment_method_data.is_default and not payment_method.is_default:
            existing_default = db.query(UserPaymentMethod).filter(
                UserPaymentMethod.user_id == current_user.get('id'),
                UserPaymentMethod.is_default == True,
                UserPaymentMethod.status == PaymentMethodStatus.ACTIVE,
                UserPaymentMethod.id != payment_method_id
            ).first()
            
            if existing_default:
                existing_default.is_default = False
                db.commit()
        
        # 결제 수단 정보 업데이트
        if payment_method_data.method_name is not None:
            payment_method.method_name = payment_method_data.method_name
        if payment_method_data.is_default is not None:
            payment_method.is_default = payment_method_data.is_default
        if payment_method_data.status is not None:
            payment_method.status = PaymentMethodStatus(payment_method_data.status)
        if payment_method_data.expires_at is not None:
            payment_method.expires_at = payment_method_data.expires_at
        if payment_method_data.extra_data is not None:
            payment_method.extra_data = payment_method_data.extra_data
        
        payment_method.updated_at = datetime.now()
        
        db.commit()
        db.refresh(payment_method)
        
        result = PaymentMethodResponse(
            id=payment_method.id,            user_id=payment_method.user_id,
            method_type=payment_method.method_type.value,
            method_name=payment_method.method_name,
            masked_info=payment_method.masked_info,
            is_default=payment_method.is_default,
            status=payment_method.status.value,
            expires_at=payment_method.expires_at,
            last_used_at=payment_method.last_used_at,
            extra_data=payment_method.extra_data,
            created_at=payment_method.created_at,
            updated_at=payment_method.updated_at
        )
        
        logger.info(f"결제 수단 수정 완료 - payment_method_id: {payment_method_id}")
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"결제 수단 수정 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}"
        )

@router.delete("/{payment_method_id}", response_model=MessageResponse)
async def delete_payment_method(
    payment_method_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """결제 수단 삭제"""
    try:
        logger.info(f"결제 수단 삭제 시작 - payment_method_id: {payment_method_id}")
        
        payment_method = db.query(UserPaymentMethod).filter(
            UserPaymentMethod.id == payment_method_id,
            UserPaymentMethod.user_id == current_user.get('id')
        ).first()
        
        if not payment_method:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="결제 수단을 찾을 수 없습니다"
            )
        
        # 기본 결제 수단인 경우 다른 결제 수단을 기본으로 설정
        if payment_method.is_default:
            other_method = db.query(UserPaymentMethod).filter(
                UserPaymentMethod.user_id == current_user.get('id'),
                UserPaymentMethod.id != payment_method_id,
                UserPaymentMethod.status == PaymentMethodStatus.ACTIVE
            ).first()
            
            if other_method:
                other_method.is_default = True
                db.commit()
        
        # 결제 수단 삭제 (실제로는 비활성화)
        payment_method.status = PaymentMethodStatus.INACTIVE
        payment_method.updated_at = datetime.now()
        
        db.commit()
        
        logger.info(f"결제 수단 삭제 완료 - payment_method_id: {payment_method_id}")
        return MessageResponse(
            message="결제 수단이 성공적으로 삭제되었습니다.",
            success=True
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"결제 수단 삭제 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}"
        )

@router.put("/{payment_method_id}/set-default", response_model=MessageResponse)
async def set_default_payment_method(
    payment_method_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """기본 결제 수단 설정"""
    try:
        logger.info(f"기본 결제 수단 설정 시작 - payment_method_id: {payment_method_id}")
        
        payment_method = db.query(UserPaymentMethod).filter(
            UserPaymentMethod.id == payment_method_id,
            UserPaymentMethod.user_id == current_user.get('id'),
            UserPaymentMethod.status == PaymentMethodStatus.ACTIVE
        ).first()
        
        if not payment_method:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="결제 수단을 찾을 수 없습니다"
            )
        
        # 기존 기본 결제 수단 해제
        existing_default = db.query(UserPaymentMethod).filter(
            UserPaymentMethod.user_id == current_user.get('id'),
            UserPaymentMethod.is_default == True,
            UserPaymentMethod.status == PaymentMethodStatus.ACTIVE
        ).first()
        
        if existing_default:
            existing_default.is_default = False
            db.commit()
        
        # 새로운 기본 결제 수단 설정
        payment_method.is_default = True
        payment_method.updated_at = datetime.now()
        
        db.commit()
        
        logger.info(f"기본 결제 수단 설정 완료 - payment_method_id: {payment_method_id}")
        return MessageResponse(
            message="기본 결제 수단이 성공적으로 설정되었습니다.",
            success=True
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"기본 결제 수단 설정 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}"
        )
