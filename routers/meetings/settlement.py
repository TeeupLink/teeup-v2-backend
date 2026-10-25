"""
모임 정산 관리 API
- 라운딩/소셜 모임 정산
- 비용 항목 관리
- 정산 대상자 관리
"""

from fastapi import APIRouter, Depends, HTTPException, status, Body
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, and_, or_
from datetime import datetime
from decimal import Decimal
import logging

from database import get_db
from models import (
    User, Club, ClubMembership, Meeting, MeetingParticipant,
    Team, TeamMember, Expense, ExpenseItem, ExpenseItemParticipant, Guest
)
from models.enums import ExpenseItemType
from schemas import (
    MeetingType, MeetingSubtype, SettlementMethod,
    MeetingStatus, MeetingParticipantStatus, MeetingParticipantRole,
    ClubRole
)
from schemas import (
    RoundingMeetingCreate, SocialMeetingCreate, MeetingUpdate, 
    MeetingResponse, MeetingParticipantResponse, PaginatedResponse
)
from routers.auth import get_current_active_user, get_current_user, get_current_user_allow_both
from services.push_delivery_service import send_push_to_user
from utils.permissions import MEMBERSHIP_ACTIVE_STATUSES
from utils.amount_split import split_amount_10won, allocate_by_total, allocate_items_to_exact_totals
from models import Notification
from schemas import NotificationType, NotificationStatus
from utils.cuid import generate_cuid
from utils.club_flags import club_settlement_enabled

router = APIRouter(prefix="/meetings", tags=["meeting-settlement"])
logger = logging.getLogger(__name__)


def assert_club_settlement_api_allowed(meeting: Meeting, db: Session) -> None:
    """클럽에서 정산 기능을 끈 경우 정산 API 전부 403."""
    club = db.query(Club).filter(Club.id == meeting.club_id).first()
    if club is not None and not club_settlement_enabled(club):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="클럽에서는 정산 기능이 비활성화되어 있습니다.",
        )


def _resolve_extra_payer_index(effective_ids: list, extra_payer_id) -> int | None:
    """나머지 10원을 부담할 참가자의 effective_ids 내 인덱스 반환. 없으면 None."""
    if extra_payer_id is None:
        return None
    try:
        pid = int(extra_payer_id)
        for i, eid in enumerate(effective_ids):
            if eid is not None and int(eid) == pid:
                return i
    except (TypeError, ValueError):
        pass
    return None

# =============================================================================
# 권한 체크 유틸리티 함수
# =============================================================================

def send_settlement_created_notification(meeting_id: int, db: Session, is_edit: bool = False):
    """정산 생성/수정 완료 시 참가자들에게 알림 전송"""
    try:
        # 모임 정보 조회
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            logger.error(f"모임을 찾을 수 없습니다: {meeting_id}")
            return
        
        # 클럽 정보 조회
        club = db.query(Club).filter(Club.id == meeting.club_id).first()
        club_name = club.name if club else "알 수 없는 클럽"
        
        # 참가자들 조회
        participants = db.query(MeetingParticipant).filter(
            MeetingParticipant.meeting_id == meeting_id,
            MeetingParticipant.user_id.isnot(None)
        ).all()
        
        if not participants:
            logger.info(f"알림을 받을 참가자가 없습니다: {meeting_id}")
            return
        
        # 알림 메시지 생성
        action = "수정" if is_edit else "생성"
        title = f"정산 {action} 완료 - {meeting.name}"
        content = f"""
{club_name}의 모임 '{meeting.name}' 정산이 {action}되었습니다.

📅 모임 일시: {meeting.meeting_time.strftime('%Y년 %m월 %d일 %H:%M') if meeting.meeting_time else '미정'}
📍 장소: {meeting.location or meeting.venue_name or '미정'}

정산 내역을 확인해보세요!
        """.strip()
        
        # 각 참가자에게 알림 전송 (게스트는 알림을 받을 수 없으므로 제외)
        sent_count = 0
        for participant in participants:
            # 게스트는 알림을 받을 수 없음
            if participant.guest_id or not participant.user_id:
                continue
                
            try:
                notification = Notification(
                    user_id=participant.user_id,
                    type=NotificationType.MEETING_SETTLEMENT_COMPLETED.value,
                    title=title,
                    content=content,
                    status=NotificationStatus.UNREAD.value
                )
                
                db.add(notification)
                send_push_to_user(db=db,
                                  user_id=participant.user_id,
                                  title=notification.title,
                                  content=notification.content,
                                  category="meeting",
                                  target_id=meeting_id,
                                  extra_data={"meeting_type": meeting.meeting_type.value.lower()})
                sent_count += 1
                
            except Exception as e:
                logger.error(f"정산 {action} 알림 생성 실패 - user_id: {participant.user_id}, error: {str(e)}")
                continue
        
        db.commit()
        logger.info(f"정산 {action} 알림 전송 완료 - meeting_id: {meeting_id}, sent_count: {sent_count}")
        
    except Exception as e:
        logger.error(f"정산 {action} 알림 전송 오류: {str(e)}")
        db.rollback()

def can_manage_settlement(meeting_id: int, user_id: int, db: Session) -> bool:
    """
    정산 관리 권한 체크
    
    권한이 있는 경우:
    1. 주최자: meeting.created_by 사용자
    2. 클럽 리더/매니저: club_role ∈ {LEADER, MANAGER}
    
    Args:
        meeting_id: 모임 ID
        user_id: 사용자 ID
        db: 데이터베이스 세션
    
    Returns:
        bool: 정산 관리 권한이 있으면 True
    """
    try:
        # 모임 조회
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            return False
        
        # 모임 생성자인 경우 권한 부여
        if meeting.created_by == user_id:
            return True
        
        # 클럽 리더/매니저 권한 확인
        manager_membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == meeting.club_id,
            ClubMembership.user_id == user_id,
            ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES),
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER]),
        ).first()

        return manager_membership is not None
        
    except Exception as e:
        logger.error(f"정산 권한 체크 중 오류: {str(e)}")
        return False

# =============================================================================
# 라운딩 정산 API
# =============================================================================

@router.post("/{meeting_id}/settlement/rounding")
async def create_rounding_settlement(
    meeting_id: int,
    settlement_data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """라운딩 정산 생성 (매니저/리더만 가능)"""
    try:
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="모임을 찾을 수 없습니다."
            )
        
        if meeting.meeting_type != MeetingType.ROUND:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="라운딩 모임이 아닙니다."
            )

        assert_club_settlement_api_allowed(meeting, db)
        
        # 정산이 이미 확정된 경우 생성/수정 불가
        if meeting.settlement_confirmed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="정산이 이미 확정되어 수정할 수 없습니다."
            )
        
        # 정산 권한 확인: 주최자 또는 클럽 리더/매니저
        if not can_manage_settlement(meeting_id, current_user.id, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="모임 개설자 또는 클럽 리더/매니저만 정산을 생성할 수 있습니다."
            )
        
        # 정산 데이터 추출
        total_cost = Decimal(str(settlement_data.get('total_cost', 0)))
        green_fee = Decimal(str(settlement_data.get('green_fee', 0)))
        caddy_fee = Decimal(str(settlement_data.get('caddy_fee', 0)))
        cart_fee = Decimal(str(settlement_data.get('cart_fee', 0)))
        other_fee = Decimal(str(settlement_data.get('other_fee', 0)))
        notes = settlement_data.get('notes', '')
        green_fee_participants = settlement_data.get('green_fee_participants', [])
        green_fee_exempted = settlement_data.get('green_fee_exempted', [])
        green_fee_extra_payer_id = settlement_data.get('green_fee_extra_payer_id')
        cart_fee_participants = settlement_data.get('cart_fee_participants', [])
        cart_fee_exempted = settlement_data.get('cart_fee_exempted', [])
        cart_fee_extra_payer_id = settlement_data.get('cart_fee_extra_payer_id')
        caddy_fee_participants = settlement_data.get('caddy_fee_participants', [])
        caddy_fee_exempted = settlement_data.get('caddy_fee_exempted', [])
        caddy_fee_extra_payer_id = settlement_data.get('caddy_fee_extra_payer_id')
        other_expense_items = settlement_data.get('other_expense_items', [])
        exclude_remaining_amount = settlement_data.get('exclude_remaining_amount', False)
        all_covered_by_fee = settlement_data.get('all_covered_by_fee', False)
        # 정산 방법: settlement_data 우선, 없으면 meeting.settlement_method (n분의1 vs 개별정산)
        # INDIVIDUAL이면 항상 개별정산, EQUAL_SPLIT 또는 미지정이면 같은 참가자일 때 n분의1
        settlement_method_val = settlement_data.get('settlement_method')
        if settlement_method_val is None and meeting.settlement_method is not None:
            settlement_method_val = meeting.settlement_method.value if hasattr(meeting.settlement_method, 'value') else str(meeting.settlement_method)
        use_equal_split = (settlement_method_val != 'INDIVIDUAL')
        green_fee_covered_by_fee = settlement_data.get('green_fee_covered_by_fee', False)
        caddy_fee_covered_by_fee = settlement_data.get('caddy_fee_covered_by_fee', False)
        cart_fee_covered_by_fee = settlement_data.get('cart_fee_covered_by_fee', False)

        all_settlement_targets = set()
        if not all_covered_by_fee and not green_fee_covered_by_fee:
            all_settlement_targets.update(green_fee_participants)
        if not all_covered_by_fee and not cart_fee_covered_by_fee:
            all_settlement_targets.update(cart_fee_participants)
        if not all_covered_by_fee and not caddy_fee_covered_by_fee:
            all_settlement_targets.update(caddy_fee_participants)
        if not all_covered_by_fee:
            for item in other_expense_items:
                if bool(item.get("covered_by_fee")):
                    continue
                for pid in (item.get('participants') or []):
                    if pid and pid != 'UNSETTLED':
                        all_settlement_targets.add(pid)

        settlement_targets = list(all_settlement_targets)
        target_count = len(settlement_targets)
        if target_count == 0 and not exclude_remaining_amount and not all_covered_by_fee:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="정산 대상자를 선택해주세요.")
        if not all_covered_by_fee and not green_fee_covered_by_fee and len(green_fee_participants) == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="그린피의 정산 대상자를 선택해주세요.")
        if not all_covered_by_fee and not cart_fee_covered_by_fee and len(cart_fee_participants) == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="카트비의 정산 대상자를 선택해주세요.")
        if not all_covered_by_fee and not caddy_fee_covered_by_fee and len(caddy_fee_participants) == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="캐디피의 정산 대상자를 선택해주세요.")
        if not all_covered_by_fee:
            for idx, item in enumerate(other_expense_items):
                if bool(item.get("covered_by_fee")):
                    continue
                if not (item.get('participants') or []):
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"기타 비용 항목 {idx + 1}의 정산 대상자를 선택해주세요.")

        amount_per_person = Decimal('0') if all_covered_by_fee else (total_cost / target_count if target_count > 0 else Decimal('0'))

        # 총액 기준 1인당 부담금 (딱 떨어지면 전원 동일, 아니면 10원 단위 배분)
        _cost_to_split = total_cost if not all_covered_by_fee and target_count > 0 else Decimal('0')
        _extra_idx = _resolve_extra_payer_index(settlement_targets, settlement_data.get('extra_payer_id'))
        _total_per_person = split_amount_10won(
            _cost_to_split, target_count,
            extra_recipient_indices=[_extra_idx] if _extra_idx is not None else None
        ) if target_count > 0 else []

        def _add_item_participants(expense_item, participant_ids, exempted_ids, amount_list):
            """amount_list: 10원 단위 나머지 배분법으로 계산된 인원별 금액 (비면제 참가자 순)"""
            exempted_set = set(exempted_ids or [])
            amount_idx = 0
            for pid in participant_ids:
                if pid in exempted_set:
                    continue
                pid_int = int(pid) if pid is not None else None
                if pid_int is None or amount_idx >= len(amount_list):
                    continue
                mp = db.query(MeetingParticipant).filter(
                    MeetingParticipant.meeting_id == meeting_id
                ).filter(
                    or_(
                        MeetingParticipant.id == pid_int,
                        MeetingParticipant.user_id == pid_int,
                        MeetingParticipant.guest_id == pid_int
                    )
                ).first()
                if mp:
                    eip = ExpenseItemParticipant(
                        expense_item_id=expense_item.id,
                        user_id=mp.user_id,
                        guest_id=mp.guest_id,
                        is_exempted=False,
                        amount=amount_list[amount_idx]
                    )
                    db.add(eip)
                    amount_idx += 1

        expense = Expense(
            title=f"{meeting.name} 라운딩 정산",
            description=f"그린피: {green_fee:,}원, 캐디피: {caddy_fee:,}원, 카트비: {cart_fee:,}원, 기타: {other_fee:,}원",
            meeting_id=meeting_id,
            club_id=meeting.club_id,
            created_by=current_user.id,
            notes=notes,
            exclude_remaining_amount=exclude_remaining_amount,
        )
        db.add(expense)
        db.flush()

        # n분의1 + 같은 참가자: 항목별 배분 후 인당 합계가 total_per_person과 정확히 일치하도록
        def _eff(pids, exempted):
            return [p for p in (pids or []) if p not in set(exempted or [])]
        _g_eff = _eff(green_fee_participants, green_fee_exempted)
        _c_eff = _eff(caddy_fee_participants, caddy_fee_exempted)
        _k_eff = _eff(cart_fee_participants, cart_fee_exempted)
        _same_targets = (
            set(_g_eff) == set(settlement_targets) and
            set(_c_eff) == set(settlement_targets) and
            set(_k_eff) == set(settlement_targets)
        )
        _item_amounts = []
        if not green_fee_covered_by_fee and green_fee > 0:
            _item_amounts.append(green_fee)
        if not caddy_fee_covered_by_fee and caddy_fee > 0:
            _item_amounts.append(caddy_fee)
        if not cart_fee_covered_by_fee and cart_fee > 0:
            _item_amounts.append(cart_fee)
        for oi in (other_expense_items or []):
            if bool(oi.get("covered_by_fee")):
                continue
            am = Decimal(str(oi.get('amount', 0)))
            if am > 0:
                _item_amounts.append(am)
        _exact_allocations = []
        if use_equal_split and _total_per_person and _cost_to_split > 0 and _item_amounts and _same_targets:
            _exact_allocations = allocate_items_to_exact_totals(_total_per_person, _item_amounts, _cost_to_split)
        _alloc_idx = 0

        order_idx = 0
        if green_fee > 0:
            gf_item = ExpenseItem(
                expense_id=expense.id,
                type=ExpenseItemType.GREEN_FEE,
                amount=green_fee,
                covered_by_fee=green_fee_covered_by_fee,
                order_index=order_idx,
            )
            db.add(gf_item)
            db.flush()
            if not green_fee_covered_by_fee and green_fee_participants:
                exempted = set(green_fee_exempted or [])
                effective_ids = [p for p in green_fee_participants if p not in exempted]
                if _exact_allocations and not green_fee_covered_by_fee and green_fee > 0:
                    alloc_row = _exact_allocations[_alloc_idx]
                    amount_list = [alloc_row[settlement_targets.index(pid)] for pid in effective_ids]
                    _alloc_idx += 1
                elif use_equal_split and effective_ids and _total_per_person and set(effective_ids) == set(settlement_targets) and _cost_to_split > 0:
                    allocated = allocate_by_total(green_fee, _cost_to_split, _total_per_person)
                    amount_list = [allocated[settlement_targets.index(pid)] for pid in effective_ids]
                else:
                    extra_idx = _resolve_extra_payer_index(effective_ids, green_fee_extra_payer_id)
                    amount_list = split_amount_10won(
                        green_fee, len(effective_ids),
                        extra_recipient_indices=[extra_idx] if extra_idx is not None else None
                    ) if effective_ids else []
                _add_item_participants(gf_item, green_fee_participants, green_fee_exempted, amount_list)
            order_idx += 1

        if caddy_fee > 0:
            cf_item = ExpenseItem(
                expense_id=expense.id,
                type=ExpenseItemType.CADDY_FEE,
                amount=caddy_fee,
                covered_by_fee=caddy_fee_covered_by_fee,
                order_index=order_idx,
            )
            db.add(cf_item)
            db.flush()
            if not caddy_fee_covered_by_fee and caddy_fee_participants:
                exempted = set(caddy_fee_exempted or [])
                effective_ids = [p for p in caddy_fee_participants if p not in exempted]
                if _exact_allocations and not caddy_fee_covered_by_fee and caddy_fee > 0:
                    alloc_row = _exact_allocations[_alloc_idx]
                    amount_list = [alloc_row[settlement_targets.index(pid)] for pid in effective_ids]
                    _alloc_idx += 1
                elif use_equal_split and effective_ids and _total_per_person and set(effective_ids) == set(settlement_targets) and _cost_to_split > 0:
                    allocated = allocate_by_total(caddy_fee, _cost_to_split, _total_per_person)
                    amount_list = [allocated[settlement_targets.index(pid)] for pid in effective_ids]
                else:
                    extra_idx = _resolve_extra_payer_index(effective_ids, caddy_fee_extra_payer_id)
                    amount_list = split_amount_10won(
                        caddy_fee, len(effective_ids),
                        extra_recipient_indices=[extra_idx] if extra_idx is not None else None
                    ) if effective_ids else []
                _add_item_participants(cf_item, caddy_fee_participants, caddy_fee_exempted, amount_list)
            order_idx += 1

        if cart_fee > 0:
            crf_item = ExpenseItem(
                expense_id=expense.id,
                type=ExpenseItemType.CART_FEE,
                amount=cart_fee,
                covered_by_fee=cart_fee_covered_by_fee,
                order_index=order_idx,
            )
            db.add(crf_item)
            db.flush()
            if not cart_fee_covered_by_fee and cart_fee_participants:
                exempted = set(cart_fee_exempted or [])
                effective_ids = [p for p in cart_fee_participants if p not in exempted]
                if _exact_allocations and not cart_fee_covered_by_fee and cart_fee > 0:
                    alloc_row = _exact_allocations[_alloc_idx]
                    amount_list = [alloc_row[settlement_targets.index(pid)] for pid in effective_ids]
                    _alloc_idx += 1
                elif use_equal_split and effective_ids and _total_per_person and set(effective_ids) == set(settlement_targets) and _cost_to_split > 0:
                    allocated = allocate_by_total(cart_fee, _cost_to_split, _total_per_person)
                    amount_list = [allocated[settlement_targets.index(pid)] for pid in effective_ids]
                else:
                    extra_idx = _resolve_extra_payer_index(effective_ids, cart_fee_extra_payer_id)
                    amount_list = split_amount_10won(
                        cart_fee, len(effective_ids),
                        extra_recipient_indices=[extra_idx] if extra_idx is not None else None
                    ) if effective_ids else []
                _add_item_participants(crf_item, cart_fee_participants, cart_fee_exempted, amount_list)
            order_idx += 1

        for oi in (other_expense_items or []):
            amt = Decimal(str(oi.get('amount', 0)))
            if amt <= 0:
                continue
            participants = oi.get('participants') or []
            title = oi.get('title') or oi.get('name') or '기타 비용'
            om = oi.get("memo")
            om_str = (str(om).strip() if om is not None else "") or None
            other_covered = bool(oi.get("covered_by_fee"))
            other_item = ExpenseItem(
                expense_id=expense.id,
                type=ExpenseItemType.OTHER,
                title=title,
                memo=om_str,
                amount=amt,
                covered_by_fee=other_covered,
                order_index=order_idx,
            )
            db.add(other_item)
            db.flush()
            if participants and not other_covered:
                if _exact_allocations and _alloc_idx < len(_exact_allocations) and set(participants) == set(settlement_targets):
                    alloc_row = _exact_allocations[_alloc_idx]
                    amount_list = [alloc_row[settlement_targets.index(pid)] for pid in participants]
                    _alloc_idx += 1
                else:
                    extra_payer_id = oi.get('extra_payer_id')
                    extra_idx = _resolve_extra_payer_index(participants, extra_payer_id)
                    amount_list = split_amount_10won(
                        amt, len(participants),
                        extra_recipient_indices=[extra_idx] if extra_idx is not None else None
                    )
                _add_item_participants(other_item, participants, [], amount_list)
            order_idx += 1

        db.commit()
        logger.info(f"라운딩 정산 생성 완료 - expense.id: {expense.id}")
        
        # 정산 생성/수정 완료 알림 전송
        try:
            send_settlement_created_notification(meeting_id, db, is_edit=False)
        except Exception as e:
            logger.error(f"정산 생성 알림 전송 실패: {str(e)}")
        
        return {
            "message": "라운딩 정산이 생성되었습니다.",
            "settlement_id": expense.id,
            "total_cost": float(total_cost),
            "amount_per_person": float(amount_per_person),
            "target_count": target_count
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"라운딩 정산 생성 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )


@router.put("/{meeting_id}/settlement/rounding")
async def update_rounding_settlement(
    meeting_id: int,
    settlement_data: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """라운딩 정산 수정 (기존 정산 삭제 후 재생성)"""
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")
        if meeting.meeting_type != MeetingType.ROUND:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라운딩 모임이 아닙니다.")
        assert_club_settlement_api_allowed(meeting, db)
        if meeting.settlement_confirmed:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="정산이 이미 확정되어 수정할 수 없습니다.")
        if not can_manage_settlement(meeting_id, current_user.id, db):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="정산 수정 권한이 없습니다.")

        existing = db.query(Expense).filter(Expense.meeting_id == meeting_id).first()
        if existing:
            db.delete(existing)
            db.flush()

        return await create_rounding_settlement(meeting_id, settlement_data, db, current_user)
    except HTTPException:
        raise


# =============================================================================
# 소셜 모임 정산 API
# =============================================================================

@router.post("/{meeting_id}/settlement/social")
async def create_social_settlement(
    meeting_id: int,
    settlement_data: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """소셜 모임 정산 생성 (매니저/리더만 가능)"""
    try:
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="모임을 찾을 수 없습니다."
            )
        
        if meeting.meeting_type != MeetingType.SOCIAL:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="소셜 모임이 아닙니다."
            )

        assert_club_settlement_api_allowed(meeting, db)
        
        # 정산이 이미 확정된 경우 생성/수정 불가
        if meeting.settlement_confirmed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="정산이 이미 확정되어 수정할 수 없습니다."
            )
        
        # 정산 권한 확인: 주최자 또는 클럽 리더/매니저
        if not can_manage_settlement(meeting_id, current_user.id, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="모임 개설자 또는 클럽 리더/매니저만 정산을 생성할 수 있습니다."
            )
        
        expense_items_data = settlement_data.get('expense_items', [])
        settlement_targets = settlement_data.get('settlement_targets', [])
        notes = settlement_data.get('notes', '')
        exclude_remaining_amount = settlement_data.get('exclude_remaining_amount', False)
        # 항목별 나머지 부담자 (미지정 시 앞에서부터)
        default_extra_payer_id = settlement_data.get('extra_payer_id')

        total_cost = settlement_data.get('total_cost')
        if total_cost is None:
            total_cost = sum(Decimal(str(item.get('amount', 0))) for item in expense_items_data)
        else:
            total_cost = Decimal(str(total_cost))

        # settlement_targets 미지정이면 "전체 인원 N분의1"로 자동 처리
        # - 대상자 식별은 MeetingParticipant.id 사용 (user_id/guest_id 충돌 방지)
        target_count = len(settlement_targets or [])
        if target_count == 0:
            from models.enums import ParticipantStatus
            all_participants = db.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == meeting_id,
                MeetingParticipant.status != ParticipantStatus.CANCELED,
            ).all()
            settlement_targets = [p.id for p in all_participants if p and p.id is not None]
            target_count = len(settlement_targets)

        if target_count == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="정산 대상자가 없습니다.")

        # 나머지 10원 단위는 기본적으로 개설자(없으면 첫 번째 대상자)가 부담
        organizer_pid = None
        try:
            organizer = db.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == meeting_id,
                MeetingParticipant.user_id == meeting.created_by,
            ).first()
            organizer_pid = organizer.id if organizer else None
        except Exception:
            organizer_pid = None
        default_extra_payer_id = default_extra_payer_id or organizer_pid or (settlement_targets[0] if settlement_targets else None)

        cost_to_split = sum(
            Decimal(str(x.get("amount", 0)))
            for x in (expense_items_data or [])
            if Decimal(str(x.get("amount", 0))) > 0 and not bool(x.get("covered_by_fee"))
        )
        amount_per_person = (
            Decimal("0")
            if exclude_remaining_amount or target_count == 0
            else cost_to_split / target_count
        )

        def _add_social_participants(expense_item, participant_ids, amount_list):
            """amount_list: 10원 단위 나머지 배분법으로 계산된 인원별 금액"""
            for idx, pid in enumerate(participant_ids):
                if idx >= len(amount_list):
                    break
                pid_int = int(pid) if pid is not None else None
                if pid_int is None:
                    continue
                mp = db.query(MeetingParticipant).filter(
                    MeetingParticipant.meeting_id == meeting_id
                ).filter(
                    or_(
                        MeetingParticipant.id == pid_int,
                        MeetingParticipant.user_id == pid_int,
                        MeetingParticipant.guest_id == pid_int
                    )
                ).first()
                if mp:
                    eip = ExpenseItemParticipant(
                        expense_item_id=expense_item.id,
                        user_id=mp.user_id,
                        guest_id=mp.guest_id,
                        is_exempted=False,
                        amount=amount_list[idx]
                    )
                    db.add(eip)

        expense = Expense(
            title=f"{meeting.name} 소셜 모임 정산",
            description=f"총 {len(expense_items_data)}개 항목",
            meeting_id=meeting_id,
            club_id=meeting.club_id,
            created_by=current_user.id,
            notes=notes,
            exclude_remaining_amount=exclude_remaining_amount,
        )
        db.add(expense)
        db.flush()

        # 각 정산 항목 생성, 금액 합산 후 n명 균등 분배 (회비 처리 항목은 참가자 분담 라인 없음)
        for idx, item in enumerate(expense_items_data or []):
            amt = Decimal(str(item.get('amount', 0)))
            if amt <= 0:
                continue
            title = item.get('title') or item.get('name') or f"항목 {idx + 1}"
            memo_val = item.get("memo")
            memo_str = (str(memo_val).strip() if memo_val is not None else "") or None
            item_covered = bool(item.get("covered_by_fee"))
            ei = ExpenseItem(
                expense_id=expense.id,
                type=ExpenseItemType.SOCIAL_ITEM,
                title=title,
                memo=memo_str,
                amount=amt,
                covered_by_fee=item_covered,
                order_index=idx,
            )
            db.add(ei)
            db.flush()
            if (
                not item_covered
                and not exclude_remaining_amount
                and settlement_targets
                and target_count > 0
            ):
                extra_payer_id = item.get('extra_payer_id', default_extra_payer_id)
                extra_idx = _resolve_extra_payer_index(settlement_targets, extra_payer_id)
                if extra_idx is None:
                    extra_idx = 0
                amount_list = split_amount_10won(
                    amt, target_count,
                    extra_recipient_indices=[extra_idx] if extra_idx is not None else None
                )
                _add_social_participants(ei, settlement_targets, amount_list)

        db.commit()
        logger.info(f"소셜 정산 생성 완료 - expense.id: {expense.id}")
        
        # 정산 생성/수정 완료 알림 전송
        try:
            send_settlement_created_notification(meeting_id, db, is_edit=False)
        except Exception as e:
            logger.error(f"정산 생성 알림 전송 실패: {str(e)}")
        
        return {
            "message": "소셜 모임 정산이 생성되었습니다.",
            "settlement_id": expense.id,
            "total_cost": float(total_cost),
            "amount_per_person": float(amount_per_person),
            "target_count": target_count
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"소셜 모임 정산 생성 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )


@router.put("/{meeting_id}/settlement/social")
async def update_social_settlement(
    meeting_id: int,
    settlement_data: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """소셜 모임 정산 수정 (기존 정산 삭제 후 재생성)"""
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")
        if meeting.meeting_type != MeetingType.SOCIAL:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="소셜 모임이 아닙니다.")
        assert_club_settlement_api_allowed(meeting, db)
        if meeting.settlement_confirmed:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="정산이 이미 확정되어 수정할 수 없습니다.")
        if not can_manage_settlement(meeting_id, current_user.id, db):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="정산 수정 권한이 없습니다.")

        existing = db.query(Expense).filter(Expense.meeting_id == meeting_id).first()
        if existing:
            db.delete(existing)
            db.flush()

        return await create_social_settlement(meeting_id, settlement_data, db, current_user)
    except HTTPException:
        raise


# =============================================================================
# 정산 조회 API
# =============================================================================


def _build_settlement_response(meeting_id: int, db: Session) -> dict:
    """정산 데이터 조회 (권한 체크 없음) - admin 등에서 재사용"""
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")
    expense = db.query(Expense).options(joinedload(Expense.items)).filter(Expense.meeting_id == meeting_id).first()
    if not expense:
        return {"settlement": None}

    total_cost = sum(float(item.amount or 0) for item in expense.items)
    all_eips = []
    for item in expense.items:
        eips = db.query(ExpenseItemParticipant).options(
            joinedload(ExpenseItemParticipant.user), joinedload(ExpenseItemParticipant.guest)
        ).filter(ExpenseItemParticipant.expense_item_id == item.id).all()
        all_eips.extend(eips)

    # 참가자별 부담금·납부액 집계 (같은 user_id/guest_id가 여러 expense_item에 걸쳐 있을 수 있음)
    agg = {}
    for ep in all_eips:
        key = (ep.user_id, ep.guest_id)
        if key not in agg:
            agg[key] = {"amount": Decimal("0"), "amount_paid": Decimal("0"), "is_paid": True, "paid_at": None, "ep": ep}
        agg[key]["amount"] += Decimal(str(ep.amount or 0))
        agg[key]["amount_paid"] += Decimal(str(ep.amount_paid or 0))
        if not (ep.is_paid or False):
            agg[key]["is_paid"] = False
        if ep.paid_at and (agg[key]["paid_at"] is None or ep.paid_at > agg[key]["paid_at"]):
            agg[key]["paid_at"] = ep.paid_at

    participant_list = []
    settlement_targets = []
    for key, v in agg.items():
        ep = v["ep"]
        amt = float(v["amount"])
        amt_paid = float(v["amount_paid"])
        # 납부완료는 실제 납부액이 부담금 이상일 때만 (DB is_paid와 무관하게 금액 기준)
        effective_is_paid = (amt <= 0) or (amt_paid >= amt)
        if ep.guest_id:
            g = ep.guest
            participant_list.append({
                "id": ep.id, "user_id": None, "guest_id": ep.guest_id,
                "user_name": g.name if g else "게스트", "user_email": None, "is_guest": True,
                "amount": amt, "amount_paid": amt_paid,
                "is_paid": effective_is_paid, "paid_at": v["paid_at"].isoformat() if v["paid_at"] and effective_is_paid else None,
                "is_settlement_target": True,
            })
            settlement_targets.append(ep.guest_id)
        else:
            u = ep.user
            participant_list.append({
                "id": ep.id, "user_id": u.id if u else None, "guest_id": None,
                "user_name": u.nickname if u else "알 수 없음", "user_email": u.email if u else None, "is_guest": False,
                "amount": amt, "amount_paid": amt_paid,
                "is_paid": effective_is_paid, "paid_at": v["paid_at"].isoformat() if v["paid_at"] and effective_is_paid else None,
                "is_settlement_target": True,
            })
            if u:
                settlement_targets.append(u.id)

    amount_per_person = total_cost / len(settlement_targets) if settlement_targets else 0
    settlement_data = {
        "id": expense.id, "title": expense.title, "description": expense.description,
        "total_cost": total_cost, "amount_per_person": amount_per_person,
        "total_participants": len(settlement_targets), "notes": expense.notes,
        "created_at": expense.created_at, "participants": participant_list, "settlement_targets": settlement_targets,
    }
    meeting_type_val = meeting.meeting_type.value if hasattr(meeting.meeting_type, "value") else str(meeting.meeting_type)

    if meeting_type_val == "SOCIAL":
        # 항목별 회비 처리(covered_by_fee)는 ExpenseItemParticipant가 없으므로, 인당 부담은 실제 분담 합계 기준
        if settlement_targets:
            split_total = sum(float(v["amount"]) for v in agg.values())
            settlement_data["amount_per_person"] = split_total / len(settlement_targets)
        else:
            settlement_data["amount_per_person"] = 0
        settlement_data["exclude_remaining_amount"] = bool(expense.exclude_remaining_amount)
        settlement_data["extra_payer_id"] = None
        expense_items_api = []
        for item in sorted(expense.items, key=lambda x: x.order_index):
            pids = []
            for eip in db.query(ExpenseItemParticipant).filter(ExpenseItemParticipant.expense_item_id == item.id).all():
                if eip.user_id:
                    pids.append(eip.user_id)
                elif eip.guest_id:
                    pids.append(eip.guest_id)
            expense_items_api.append({
                "id": item.id, "title": item.title or "항목", "amount": float(item.amount or 0),
                "memo": (getattr(item, "memo", None) or "").strip() or None,
                "covered_by_fee": bool(getattr(item, "covered_by_fee", False)),
                "participants": pids,
            })
        settlement_data["expense_items"] = expense_items_api

    if meeting_type_val == "ROUND":
        green_fee = caddy_fee = cart_fee = other_fee = Decimal("0")
        green_fee_participants = green_fee_exempted = []
        caddy_fee_participants = caddy_fee_exempted = []
        cart_fee_participants = cart_fee_exempted = []
        other_expense_items = []
        green_covered = caddy_covered = cart_covered = False
        for item in expense.items:
            amt = float(item.amount or 0)
            pids = []
            for eip in db.query(ExpenseItemParticipant).filter(ExpenseItemParticipant.expense_item_id == item.id).all():
                if not eip.is_exempted:
                    if eip.user_id:
                        pids.append(eip.user_id)
                    elif eip.guest_id:
                        pids.append(eip.guest_id)
            t = item.type.value if hasattr(item.type, "value") else str(item.type)
            if t == "GREEN_FEE":
                green_fee = amt
                green_fee_participants = pids
                green_covered = bool(item.covered_by_fee)
            elif t == "CADDY_FEE":
                caddy_fee = amt
                caddy_fee_participants = pids
                caddy_covered = bool(item.covered_by_fee)
            elif t == "CART_FEE":
                cart_fee = amt
                cart_fee_participants = pids
                cart_covered = bool(item.covered_by_fee)
            elif t == "OTHER":
                other_expense_items.append({
                    "title": item.title or "기타",
                    "amount": amt,
                    "memo": (getattr(item, "memo", None) or "").strip() or None,
                    "covered_by_fee": bool(getattr(item, "covered_by_fee", False)),
                    "participants": pids,
                })
                other_fee += Decimal(str(amt))
        settlement_data["green_fee"] = green_fee
        settlement_data["caddy_fee"] = caddy_fee
        settlement_data["cart_fee"] = cart_fee
        settlement_data["other_fee"] = other_fee
        settlement_data["green_fee_participants"] = green_fee_participants
        settlement_data["green_fee_exempted"] = green_fee_exempted
        settlement_data["caddy_fee_participants"] = caddy_fee_participants
        settlement_data["caddy_fee_exempted"] = caddy_fee_exempted
        settlement_data["cart_fee_participants"] = cart_fee_participants
        settlement_data["cart_fee_exempted"] = cart_fee_exempted
        settlement_data["other_expense_items"] = other_expense_items
        settlement_data["total_cost_participants"] = settlement_targets
        settlement_data["total_cost_exempted"] = []
        settlement_data["exempted_participants"] = []
        settlement_data["all_covered_by_fee"] = False
        settlement_data["green_fee_covered_by_fee"] = green_covered
        settlement_data["caddy_fee_covered_by_fee"] = caddy_covered
        settlement_data["cart_fee_covered_by_fee"] = cart_covered
        # 정산 방법 (n분의1 vs 개별정산) - 수정 폼에서 모드 표시용
        sm = meeting.settlement_method
        settlement_data["settlement_method"] = sm.value if sm and hasattr(sm, 'value') else (str(sm) if sm else None)
        # 나머지 10원 부담자: 금액이 다른 참가자 중 가장 많이 부담한 사람 추정 (수정 폼 기본값용)
        amt_to_id = []
        for _, v in agg.items():
            ep = v["ep"]
            pid = ep.user_id if ep.user_id is not None else ep.guest_id
            if pid is not None:
                amt_to_id.append((float(v["amount"]), pid))
        if len(amt_to_id) > 1 and len(set(a for a, _ in amt_to_id)) > 1:
            inferred_extra = max(amt_to_id, key=lambda x: (float(x[0]), 0))[1]
            settlement_data["extra_payer_id"] = inferred_extra
        else:
            settlement_data["extra_payer_id"] = None
    return {"settlement": settlement_data}


@router.get("/{meeting_id}/settlement")
async def get_meeting_settlement(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_allow_both)
):
    """모임 정산 조회"""
    try:
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="모임을 찾을 수 없습니다."
            )

        assert_club_settlement_api_allowed(meeting, db)
        
        # 프라이빗 라운딩인 경우 권한 체크
        if meeting.is_private:
            # 관리자는 접근 가능
            from models import Admin
            admin = db.query(Admin).filter(
                Admin.id == current_user.id,
                Admin.deleted_at.is_(None)
            ).first()
            
            if not admin:
                # 참가자 또는 생성자인지 확인
                is_participant = db.query(MeetingParticipant).filter(
                    MeetingParticipant.meeting_id == meeting_id,
                    MeetingParticipant.user_id == current_user.id
                ).first()
                
                is_creator = meeting.created_by == current_user.id
                is_manager_or_leader = db.query(ClubMembership).filter(
                    ClubMembership.club_id == meeting.club_id,
                    ClubMembership.user_id == current_user.id,
                    ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES),
                    ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER]),
                ).first()
                
                if not is_participant and not is_creator and not is_manager_or_leader:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="프라이빗 라운딩의 정산 정보는 참가자/생성자/클럽 리더·매니저만 조회할 수 있습니다."
                    )
        else:
            # 일반 라운딩: 관리자가 아닌 경우에만 클럽 멤버십 확인
            from models import Admin
            admin = db.query(Admin).filter(
                Admin.id == current_user.id,
                Admin.deleted_at.is_(None)
            ).first()
            
            if not admin:
                membership = db.query(ClubMembership).filter(
                    ClubMembership.club_id == meeting.club_id,
                    ClubMembership.user_id == current_user.id,
                    ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES)
                ).first()
                
                if not membership:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="클럽 멤버만 정산을 조회할 수 있습니다."
                    )
        return _build_settlement_response(meeting_id, db)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"정산 조회 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

# =============================================================================
# 내 정산 보기 API
# =============================================================================

@router.get("/{meeting_id}/settlement/my")
async def get_my_settlement(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """현재 사용자의 정산 정보 조회 (납부해야 할 금액만)"""
    try:
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="모임을 찾을 수 없습니다."
            )

        assert_club_settlement_api_allowed(meeting, db)
        
        # 정산 존재 확인 (정산 확정 여부는 체크하지 않음)
        
        # 클럽 멤버십 확인
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == meeting.club_id,
            ClubMembership.user_id == current_user.id,
            ClubMembership.status.in_(MEMBERSHIP_ACTIVE_STATUSES)
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 멤버만 정산을 조회할 수 있습니다."
            )
        
        # 정산 조회
        expense = db.query(Expense).filter(Expense.meeting_id == meeting_id).first()
        
        if not expense:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="정산 정보를 찾을 수 없습니다."
            )
        
        user_id = current_user.id
        items = []
        total_amount_due_decimal = Decimal('0')
        amount_paid = Decimal('0')

        expense_items = db.query(ExpenseItem).filter(ExpenseItem.expense_id == expense.id).all()
        for ei in expense_items:
            eip = db.query(ExpenseItemParticipant).filter(
                ExpenseItemParticipant.expense_item_id == ei.id,
                ExpenseItemParticipant.user_id == user_id,
                ExpenseItemParticipant.is_exempted == False
            ).first()
            if not eip:
                continue
            my_amt = float(eip.amount or 0)
            total_amount_due_decimal += Decimal(str(my_amt))
            amount_paid += Decimal(str(eip.amount_paid or 0))
            cnt = db.query(ExpenseItemParticipant).filter(
                ExpenseItemParticipant.expense_item_id == ei.id,
                ExpenseItemParticipant.is_exempted == False
            ).count()
            type_map = {"GREEN_FEE": "green_fee", "CADDY_FEE": "caddy_fee", "CART_FEE": "cart_fee",
                        "OTHER": "expense_item", "SOCIAL_ITEM": "expense_item"}
            t = ei.type.value if hasattr(ei.type, "value") else str(ei.type)
            name = ei.title or ({"GREEN_FEE": "그린피", "CADDY_FEE": "캐디피", "CART_FEE": "카트비",
                                 "OTHER": "기타", "SOCIAL_ITEM": "비용 항목"}.get(t, "비용 항목"))
            items.append({
                "type": type_map.get(t, "expense_item"),
                "name": name,
                "amount": float(ei.amount or 0),
                "participants_count": cnt,
                "my_amount": my_amt
            })

        total_amount_due_float = float(total_amount_due_decimal)
        amount_paid_float = float(amount_paid)
        remaining_amount = total_amount_due_float - amount_paid_float
        
        return {
            "total_amount_due": total_amount_due_float,
            "amount_paid": amount_paid_float,
            "remaining_amount": remaining_amount,
            "is_paid": remaining_amount <= 0,
            "items": items
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"내 정산 조회 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

# =============================================================================
# 정산 대상자 관리 API
# =============================================================================

@router.get("/{meeting_id}/settlement/available-participants")
async def get_available_participants(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """정산 대상자로 선택 가능한 참가자 목록 조회"""
    try:
        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="모임을 찾을 수 없습니다."
            )

        assert_club_settlement_api_allowed(meeting, db)
        
        # 정산 권한 확인: 주최자 또는 클럽 리더/매니저
        if not can_manage_settlement(meeting_id, current_user.id, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="모임 개설자 또는 클럽 리더/매니저만 정산 대상자를 조회할 수 있습니다."
            )
        
        # 참가자 조회 (게스트 포함)
        participants = db.query(MeetingParticipant).options(
            joinedload(MeetingParticipant.user),
            joinedload(MeetingParticipant.guest)
        ).filter(
            MeetingParticipant.meeting_id == meeting_id
        ).all()
        
        participant_list = []
        for meeting_participant in participants:
            if meeting_participant.guest_id:
                # 게스트인 경우
                guest = meeting_participant.guest
                if guest:
                    participant_list.append({
                        "id": None,  # 게스트는 id가 없음
                        "name": guest.name or "게스트",
                        "email": None,
                        "joined_at": meeting_participant.created_at,
                        "is_guest": True
                    })
            else:
                # 일반 사용자인 경우
                user = meeting_participant.user
                if user:
                    participant_list.append({
                        "id": user.id,
                        "name": user.nickname or user.realname,
                        "email": user.email,
                        "joined_at": meeting_participant.created_at,
                        "is_guest": False
                    })
        
        return {"participants": participant_list}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"정산 대상자 조회 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )








