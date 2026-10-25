"""
백오피스 결제/플랜/구독/환불 API
"""
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
import logging

from database import get_db
from models import ClubMembership, Meeting, MeetingParticipant
from schemas import MeetingType
from .deps import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-payments"])


@router.get("/payments")
async def get_admin_payments(
    page: int = 1,
    size: int = 20,
    status_filter: str | None = None,
    user_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 결제 목록 조회"""
    try:
        from models import Payment, PaymentStatus
        from sqlalchemy import desc

        query = db.query(Payment)
        if status_filter:
            query = query.filter(Payment.status == PaymentStatus(status_filter))
        if user_id:
            query = query.filter(Payment.user_id == user_id)
        total = query.count()
        payments = query.order_by(desc(Payment.created_at)).offset((page - 1) * size).limit(size).all()
        payment_responses = []
        for p in payments:
            payment_responses.append({
                "id": p.id, "user_id": p.user_id, "subscription_id": p.subscription_id,
                "amount": float(p.amount), "currency": p.currency, "status": p.status.value,
                "payment_method": p.payment_method.value,
                "payment_provider": getattr(p, "payment_provider", "TOSS_PAYMENTS"),
                "provider_payment_id": getattr(p, "provider_payment_id", ""),
                "payment_key": p.payment_key, "order_id": p.order_id, "order_name": p.order_name,
                "transaction_id": p.transaction_id,
                "paid_at": p.paid_at.isoformat() if p.paid_at else None,
                "canceled_at": p.canceled_at.isoformat() if p.canceled_at else None,
                "cancel_reason": p.cancel_reason, "description": p.description,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            })
        return {"data": payment_responses, "total": total, "page": page, "size": size}
    except Exception as e:
        logger.error(f"관리자 결제 목록 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="결제 목록 조회 중 오류가 발생했습니다",
        )


@router.get("/plans")
async def get_admin_plans(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 플랜 목록 조회"""
    try:
        from models import Plan
        from sqlalchemy import desc

        plans = db.query(Plan).order_by(desc(Plan.created_at)).limit(20).all()
        plan_list = []
        for p in plans:
            plan_list.append({
                "id": p.id, "name": p.name, "description": p.description,
                "detailed_description": getattr(p, "detailed_description", ""),
                "type": p.type.value if p.type else "BASIC",
                "price": float(p.price) if p.price else 0,
                "billing_cycle": p.billing_cycle.value if p.billing_cycle else "MONTHLY",
                "features": p.features, "max_clubs": p.max_clubs,
                "max_members_per_club": p.max_members_per_club,
                "max_meetings_per_month": p.max_meetings_per_month,
                "is_active": p.is_active,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            })
        return {"data": plan_list, "total": len(plan_list)}
    except Exception as e:
        logger.error(f"관리자 플랜 목록 조회 중 오류: {str(e)}")
        return {"data": [], "total": 0}


@router.post("/payments/{payment_id}/cancel")
async def admin_cancel_payment(
    payment_id: int,
    cancel_data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 결제 취소"""
    try:
        from models import Payment, PaymentStatus, Subscription

        payment = db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="결제 내역을 찾을 수 없습니다")
        if payment.status != PaymentStatus.SUCCEEDED:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="취소 가능한 결제가 아닙니다")
        user_id = payment.user_id
        payment_date = payment.created_at.date()
        today = date.today()
        club_memberships_count = db.query(ClubMembership).filter(
            ClubMembership.user_id == user_id, ClubMembership.created_at >= payment_date
        ).count()
        meeting_participations_count = db.query(MeetingParticipant).filter(
            MeetingParticipant.user_id == user_id, MeetingParticipant.created_at >= payment_date
        ).count()
        socials_count = db.query(Meeting).filter(
            Meeting.meeting_type == MeetingType.SOCIAL,
            Meeting.club_id.in_(db.query(ClubMembership.club_id).filter(ClubMembership.user_id == user_id)),
            Meeting.created_at >= payment_date,
        ).count()
        has_activity = club_memberships_count > 0 or meeting_participations_count > 0 or socials_count > 0
        is_same_day = payment_date == today
        policy_info = {
            "is_same_day": is_same_day, "has_activity": has_activity,
            "club_memberships": club_memberships_count,
            "meeting_participations": meeting_participations_count,
            "socials_created": socials_count,
        }
        if is_same_day and not has_activity:
            if payment.payment_key:
                from utils.toss_payments import toss_payments
                try:
                    toss_result = toss_payments.cancel_payment(
                        payment_key=payment.payment_key,
                        cancel_reason=cancel_data.get("reason", "관리자 취소"),
                        cancel_amount=cancel_data.get("amount"),
                    )
                    payment.status = PaymentStatus.CANCELED
                    payment.canceled_at = datetime.now()
                    payment.cancel_reason = cancel_data.get("reason", "관리자 취소")
                    db.commit()
                    return {"success": True, "cancel_type": "refund", "message": "결제가 성공적으로 취소되었습니다. (즉시 환불)", "toss_result": toss_result, "policy_info": policy_info}
                except Exception as toss_error:
                    logger.error(f"토스페이먼츠 취소 실패: {str(toss_error)}")
                    if "404" in str(toss_error) or "Not Found" in str(toss_error):
                        payment.status = PaymentStatus.CANCELED
                        payment.canceled_at = datetime.now()
                        payment.cancel_reason = cancel_data.get("reason", "관리자 취소")
                        db.commit()
                        return {"success": True, "cancel_type": "refund", "message": "결제가 성공적으로 취소되었습니다. (테스트 결제 - 내부 처리)", "policy_info": policy_info}
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"결제 취소 실패: {str(toss_error)}")
            else:
                payment.status = PaymentStatus.CANCELED
                payment.canceled_at = datetime.now()
                payment.cancel_reason = cancel_data.get("reason", "관리자 취소")
                db.commit()
                return {"success": True, "cancel_type": "refund", "message": "결제가 성공적으로 취소되었습니다. (즉시 환불)", "policy_info": policy_info}
        else:
            active_subscription = db.query(Subscription).filter(Subscription.user_id == user_id, Subscription.status == "ACTIVE").first()
            if active_subscription:
                active_subscription.status = "CANCELED"
                if hasattr(active_subscription, "CANCELED_at"):
                    active_subscription.CANCELED_at = datetime.now()
                if hasattr(active_subscription, "cancel_reason"):
                    active_subscription.cancel_reason = cancel_data.get("reason", "관리자 취소")
                db.commit()
                return {"success": True, "cancel_type": "subscription_cancel", "message": "구독이 다음달부터 해지됩니다. (현재 달은 유지)", "policy_info": {**policy_info, "subscription_id": active_subscription.id}}
            else:
                payment.status = PaymentStatus.CANCELED
                payment.canceled_at = datetime.now()
                payment.cancel_reason = cancel_data.get("reason", "관리자 취소")
                db.commit()
                return {"success": True, "cancel_type": "payment_cancel", "message": "결제가 취소되었습니다. (활성 구독 없음)", "policy_info": policy_info}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 결제 취소 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/refunds")
async def get_admin_refunds(
    page: int = 1,
    size: int = 20,
    status_filter: str | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 환불 내역 조회"""
    try:
        from models import Payment, PaymentStatus, User
        from sqlalchemy import desc

        query = db.query(Payment).join(User, Payment.user_id == User.id).filter(Payment.status == PaymentStatus.FAILED).filter(Payment.canceled_at.isnot(None))
        total = query.count()
        refunds = query.order_by(desc(Payment.canceled_at)).offset((page - 1) * size).limit(size).all()
        refund_responses = []
        for r in refunds:
            refund_responses.append({
                "id": r.id, "user_id": r.user_id,
                "user_name": r.user.nickname if r.user else "알 수 없음",
                "user_email": r.user.email if r.user else "알 수 없음",
                "amount": float(r.amount), "currency": r.currency,
                "payment_method": r.payment_method.value if r.payment_method else "알 수 없음",
                "order_name": r.order_name, "cancel_reason": r.cancel_reason,
                "canceled_at": r.canceled_at.isoformat() if r.canceled_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "payment_key": r.payment_key,
            })
        return {"data": refund_responses, "total": total, "page": page, "size": size, "total_pages": (total + size - 1) // size}
    except Exception as e:
        logger.error(f"관리자 환불 내역 조회 중 오류: {str(e)}")
        return {"data": [], "total": 0}


@router.post("/plans")
async def create_admin_plan(
    plan_data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 플랜 생성"""
    try:
        from models import Plan, PlanType, BillingCycle

        plan = Plan(
            name=plan_data.get("name"),
            description=plan_data.get("description"),
            detailed_description=plan_data.get("detailed_description", ""),
            type=PlanType(plan_data.get("type", "BASIC")),
            price=plan_data.get("price", 0),
            billing_cycle=BillingCycle(plan_data.get("billing_cycle", "MONTHLY")),
            features=plan_data.get("features", {}),
            max_clubs=plan_data.get("max_clubs"),
            max_members_per_club=plan_data.get("max_members_per_club"),
            max_meetings_per_month=plan_data.get("max_meetings_per_month"),
            is_active=plan_data.get("is_active", True),
        )
        db.add(plan)
        db.commit()
        db.refresh(plan)
        return {
            "id": plan.id, "name": plan.name, "description": plan.description,
            "detailed_description": getattr(plan, "detailed_description", ""),
            "type": plan.type.value, "price": float(plan.price),
            "billing_cycle": plan.billing_cycle.value, "features": plan.features,
            "max_clubs": plan.max_clubs, "max_members_per_club": plan.max_members_per_club,
            "max_meetings_per_month": plan.max_meetings_per_month, "is_active": plan.is_active,
            "created_at": plan.created_at.isoformat() if plan.created_at else None,
            "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
        }
    except Exception as e:
        logger.error(f"관리자 플랜 생성 중 오류: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail=f"플랜 생성 실패: {str(e)}")


@router.put("/plans/{plan_id}")
async def update_admin_plan(
    plan_id: int,
    plan_data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 플랜 수정"""
    try:
        from models import Plan, PlanType, BillingCycle

        plan = db.query(Plan).filter(Plan.id == plan_id).first()
        if not plan:
            raise HTTPException(status_code=404, detail="플랜을 찾을 수 없습니다")
        if "name" in plan_data:
            plan.name = plan_data["name"]
        if "description" in plan_data:
            plan.description = plan_data["description"]
        if "detailed_description" in plan_data:
            plan.detailed_description = plan_data["detailed_description"]
        if "type" in plan_data:
            plan.type = PlanType(plan_data["type"])
        if "price" in plan_data:
            plan.price = plan_data["price"]
        if "billing_cycle" in plan_data:
            plan.billing_cycle = BillingCycle(plan_data["billing_cycle"])
        if "features" in plan_data:
            plan.features = plan_data["features"]
        if "max_clubs" in plan_data:
            plan.max_clubs = plan_data["max_clubs"]
        if "max_members_per_club" in plan_data:
            plan.max_members_per_club = plan_data["max_members_per_club"]
        if "max_meetings_per_month" in plan_data:
            plan.max_meetings_per_month = plan_data["max_meetings_per_month"]
        if "is_active" in plan_data:
            plan.is_active = plan_data["is_active"]
        db.commit()
        db.refresh(plan)
        return {
            "id": plan.id, "name": plan.name, "description": plan.description,
            "detailed_description": getattr(plan, "detailed_description", ""),
            "type": plan.type.value, "price": float(plan.price),
            "billing_cycle": plan.billing_cycle.value, "features": plan.features,
            "max_clubs": plan.max_clubs, "max_members_per_club": plan.max_members_per_club,
            "max_meetings_per_month": plan.max_meetings_per_month, "is_active": plan.is_active,
            "created_at": plan.created_at.isoformat() if plan.created_at else None,
            "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 플랜 수정 중 오류: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail=f"플랜 수정 실패: {str(e)}")


@router.delete("/plans/{plan_id}")
async def delete_admin_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 플랜 삭제"""
    try:
        from models import Plan, Subscription

        plan = db.query(Plan).filter(Plan.id == plan_id).first()
        if not plan:
            raise HTTPException(status_code=404, detail="플랜을 찾을 수 없습니다")
        active_count = db.query(Subscription).filter(Subscription.plan_id == plan_id, Subscription.status == "ACTIVE").count()
        if active_count > 0:
            raise HTTPException(status_code=400, detail="활성 구독이 있는 플랜은 삭제할 수 없습니다")
        db.delete(plan)
        db.commit()
        return {"message": "플랜이 성공적으로 삭제되었습니다", "success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 플랜 삭제 중 오류: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail=f"플랜 삭제 실패: {str(e)}")


@router.get("/subscriptions")
async def get_admin_subscriptions(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 구독 목록 조회"""
    try:
        from models import Subscription
        from sqlalchemy import desc

        subscriptions = db.query(Subscription).order_by(desc(Subscription.created_at)).limit(20).all()
        subscription_list = []
        for s in subscriptions:
            subscription_list.append({
                "id": s.id, "user_id": s.user_id, "plan_id": s.plan_id,
                "status": s.status.value,
                "start_date": s.start_date.isoformat() if s.start_date else None,
                "end_date": s.end_date.isoformat() if s.end_date else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            })
        return {"data": subscription_list, "total": len(subscription_list)}
    except Exception as e:
        logger.error(f"관리자 구독 목록 조회 중 오류: {str(e)}")
        return {"data": [], "total": 0}
