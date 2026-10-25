"""
요금제 관리 API 라우터
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
import logging

from database import get_db
from models import Plan
from schemas import PlanType, BillingCycle
from schemas import PlanResponse
from routers.auth import get_current_user

# 로깅 설정
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plans", tags=["plans"])

@router.get("/", response_model=list[PlanResponse])
async def get_plans(
    is_active: bool | None = None,
    plan_type: str | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """요금제 목록 조회"""
    try:
        logger.info(f"요금제 목록 조회 시작 - user_id: {current_user.get('id')}")
        
        query = db.query(Plan)
        
        if is_active is not None:
            query = query.filter(Plan.is_active == is_active)
        
        if plan_type:
            query = query.filter(Plan.type == plan_type)
        
        plans = query.order_by(Plan.price.asc()).all()
        
        plan_responses = []
        for plan in plans:
            plan_response = PlanResponse(
                id=plan.id,                name=plan.name,
                description=plan.description,
                type=plan.type.value,
                price=float(plan.price),
                billing_cycle=plan.billing_cycle.value,
                features=plan.features,
                max_clubs=plan.max_clubs,
                max_members_per_club=plan.max_members_per_club,
                max_meetings_per_month=plan.max_meetings_per_month,
                is_active=plan.is_active,
                created_at=plan.created_at,
                updated_at=plan.updated_at
            )
            plan_responses.append(plan_response)
        
        logger.info(f"요금제 목록 조회 완료 - 총 {len(plan_responses)}개")
        return plan_responses
        
    except Exception as e:
        logger.error(f"요금제 목록 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}"
        )

@router.get("/{plan_id}", response_model=PlanResponse)
async def get_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """요금제 상세 조회"""
    try:
        logger.info(f"요금제 상세 조회 시작 - plan_id: {plan_id}")
        
        plan = db.query(Plan).filter(Plan.id == plan_id).first()
        if not plan:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="요금제를 찾을 수 없습니다"
            )
        
        plan_response = PlanResponse(
            id=plan.id,            name=plan.name,
            description=plan.description,
            type=plan.type.value,
            price=float(plan.price),
            billing_cycle=plan.billing_cycle.value,
            features=plan.features,
            max_clubs=plan.max_clubs,
            max_members_per_club=plan.max_members_per_club,
            max_meetings_per_month=plan.max_meetings_per_month,
            is_active=plan.is_active,
            created_at=plan.created_at,
            updated_at=plan.updated_at
        )
        
        logger.info(f"요금제 상세 조회 완료 - plan_id: {plan_id}")
        return plan_response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"요금제 상세 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 오류: {str(e)}"
        )

# 요금제 생성/수정/삭제는 /api/v1/admin/plans (admin/payments) 에서 가능

