# 클럽 회비 관리 API들
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
import logging

logger = logging.getLogger(__name__)

from database import get_db
from models import User, Club, ClubMembership, ClubFee
from schemas import (
    MessageResponse, RegularFeeUpdate, RegularFeeResponse, ClubRole,
    ClubFeeCreate, ClubFeeUpdate, ClubFeeResponse
)
from routers.auth import get_current_active_user
from utils.datetime_utils import get_kst_now

router = APIRouter(prefix="/clubs", tags=["클럽 회비 관리"])

@router.get("/{club_id}/regular-fee", response_model=RegularFeeResponse)
async def get_regular_fee(
    club_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """정기 회비 조회 (클럽 멤버만 가능) - ClubFee 엔티티에서 조회"""
    try:
        # 클럽 존재 확인 (display_id로 먼저 조회)
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
                detail="클럽 멤버만 정기 회비 정보를 조회할 수 있습니다."
            )
        
        # 활성화된 정기 회비 조회 (cycle이 MONTHLY, QUARTERLY, YEARLY인 활성 회비)
        from models.enums import BillingCycle
        regular_fee = db.query(ClubFee).filter(
            ClubFee.club_id == actual_club_id,
            ClubFee.is_active == True,
            ClubFee.cycle.in_([BillingCycle.MONTHLY, BillingCycle.QUARTERLY, BillingCycle.YEARLY])
        ).first()
        
        if regular_fee:
            return {
                "has_regular_fee": True,
                "regular_fee_amount": float(regular_fee.amount) if regular_fee.amount else None,
                "regular_fee_cycle": regular_fee.cycle.value if regular_fee.cycle else None,
                "regular_fee_description": regular_fee.description,
                "updated_at": regular_fee.updated_at.isoformat() if regular_fee.updated_at else None
            }
        else:
            return {
                "has_regular_fee": False,
                "regular_fee_amount": None,
                "regular_fee_cycle": None,
                "regular_fee_description": None,
                "updated_at": None
            }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.put("/{club_id}/regular-fee", response_model=MessageResponse)
async def update_regular_fee(
    club_id: str,
    fee_data: RegularFeeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """정기 회비 설정/수정 (리더/매니저만 가능) - ClubFee 엔티티로 저장"""
    try:
        # 클럽 조회 (display_id로 먼저 조회)
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
        
        # 권한 확인 (리더/매니저만 가능)
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,  # 실제 클럽 ID 사용
            ClubMembership.user_id == current_user.id,
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER])
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 리더나 매니저만 정기 회비를 설정할 수 있습니다."
            )
        
        # 정기 회비가 있는 경우 필수 필드 검증
        if fee_data.has_regular_fee:
            if fee_data.regular_fee_amount is None or fee_data.regular_fee_amount <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="정기 회비가 있는 경우 금액을 0보다 큰 값으로 설정해야 합니다."
                )
            
            if fee_data.regular_fee_cycle is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="정기 회비가 있는 경우 주기를 설정해야 합니다."
                )
        
        # 기존 정기 회비 조회 (cycle이 MONTHLY, QUARTERLY, YEARLY인 회비)
        from models.enums import BillingCycle
        existing_regular_fee = db.query(ClubFee).filter(
            ClubFee.club_id == club.id,
            ClubFee.cycle.in_([BillingCycle.MONTHLY, BillingCycle.QUARTERLY, BillingCycle.YEARLY])
        ).first()
        
        if fee_data.has_regular_fee:
            if existing_regular_fee:
                # 기존 정기 회비 업데이트
                existing_regular_fee.name = "정기 회비"
                existing_regular_fee.amount = fee_data.regular_fee_amount
                existing_regular_fee.cycle = fee_data.regular_fee_cycle
                existing_regular_fee.description = fee_data.regular_fee_description
                existing_regular_fee.is_active = True
                existing_regular_fee.updated_at = get_kst_now()
            else:
                # 새로운 정기 회비 생성
                new_fee = ClubFee(
                    club_id=club.id,
                    name="정기 회비",
                    amount=fee_data.regular_fee_amount,
                    cycle=fee_data.regular_fee_cycle,
                    description=fee_data.regular_fee_description,
                    is_active=True,
                    created_by=current_user.id
                )
                db.add(new_fee)
        else:
            # 정기 회비 비활성화
            if existing_regular_fee:
                existing_regular_fee.is_active = False
                existing_regular_fee.updated_at = get_kst_now()
        
        db.commit()
        
        if fee_data.has_regular_fee:
            return {
                "message": "정기 회비가 설정되었습니다.",
                "success": True
            }
        else:
            return {
                "message": "정기 회비가 비활성화되었습니다.",
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

# ===== 회비 항목 관리 =====

@router.get("/{club_id}/fees", response_model=list[ClubFeeResponse])
async def get_club_fees(
    club_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """회비 항목 목록 조회 (클럽 멤버만 가능)"""
    try:
        # 클럽 조회 (display_id로 먼저 조회)
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
        
        # 클럽 멤버인지 확인
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == current_user.id
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 멤버만 회비 항목을 조회할 수 있습니다."
            )
        
        # 회비 항목 목록 조회
        fees = db.query(ClubFee).filter(
            ClubFee.club_id == club.id
        ).order_by(ClubFee.created_at.desc()).all()
        
        # 응답 데이터 생성
        fee_responses = []
        for fee in fees:
            cycle_value = None
            if fee.cycle:
                if hasattr(fee.cycle, 'value'):
                    cycle_value = fee.cycle.value
                else:
                    cycle_value = str(fee.cycle)
            
            fee_responses.append({
                "id": fee.id,
                "club_id": fee.club_id,
                "name": fee.name,
                "amount": float(fee.amount) if fee.amount else 0,
                "cycle": cycle_value,
                "description": fee.description,
                "is_active": fee.is_active if fee.is_active is not None else True,
                "created_by": fee.created_by,
                "created_at": fee.created_at.isoformat() if fee.created_at else None,
                "updated_at": fee.updated_at.isoformat() if fee.updated_at else None
            })
        
        return fee_responses
        
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error(f"회비 항목 조회 중 오류: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )


@router.get("/{club_id}/fees/{fee_id}", response_model=ClubFeeResponse)
async def get_club_fee(
    club_id: str,
    fee_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """회비 항목 상세 조회 (클럽 멤버만 가능)"""
    try:
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()

        if not club:
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(
                    Club.id == club_id_int,
                    Club.deleted_at.is_(None)
                ).first()
            except ValueError:
                club = None

        if not club:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )

        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == current_user.id
        ).first()

        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 멤버만 회비 항목을 조회할 수 있습니다."
            )

        fee = db.query(ClubFee).filter(
            ClubFee.id == fee_id,
            ClubFee.club_id == club.id
        ).first()

        if not fee:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="회비 항목을 찾을 수 없습니다."
            )

        cycle_value = fee.cycle.value if hasattr(fee.cycle, 'value') else str(fee.cycle) if fee.cycle else None
        return {
            "id": fee.id,
            "club_id": fee.club_id,
            "name": fee.name,
            "amount": float(fee.amount),
            "cycle": cycle_value,
            "description": fee.description,
            "is_active": fee.is_active if fee.is_active is not None else True,
            "created_by": fee.created_by,
            "created_at": fee.created_at,
            "updated_at": fee.updated_at
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"회비 항목 조회 중 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )


@router.post("/{club_id}/fees", response_model=ClubFeeResponse)
async def create_club_fee(
    club_id: str,
    fee_data: ClubFeeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """회비 항목 생성 (리더/매니저만 가능)"""
    try:
        logger.info(f"회비 항목 생성 요청: club_id={club_id}, fee_data={fee_data}")
        # 클럽 조회 (display_id로 먼저 조회)
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
        
        # 권한 확인 (리더/매니저만 가능)
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == current_user.id,
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER])
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 리더나 매니저만 회비 항목을 생성할 수 있습니다."
            )
        
        # 회비 항목 생성
        from schemas import BillingCycle
        cycle_value = None
        if fee_data.cycle is not None:
            # Pydantic이 이미 Enum으로 변환했을 수도 있음
            if isinstance(fee_data.cycle, BillingCycle):
                cycle_value = fee_data.cycle
            elif hasattr(fee_data.cycle, 'value'):
                cycle_value = BillingCycle[fee_data.cycle.value]
            elif isinstance(fee_data.cycle, str):
                try:
                    cycle_value = BillingCycle[fee_data.cycle]
                except (KeyError, ValueError):
                    cycle_value = None
                    logger.warning(f"유효하지 않은 cycle 값: {fee_data.cycle}")
        
        new_fee = ClubFee(
            club_id=club.id,
            name=fee_data.name,
            amount=fee_data.amount,
            cycle=cycle_value,
            description=fee_data.description,
            is_active=fee_data.is_active,
            created_by=current_user.id
        )
        
        db.add(new_fee)
        db.commit()
        db.refresh(new_fee)
        
        # 응답 데이터 생성
        cycle_value = new_fee.cycle.value if hasattr(new_fee.cycle, 'value') else str(new_fee.cycle) if new_fee.cycle else None
        
        return {
            "id": new_fee.id,
            "club_id": new_fee.club_id,
            "name": new_fee.name,
            "amount": float(new_fee.amount),
            "cycle": cycle_value,
            "description": new_fee.description,
            "is_active": new_fee.is_active,
            "created_by": new_fee.created_by,
            "created_at": new_fee.created_at,
            "updated_at": new_fee.updated_at
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )

@router.put("/{club_id}/fees/{fee_id}", response_model=ClubFeeResponse)
async def update_club_fee(
    club_id: str,
    fee_id: int,
    fee_data: ClubFeeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """회비 항목 수정 (리더/매니저만 가능)"""
    try:
        logger.info(f"회비 항목 수정 요청: club_id={club_id}, fee_id={fee_id}, fee_data={fee_data}")
        # 클럽 조회 (display_id로 먼저 조회)
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
        
        # 권한 확인 (리더/매니저만 가능)
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == current_user.id,
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER])
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 리더나 매니저만 회비 항목을 수정할 수 있습니다."
            )
        
        # 회비 항목 조회
        fee = db.query(ClubFee).filter(
            ClubFee.id == fee_id,
            ClubFee.club_id == club.id
        ).first()
        
        if not fee:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="회비 항목을 찾을 수 없습니다."
            )
        
        # 회비 항목 수정
        if fee_data.name is not None:
            fee.name = fee_data.name
        if fee_data.amount is not None:
            fee.amount = fee_data.amount
        
        # cycle 처리 (Optional 필드이므로 None 체크로 처리)
        from schemas import BillingCycle
        if fee_data.cycle is not None:
            # Pydantic이 이미 Enum으로 변환했을 수도 있음
            if isinstance(fee_data.cycle, BillingCycle):
                fee.cycle = fee_data.cycle
            elif hasattr(fee_data.cycle, 'value'):
                fee.cycle = BillingCycle[fee_data.cycle.value]
            elif isinstance(fee_data.cycle, str) and fee_data.cycle.strip():
                try:
                    fee.cycle = BillingCycle[fee_data.cycle]
                except (KeyError, ValueError):
                    fee.cycle = None
                    logger.warning(f"유효하지 않은 cycle 값: {fee_data.cycle}")
            else:
                fee.cycle = None
        else:
            # cycle이 None이면 None으로 설정 (명시적으로 제거)
            fee.cycle = None
        
        # description 처리
        if fee_data.description is not None:
            fee.description = fee_data.description
        else:
            # description이 None이면 None으로 설정 (명시적으로 제거)
            fee.description = None
        
        if fee_data.is_active is not None:
            fee.is_active = fee_data.is_active
        
        fee.updated_at = get_kst_now()
        db.commit()
        db.refresh(fee)
        
        # 응답 데이터 생성
        cycle_value = fee.cycle.value if hasattr(fee.cycle, 'value') else str(fee.cycle) if fee.cycle else None
        
        return {
            "id": fee.id,
            "club_id": fee.club_id,
            "name": fee.name,
            "amount": float(fee.amount),
            "cycle": cycle_value,
            "description": fee.description,
            "is_active": fee.is_active,
            "created_by": fee.created_by,
            "created_at": fee.created_at,
            "updated_at": fee.updated_at
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )

@router.delete("/{club_id}/fees/{fee_id}", response_model=MessageResponse)
async def delete_club_fee(
    club_id: str,
    fee_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """회비 항목 삭제 (리더/매니저만 가능)"""
    try:
        # 클럽 조회 (display_id로 먼저 조회)
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
        
        # 권한 확인 (리더/매니저만 가능)
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == current_user.id,
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER])
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 리더나 매니저만 회비 항목을 삭제할 수 있습니다."
            )
        
        # 회비 항목 조회
        fee = db.query(ClubFee).filter(
            ClubFee.id == fee_id,
            ClubFee.club_id == club.id
        ).first()
        
        if not fee:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="회비 항목을 찾을 수 없습니다."
            )
        
        # 회비 항목 삭제
        db.delete(fee)
        db.commit()
        
        return {
            "message": "회비 항목이 삭제되었습니다.",
            "success": True
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버 내부 오류가 발생했습니다: {str(e)}"
        )
