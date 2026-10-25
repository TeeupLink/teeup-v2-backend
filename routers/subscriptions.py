"""
구독 관리 API 라우터
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timedelta
import logging

from database import get_db
from models import Subscription, Plan, User
from schemas import SubscriptionStatus
from schemas import SubscriptionCreate, SubscriptionUpdate, SubscriptionResponse, MessageResponse
from routers.auth import get_current_user

# 로깅 설정
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

def calculate_next_billing_date(billing_cycle: str, current_date: datetime = None) -> datetime:
    """다음 결제일 계산"""
    if current_date is None:
        current_date = datetime.now()
    
    if billing_cycle == "MONTHLY":
        return current_date + timedelta(days=30)
    elif billing_cycle == "YEARLY":
        return current_date + timedelta(days=365)
    else:
        return current_date + timedelta(days=30)

@router.get("/", response_model=list[SubscriptionResponse])
async def get_subscriptions(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """구독 목록 조회"""
    try:
        logger.info(f"구독 목록 조회 시작 - user_id: {current_user.get('id')}")
        
        subscriptions = db.query(Subscription).filter(
            Subscription.user_id == current_user.get('id')
        ).order_by(Subscription.created_at.desc()).all()
        
        subscription_responses = []
        for subscription in subscriptions:
            subscription_response = SubscriptionResponse(
                id=subscription.id,                user_id=subscription.user_id,
                plan_id=subscription.plan_id,
                plan_name=subscription.plan.name,
                status=subscription.status.value,
                start_date=subscription.start_date,
                end_date=subscription.end_date,
                next_billing_date=subscription.next_billing_date,
                canceled_at=subscription.canceled_at,
                cancel_reason=subscription.cancel_reason,
                created_at=subscription.created_at,
                updated_at=subscription.updated_at
            )
            subscription_responses.append(subscription_response)
        
        logger.info(f"구독 목록 조회 완료 - 총 {len(subscription_responses)}개")
        return subscription_responses
        
    except Exception as e:
        logger.error(f"구독 목록 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}"
        )

@router.get("/{subscription_id}", response_model=SubscriptionResponse)
async def get_subscription(
    subscription_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """구독 상세 조회"""
    try:
        logger.info(f"구독 상세 조회 시작 - subscription_id: {subscription_id}")
        
        subscription = db.query(Subscription).filter(
            Subscription.id == subscription_id,
            Subscription.user_id == current_user.get('id')
        ).first()
        
        if not subscription:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="구독을 찾을 수 없습니다"
            )
        
        subscription_response = SubscriptionResponse(
            id=subscription.id,            user_id=subscription.user_id,
            plan_id=subscription.plan_id,
            plan_name=subscription.plan.name,
            status=subscription.status.value,
            start_date=subscription.start_date,
            end_date=subscription.end_date,
            next_billing_date=subscription.next_billing_date,
            canceled_at=subscription.canceled_at,
            cancel_reason=subscription.cancel_reason,
            created_at=subscription.created_at,
            updated_at=subscription.updated_at
        )
        
        logger.info(f"구독 상세 조회 완료 - subscription_id: {subscription_id}")
        return subscription_response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"구독 상세 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}"
        )

@router.post("/", response_model=SubscriptionResponse)
async def create_subscription(
    subscription_data: SubscriptionCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """구독 생성"""
    try:
        logger.info(f"구독 생성 시작 - user_id: {current_user.get('id')}")
        
        # 요금제 조회
        plan = db.query(Plan).filter(Plan.id == subscription_data.plan_id).first()
        if not plan:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="요금제를 찾을 수 없습니다"
            )
        
        if not plan.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="비활성화된 요금제입니다"
            )
        
        # 기존 활성 구독 확인
        existing_subscription = db.query(Subscription).filter(
            Subscription.user_id == current_user.get('id'),
            Subscription.status == SubscriptionStatus.ACTIVE
        ).first()
        
        if existing_subscription:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="이미 활성 구독이 있습니다"
            )
        
        # 구독 생성
        import secrets
        import string
        chars = string.ascii_lowercase + string.digits
        subscription_id = ''.join(secrets.choice(chars) for _ in range(20))
        
        start_date = subscription_data.start_date or datetime.now()
        next_billing_date = calculate_next_billing_date(plan.billing_cycle.value, start_date)
        
        subscription = Subscription(
            id=subscription_id,
            user_id=current_user.get('id'),
            plan_id=subscription_data.plan_id,
            status=SubscriptionStatus.ACTIVE,
            start_date=start_date,
            next_billing_date=next_billing_date
        )
        
        db.add(subscription)
        db.commit()
        db.refresh(subscription)
        
        subscription_response = SubscriptionResponse(
            id=subscription.id,            user_id=subscription.user_id,
            plan_id=subscription.plan_id,
            plan_name=subscription.plan.name,
            status=subscription.status.value,
            start_date=subscription.start_date,
            end_date=subscription.end_date,
            next_billing_date=subscription.next_billing_date,
            canceled_at=subscription.canceled_at,
            cancel_reason=subscription.cancel_reason,
            created_at=subscription.created_at,
            updated_at=subscription.updated_at
        )
        
        logger.info(f"구독 생성 완료 - subscription_id: {subscription.id}")
        return subscription_response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"구독 생성 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}"
        )

@router.put("/{subscription_id}", response_model=SubscriptionResponse)
async def update_subscription(
    subscription_id: int,
    subscription_data: SubscriptionUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """구독 수정"""
    try:
        logger.info(f"구독 수정 시작 - subscription_id: {subscription_id}")
        
        # 구독 조회
        subscription = db.query(Subscription).filter(
            Subscription.id == subscription_id,
            Subscription.user_id == current_user.get('id')
        ).first()
        
        if not subscription:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="구독을 찾을 수 없습니다"
            )
        
        # 구독 정보 업데이트
        update_data = subscription_data.dict(exclude_unset=True)
        for field, value in update_data.items():
            if field == "status" and value:
                setattr(subscription, field, SubscriptionStatus(value))
            else:
                setattr(subscription, field, value)
        
        db.commit()
        db.refresh(subscription)
        
        subscription_response = SubscriptionResponse(
            id=subscription.id,            user_id=subscription.user_id,
            plan_id=subscription.plan_id,
            plan_name=subscription.plan.name,
            status=subscription.status.value,
            start_date=subscription.start_date,
            end_date=subscription.end_date,
            next_billing_date=subscription.next_billing_date,
            canceled_at=subscription.canceled_at,
            cancel_reason=subscription.cancel_reason,
            created_at=subscription.created_at,
            updated_at=subscription.updated_at
        )
        
        logger.info(f"구독 수정 완료 - subscription_id: {subscription_id}")
        return subscription_response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"구독 수정 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}"
        )

@router.delete("/{subscription_id}", response_model=MessageResponse)
async def cancel_subscription(
    subscription_id: int,
    cancel_reason: str = "사용자 요청",
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """구독 취소"""
    try:
        logger.info(f"구독 취소 시작 - subscription_id: {subscription_id}")
        
        # 구독 조회
        subscription = db.query(Subscription).filter(
            Subscription.id == subscription_id,
            Subscription.user_id == current_user.get('id')
        ).first()
        
        if not subscription:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="구독을 찾을 수 없습니다"
            )
        
        if subscription.status != SubscriptionStatus.ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="활성 구독이 아닙니다"
            )
        
        # 구독 취소
        subscription.status = SubscriptionStatus.CANCELED
        subscription.canceled_at = datetime.now()
        subscription.cancel_reason = cancel_reason
        
        db.commit()
        
        logger.info(f"구독 취소 완료 - subscription_id: {subscription_id}")
        return MessageResponse(
            message="구독이 성공적으로 취소되었습니다",
            success=True
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"구독 취소 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}"
        )


