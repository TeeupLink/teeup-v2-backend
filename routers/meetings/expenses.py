# 비용 정산 API들 (모임 전용)
from fastapi import APIRouter, Depends, HTTPException, status, Body
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from decimal import Decimal
import json
import logging

logger = logging.getLogger(__name__)

from database import get_db
from models import (
    User, Club, ClubMembership, Meeting,
    Expense, ExpenseItem, ExpenseItemParticipant, Guest, MeetingParticipant
)
from schemas import ClubRole
# Event 모델은 Phase 3에서 제거됨
from schemas import (
    ExpenseCreate, ExpenseUpdate, ExpenseResponse, ExpenseListResponse,
    ExpenseParticipantResponse, ExpenseParticipantUpdate, MessageResponse
)
from routers.auth import get_current_user, get_current_active_user
from routers.meetings.settlement import can_manage_settlement
from utils.amount_split import split_amount_10won

# 엔드포인트는 접두어 없는 라우터에 정의하고, 파일 맨 아래에서 두 경로에 붙인다.
# (앱은 /rounds/{id}/expenses 를, 백오피스·웹은 /meetings/{id}/expenses 를 호출한다)
_expense_routes = APIRouter(tags=["비용 정산"])

# ===== 헬퍼 함수 =====

# get_current_active_user 는 User ORM 객체를 돌려준다 (dict 가 아니다).
# 과거에 이 파일이 current_user 를 dict 로 보고 .get() 을 호출해 전 엔드포인트가
# AttributeError -> 500 이 됐다. 다른 경로에서 dict 로 넘어오는 경우도 있어 둘 다 받는다.
def _user_id(current_user) -> Optional[int]:
    """current_user 에서 사용자 ID 를 꺼낸다 (dict / ORM 객체 모두 지원)."""
    if isinstance(current_user, dict):
        return current_user.get("id")
    return getattr(current_user, "id", None)


def _is_admin(current_user) -> bool:
    """관리자 계정인지. get_current_active_user 경로로는 관리자가 올 수 없다."""
    if isinstance(current_user, dict):
        return current_user.get("type") == "admin"
    return False


def _eip_to_response(eip: ExpenseItemParticipant, expense_id: int, db: Session) -> ExpenseParticipantResponse:
    """ExpenseItemParticipant를 ExpenseParticipantResponse로 변환"""
    created_at = getattr(eip, "created_at", None)
    if eip.guest_id:
        g = db.query(Guest).filter(Guest.id == eip.guest_id).first()
        return ExpenseParticipantResponse(
            id=eip.id, expense_id=expense_id, user_id=None, guest_id=eip.guest_id,
            user_name=g.name if g else "게스트", user_nickname=g.name if g else "게스트",
            is_guest=True, amount_paid=float(eip.amount_paid or 0) if eip.amount_paid else None,
            is_paid=eip.is_paid or False, paid_at=eip.paid_at, created_at=created_at
        )
    else:
        u = db.query(User).filter(User.id == eip.user_id).first()
        return ExpenseParticipantResponse(
            id=eip.id, expense_id=expense_id, user_id=eip.user_id, guest_id=None,
            user_name=u.realname or u.nickname if u else "알 수 없음",
            user_nickname=u.nickname if u else "알 수 없음",
            is_guest=False, amount_paid=float(eip.amount_paid or 0) if eip.amount_paid else None,
            is_paid=eip.is_paid or False, paid_at=eip.paid_at, created_at=created_at
        )


def _build_expense_response(expense: Expense, db: Session) -> ExpenseResponse:
    """Expense + ExpenseItem + ExpenseItemParticipant로 ExpenseResponse 구성"""
    items = db.query(ExpenseItem).filter(ExpenseItem.expense_id == expense.id).all()
    total_amount = sum(float(i.amount or 0) for i in items)
    seen = set()
    participant_responses = []
    for item in items:
        for eip in db.query(ExpenseItemParticipant).filter(ExpenseItemParticipant.expense_item_id == item.id).all():
            key = (eip.user_id, eip.guest_id)
            if key not in seen:
                seen.add(key)
                participant_responses.append(_eip_to_response(eip, expense.id, db))
    total_participants = len(participant_responses) or 1
    amount_per_person = total_amount / total_participants if total_participants else 0
    club = db.query(Club).filter(Club.id == expense.club_id).first()
    creator = db.query(User).filter(User.id == expense.created_by).first()

    notes_display = expense.notes
    settlement_type = None
    try:
        parsed = json.loads(expense.notes or "{}") if isinstance(expense.notes, str) else {}
        if isinstance(parsed, dict) and parsed:
            notes_display = parsed.get("category", expense.notes)
            settlement_type = parsed.get("settlement_type")
    except (json.JSONDecodeError, TypeError):
        pass
    if settlement_type is None and items:
        first_item = items[0]
        if getattr(first_item, "covered_by_fee", False):
            settlement_type = "CLUB_FUND"
        else:
            settlement_type = "EQUAL_SPLIT"

    return ExpenseResponse(
        id=expense.id, title=expense.title, description=expense.description,
        amount=total_amount, total_participants=total_participants, amount_per_person=amount_per_person,
        meeting_id=expense.meeting_id, club_id=expense.club_id, created_by=expense.created_by,
        creator_name=creator.nickname if creator else "알 수 없음",
        notes=notes_display or expense.notes, created_at=expense.created_at, updated_at=expense.updated_at,
        participants=participant_responses,
        settlement_type=settlement_type,
    )

# ===== 모임 비용 정산 API =====

@_expense_routes.get("/{meeting_id}/expenses", response_model=ExpenseListResponse)
async def get_meeting_expenses(
    meeting_id: int,
    page: int = 1,
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """모임 비용 목록 조회"""
    try:
        # 모임 조회 및 권한 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="모임을 찾을 수 없습니다."
            )
        
        # 관리자 권한 확인 (관리자는 모든 데이터 조회 가능)
        if not _is_admin(current_user):
            # 클럽 멤버 권한 확인
            membership = db.query(ClubMembership).filter(
                ClubMembership.club_id == meeting.club_id,
                ClubMembership.user_id == _user_id(current_user)
            ).first()
            
            if not membership:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="클럽 멤버만 비용을 조회할 수 있습니다."
                )
        
        skip = (page - 1) * limit
        expenses = db.query(Expense).filter(Expense.meeting_id == meeting_id).order_by(Expense.created_at.desc()).offset(skip).limit(limit).all()
        total = db.query(Expense).filter(Expense.meeting_id == meeting_id).count()
        expense_responses = [_build_expense_response(e, db) for e in expenses]
        total_pages = (total + limit - 1) // limit if limit > 0 else 0
        return ExpenseListResponse(expenses=expense_responses, total=total, page=page, limit=limit, total_pages=total_pages)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("비용 정산 처리 중 오류: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@_expense_routes.post("/{meeting_id}/expenses", response_model=ExpenseResponse)
async def create_meeting_expense(
    meeting_id: int,
    expense_data: ExpenseCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """모임 비용 등록"""
    try:
        # 모임 조회 및 권한 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="모임을 찾을 수 없습니다."
            )
        
        # 클럽 멤버 권한 확인
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == meeting.club_id,
            ClubMembership.user_id == _user_id(current_user)
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 멤버만 비용을 등록할 수 있습니다."
            )
        
        # 모임 비용 정산은 리더/매니저만 등록 가능
        if membership.role not in [ClubRole.LEADER, ClubRole.MANAGER]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="모임 비용 정산은 클럽 리더/매니저만 등록할 수 있습니다."
            )
        
        from models.enums import ExpenseItemType
        participant_ids = expense_data.participant_ids or []
        n = len(participant_ids) or expense_data.total_participants or 1
        extra_idx = None
        if expense_data.extra_payer_id is not None and participant_ids:
            try:
                extra_idx = participant_ids.index(int(expense_data.extra_payer_id))
            except (ValueError, TypeError):
                pass
        amount_list = split_amount_10won(
            Decimal(str(expense_data.amount)), n,
            extra_recipient_indices=[extra_idx] if extra_idx is not None else None
        ) if n > 0 else []

        expense = Expense(
            title=expense_data.title,
            description=expense_data.description,
            meeting_id=meeting_id,
            club_id=meeting.club_id,
            created_by=_user_id(current_user),
            notes=expense_data.notes,
        )
        db.add(expense)
        db.flush()

        ei = ExpenseItem(
            expense_id=expense.id,
            type=ExpenseItemType.OTHER,
            title=expense_data.title,
            amount=expense_data.amount,
            covered_by_fee=False,
            order_index=0,
        )
        db.add(ei)
        db.flush()

        for idx, participant_id in enumerate(expense_data.participant_ids or []):
            if idx >= len(amount_list):
                break
            mp = db.query(MeetingParticipant).filter(
                MeetingParticipant.id == participant_id,
                MeetingParticipant.meeting_id == meeting_id
            ).first()
            if mp:
                db.add(ExpenseItemParticipant(
                    expense_item_id=ei.id,
                    user_id=mp.user_id,
                    guest_id=mp.guest_id,
                    is_exempted=False,
                    amount=amount_list[idx],
                ))
        db.commit()
        db.refresh(expense)
        return _build_expense_response(expense, db)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("비용 정산 처리 중 오류: %s", e)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@_expense_routes.get("/{meeting_id}/expenses/{expense_id}", response_model=ExpenseResponse)
async def get_meeting_expense(
    meeting_id: int,
    expense_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """모임 비용 상세 조회"""
    try:
        # 모임 조회 및 권한 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="모임을 찾을 수 없습니다."
            )
        
        # 관리자 권한 확인 (관리자는 모든 데이터 조회 가능)
        if not _is_admin(current_user):
            # 클럽 멤버 권한 확인
            membership = db.query(ClubMembership).filter(
                ClubMembership.club_id == meeting.club_id,
                ClubMembership.user_id == _user_id(current_user)
            ).first()
            
            if not membership:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="클럽 멤버만 비용을 조회할 수 있습니다."
                )
        
        # 비용 조회
        expense = db.query(Expense).filter(
            Expense.id == expense_id,
            Expense.meeting_id == meeting_id
        ).first()
        
        if not expense:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="비용을 찾을 수 없습니다."
            )
        
        return _build_expense_response(expense, db)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("비용 정산 처리 중 오류: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@_expense_routes.put("/{meeting_id}/expenses/{expense_id}", response_model=ExpenseResponse)
async def update_meeting_expense(
    meeting_id: int,
    expense_id: int,
    expense_data: ExpenseUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """모임 비용 수정"""
    try:
        # 모임 조회 및 권한 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="모임을 찾을 수 없습니다."
            )
        
        # 클럽 멤버 권한 확인
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == meeting.club_id,
            ClubMembership.user_id == _user_id(current_user)
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 멤버만 비용을 수정할 수 있습니다."
            )
        
        # 비용 조회
        expense = db.query(Expense).filter(
            Expense.id == expense_id,
            Expense.meeting_id == meeting_id
        ).first()
        
        if not expense:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="비용을 찾을 수 없습니다."
            )
        
        # 수정 권한 확인 (생성자 또는 리더/매니저)
        if expense.created_by != _user_id(current_user) and membership.role not in [ClubRole.LEADER, ClubRole.MANAGER]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="비용을 수정할 권한이 없습니다."
            )
        
        if expense_data.title is not None:
            expense.title = expense_data.title
        if expense_data.description is not None:
            expense.description = expense_data.description
        if expense_data.notes is not None:
            expense.notes = expense_data.notes
        expense.updated_at = datetime.now()

        items = db.query(ExpenseItem).filter(ExpenseItem.expense_id == expense.id).all()
        if expense_data.amount is not None or expense_data.total_participants is not None:
            from models.enums import ExpenseItemType
            first_item = next((i for i in items if (i.type.value if hasattr(i.type, "value") else str(i.type)) == "OTHER"), items[0] if items else None)
            if first_item:
                if expense_data.amount is not None:
                    first_item.amount = expense_data.amount
                participants = db.query(ExpenseItemParticipant).filter(ExpenseItemParticipant.expense_item_id == first_item.id).all()
                n = expense_data.total_participants if expense_data.total_participants is not None else len(participants) or 1
                amt = Decimal(str(expense_data.amount if expense_data.amount is not None else first_item.amount or 0))
                extra_idx = None
                if expense_data.extra_payer_id is not None and participants:
                    pid = int(expense_data.extra_payer_id)
                    for i, p in enumerate(participants):
                        if (p.user_id and p.user_id == pid) or (p.guest_id and p.guest_id == pid):
                            extra_idx = i
                            break
                amount_list = split_amount_10won(
                    amt, n,
                    extra_recipient_indices=[extra_idx] if extra_idx is not None else None
                ) if n > 0 else []
                for idx, p in enumerate(participants):
                    if idx < len(amount_list):
                        p.amount = amount_list[idx]
        db.commit()
        db.refresh(expense)
        return _build_expense_response(expense, db)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("비용 정산 처리 중 오류: %s", e)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@_expense_routes.delete("/{meeting_id}/expenses/{expense_id}", response_model=MessageResponse)
async def delete_meeting_expense(
    meeting_id: int,
    expense_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """모임 비용 삭제"""
    try:
        # 모임 조회 및 권한 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="모임을 찾을 수 없습니다."
            )
        
        # 클럽 멤버 권한 확인
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == meeting.club_id,
            ClubMembership.user_id == _user_id(current_user)
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 멤버만 비용을 삭제할 수 있습니다."
            )
        
        # 비용 조회
        expense = db.query(Expense).filter(
            Expense.id == expense_id,
            Expense.meeting_id == meeting_id
        ).first()
        
        if not expense:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="비용을 찾을 수 없습니다."
            )
        
        # 삭제 권한 확인 (생성자 또는 리더/매니저)
        if expense.created_by != _user_id(current_user) and membership.role not in [ClubRole.LEADER, ClubRole.MANAGER]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="비용을 삭제할 권한이 없습니다."
            )
        
        # 비용 삭제 (참가자도 함께 삭제됨 - cascade)
        db.delete(expense)
        db.commit()
        
        return {
            "message": "비용이 삭제되었습니다.",
            "success": True
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("비용 정산 처리 중 오류: %s", e)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@_expense_routes.get("/{meeting_id}/expenses/{expense_id}/participants", response_model=List[ExpenseParticipantResponse])
async def get_expense_participants(
    meeting_id: int,
    expense_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """비용 참가자 목록 조회"""
    try:
        # 모임 조회 및 권한 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="모임을 찾을 수 없습니다."
            )
        
        # 관리자 권한 확인 (관리자는 모든 데이터 조회 가능)
        if not _is_admin(current_user):
            # 클럽 멤버 권한 확인
            membership = db.query(ClubMembership).filter(
                ClubMembership.club_id == meeting.club_id,
                ClubMembership.user_id == _user_id(current_user)
            ).first()
            
            if not membership:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="클럽 멤버만 참가자 목록을 조회할 수 있습니다."
                )
        
        # 비용 조회
        expense = db.query(Expense).filter(
            Expense.id == expense_id,
            Expense.meeting_id == meeting_id
        ).first()
        
        if not expense:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="비용을 찾을 수 없습니다."
            )
        
        resp = _build_expense_response(expense, db)
        return resp.participants or []
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("비용 정산 처리 중 오류: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

class MarkParticipantPaidBody(BaseModel):
    """납부 완료 표시 요청"""
    user_id: Optional[int] = None
    guest_id: Optional[int] = None
    is_paid: bool = True
    amount_paid: Optional[float] = None


@_expense_routes.patch("/{meeting_id}/expenses/{expense_id}/participants/mark-paid")
async def mark_participant_paid(
    meeting_id: int,
    expense_id: int,
    data: MarkParticipantPaidBody = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """정산 참가자 납부 완료/미완료 표시 (모임 관리 권한자만 가능, user_id 또는 guest_id로 일괄 업데이트)"""
    try:
        from sqlalchemy import and_
        from utils.datetime_utils import get_kst_now

        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다.")
        expense = db.query(Expense).filter(
            Expense.id == expense_id,
            Expense.meeting_id == meeting_id
        ).first()
        if not expense:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="정산을 찾을 수 없습니다.")

        user_id = _user_id(current_user)
        if not user_id or not can_manage_settlement(meeting_id, user_id, db):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="정산 관리를 할 권한이 없습니다.")

        pid_user = data.user_id
        pid_guest = data.guest_id
        if (pid_user is None and pid_guest is None) or (pid_user is not None and pid_guest is not None):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="user_id 또는 guest_id 중 하나만 지정해주세요.")

        items = db.query(ExpenseItem).filter(ExpenseItem.expense_id == expense_id).all()
        item_ids = [i.id for i in items]
        filters = [ExpenseItemParticipant.expense_item_id.in_(item_ids)]
        if pid_user is not None:
            filters.append(ExpenseItemParticipant.user_id == pid_user)
        else:
            filters.append(ExpenseItemParticipant.guest_id == pid_guest)

        eips = db.query(ExpenseItemParticipant).filter(and_(*filters)).all()
        if not eips:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="해당 참가자를 찾을 수 없습니다.")

        total_burden = sum(Decimal(str(eip.amount or 0)) for eip in eips)
        paid_at = get_kst_now() if data.is_paid else None
        amount_paid_val = Decimal(str(data.amount_paid)) if data.amount_paid is not None else None
        # 납부완료: (1) 금액 미입력=전액 납부로 간주, (2) 입력 시 부담금 이상일 때만
        if amount_paid_val is not None:
            effective_is_paid = data.is_paid and (amount_paid_val >= total_burden)
        else:
            effective_is_paid = data.is_paid  # 빈 값 = 수동 전액 납부확인
        # amount_paid는 "총 납부액" 의미 - 여러 EIP에 동일 값 저장 시 합산 시 중복되므로 첫 EIP에만 저장
        for idx, eip in enumerate(eips):
            eip.is_paid = effective_is_paid
            eip.paid_at = paid_at if effective_is_paid else None
            if amount_paid_val is not None:
                eip.amount_paid = amount_paid_val if idx == 0 else Decimal("0")
            elif data.is_paid:
                eip.amount_paid = eip.amount or Decimal("0")
            else:
                eip.amount_paid = Decimal("0")
        db.commit()
        return {"message": "납부 상태가 변경되었습니다.", "updated_count": len(eips)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"납부 완료 표시 중 오류: {e}")
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@_expense_routes.put("/{meeting_id}/expenses/{expense_id}/participants/{participant_id}", response_model=ExpenseParticipantResponse)
async def update_expense_participant(
    meeting_id: int,
    expense_id: int,
    participant_id: int,
    participant_data: ExpenseParticipantUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """비용 참가자 상태 변경"""
    try:
        # 모임 조회 및 권한 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="모임을 찾을 수 없습니다."
            )
        
        # 클럽 멤버 권한 확인
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == meeting.club_id,
            ClubMembership.user_id == _user_id(current_user)
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 멤버만 참가자 상태를 변경할 수 있습니다."
            )
        
        # 비용 조회
        expense = db.query(Expense).filter(
            Expense.id == expense_id,
            Expense.meeting_id == meeting_id
        ).first()
        
        if not expense:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="비용을 찾을 수 없습니다."
            )
        
        participant = db.query(ExpenseItemParticipant).filter(
            ExpenseItemParticipant.id == participant_id
        ).first()
        if not participant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="참가자를 찾을 수 없습니다.")
        item = db.query(ExpenseItem).filter(ExpenseItem.id == participant.expense_item_id, ExpenseItem.expense_id == expense_id).first()
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="참가자를 찾을 수 없습니다.")
        if participant.user_id != _user_id(current_user) and membership.role not in [ClubRole.LEADER, ClubRole.MANAGER]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="참가자 상태를 변경할 권한이 없습니다.")
        if participant_data.amount_paid is not None:
            participant.amount_paid = participant_data.amount_paid
        if participant_data.is_paid is not None:
            participant.is_paid = participant_data.is_paid
            if participant_data.is_paid:
                participant.paid_at = datetime.now()
        db.commit()
        db.refresh(participant)
        return _eip_to_response(participant, expense_id, db)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("비용 정산 처리 중 오류: %s", e)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

# ===== 레거시 이벤트 비용 정산 API (Phase 3에서 Event 모델 제거됨) =====
# /events/ 엔드포인트들은 더 이상 작동하지 않음
# 소셜 모임 비용은 /expenses/meetings/{meeting_id}를 사용하세요


# ===== 라우터 노출 =====
# 같은 핸들러를 두 경로에 붙인다.
#   /meetings/{id}/expenses  — 원래 경로
#   /rounds/{id}/expenses    — 앱(1.0, 1.0.1)이 호출하는 경로.
#                              이미 배포된 앱을 바꿀 수 없어 서버에서 맞춘다.
router = APIRouter(prefix="/meetings", tags=["비용 정산"])
router.include_router(_expense_routes)

rounds_expenses_router = APIRouter(prefix="/rounds", tags=["비용 정산"])
rounds_expenses_router.include_router(_expense_routes)
