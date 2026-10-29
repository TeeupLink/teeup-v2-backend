"""
백오피스 클럽 API
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_
import logging
from datetime import datetime

from database import get_db
from models import Club, ClubMembership, ClubRegion, User, ClubStatus, ClubRole
from schemas import MembershipStatus
from services import admin_membership_service as membership_svc
from utils import activity, audit
from utils.datetime_utils import get_kst_now
from utils.display_id_generator import generate_club_display_id
from utils.region import validate_gungu_codes

from .deps import get_admin_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-clubs"])

@router.get("/clubs")
def admin_list_clubs(page: int = 1,
                     limit: int = 10,
                     status_filter: str | None = None,
                     search: str | None = None,
                     current_user: dict = Depends(get_admin_user),
                     db: Session = Depends(get_db)):
    """관리자용 클럽 목록 조회"""
    try:
        # 관리자 페이지에서는 삭제된 클럽도 조회 (deleted_at 필드로 구분)
        query = db.query(Club)
        if status_filter:
            try:
                status_enum = ClubStatus[status_filter]
                query = query.filter(Club.status == status_enum)
            except Exception:
                pass
        if search:
            like = f"%{search}%"
            query = query.filter((Club.name.ilike(like)) | (Club.description.ilike(like)))
        total = query.count()
        items = (query.order_by(Club.created_at.desc()).offset((page - 1) * limit).limit(limit).all())
        data = []
        for c in items:
            # 대표자/리더명 계산: LEADER 멤버십의 사용자명 (realname > nickname > email 우선순위)
            leader_name = None
            try:
                leader_membership = (db.query(ClubMembership,
                                              User).join(User, ClubMembership.user_id == User.id).filter(
                                                  ClubMembership.club_id == c.id,
                                                  ClubMembership.role == ClubRole.LEADER).first())
                if leader_membership:
                    _, leader_user = leader_membership
                    leader_name = getattr(leader_user, 'realname', None) or leader_user.nickname or leader_user.email
            except Exception:
                pass

            # 실제 멤버 수 계산 (승인된 멤버만)
            # ACTIVE 또는 APPROVED 상태의 멤버를 카운트 (일부는 APPROVED로 저장될 수 있음)
            from sqlalchemy import or_

            # 모든 멤버십 상태 확인 (디버깅용)
            all_memberships = db.query(ClubMembership).filter(ClubMembership.club_id == c.id).all()

            actual_member_count = db.query(ClubMembership).filter(
                ClubMembership.club_id == c.id,
                or_(
                    ClubMembership.status == MembershipStatus.ACTIVE,
                    ClubMembership.status == "APPROVED"  # 이전에 APPROVED로 저장된 멤버도 포함
                )).count()

            # 디버깅: 예상 멤버수와 실제 멤버 수 비교
            expected_count = getattr(c, 'member_count', None)
            logger.info(f"Club {c.id} ({c.name}): expected={expected_count}, actual={actual_member_count}")
            if all_memberships:
                status_counts = {}
                for m in all_memberships:
                    status_counts[m.status] = status_counts.get(m.status, 0) + 1
                logger.info(f"Club {c.id} membership statuses: {status_counts}")

            data.append({
                "id":
                c.id,
                "display_id":
                getattr(c, 'display_id', None),
                "name":
                c.name,
                "description":
                c.description,
                "type":
                c.type.value if hasattr(c.type, 'value') else str(c.type),
                "member_count":
                actual_member_count,  # 실제 멤버 수 사용
                "location":
                getattr(c, 'location', None),
                "sido_code":
                c.sido_code,
                "gungu_codes":
                [region.gungu_code for region in db.query(ClubRegion).filter(ClubRegion.club_id == c.id).all()],
                "representative_name":
                getattr(c, 'representative_name', None),
                "leader_name":
                leader_name,
                "status":
                c.status.value if hasattr(c.status, 'value') else str(c.status),
                "deleted_at":
                c.deleted_at.isoformat() if c.deleted_at else None,
                "created_at":
                c.created_at,
                "updated_at":
                c.updated_at,
            })
        return {
            "data": data,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": (total + limit - 1) // limit,
        }
    except Exception as e:
        logger.error(f"admin list clubs error: {e}")
@router.get("/clubs/{club_id}")
async def get_admin_club(club_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """관리자용 클럽 상세 조회 (display_id 또는 id로 조회)"""
    try:
        from models import Club, ClubMembership, ClubRole

        # display_id로 먼저 조회 시도, 없으면 id로 조회
        club = db.query(Club).filter(Club.display_id == club_id).first()

        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(Club.id == club_id_int).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None

        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다")

        # 현재 리더 정보 조회
        current_leader = db.query(ClubMembership).filter(ClubMembership.club_id == club.id,
                                                         ClubMembership.role == ClubRole.LEADER,
                                                         ClubMembership.status.in_(["ACTIVE", "APPROVED"])).first()

        # 실제 멤버 수 계산 (승인된 멤버만)
        from sqlalchemy import or_
        from schemas import MembershipStatus
        current_member_count = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            or_(
                ClubMembership.status == MembershipStatus.ACTIVE,
                ClubMembership.status == "APPROVED"  # 이전에 APPROVED로 저장된 멤버도 포함
            )).count()

        return {
            "id":
            club.id,
            "display_id":
            club.display_id,
            "name":
            club.name,
            "sido_code":
            club.sido_code,
            "gungu_codes":
            [region.gungu_code for region in db.query(ClubRegion).filter(ClubRegion.club_id == club.id).all()],
            "type":
            club.type.value if hasattr(club.type, 'value') else str(club.type),
            "description":
            club.description,
            "status":
            club.status.value if hasattr(club.status, 'value') else str(club.status),
            "representative_name":
            club.representative_name,
            "representative_id":
            current_leader.user_id if current_leader else None,
            "location":
            club.location,
            "contact_info":
            club.contact_info,
            "additional_info":
            club.additional_info,
            "profile_image":
            club.profile_image,
            "member_count":
            club.member_count,  # 예상 멤버 수 (레거시)
            "current_member_count":
            current_member_count,  # 실제 멤버 수
            "created_at":
            club.created_at.isoformat() if club.created_at else None,
            "updated_at":
            club.updated_at.isoformat() if club.updated_at else None
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 클럽 상세 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="클럽 상세 조회 중 오류가 발생했습니다")


def _resolve_club(db: Session, club_id: str) -> Club:
    """club_id(display_id 또는 id)로 Club 조회"""
    club = db.query(Club).filter(Club.display_id == club_id, Club.deleted_at.is_(None)).first()
    if not club:
        try:
            club = db.query(Club).filter(Club.id == int(club_id), Club.deleted_at.is_(None)).first()
        except ValueError:
            club = None
    if not club:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다")
    return club


@router.get("/clubs/{club_id}/notices")
async def get_admin_club_notices(
    club_id: str,
    page: int = 1,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 공지사항 조회"""
    from models import ClubNotice, User
    club = _resolve_club(db, club_id)
    offset = (page - 1) * limit
    notices_query = db.query(ClubNotice).filter(ClubNotice.club_id == club.id)
    total = notices_query.count()
    notices = notices_query.order_by(
        ClubNotice.is_important.desc(),
        ClubNotice.created_at.desc()
    ).offset(offset).limit(limit).all()
    notice_list = []
    for n in notices:
        author = db.query(User).filter(User.id == n.author_id).first()
        notice_list.append({
            "id": n.id,
            "club_id": n.club_id,
            "title": n.title,
            "content": n.content,
            "is_important": n.is_important,
            "is_private": n.is_private,
            "author_id": n.author_id,
            "author_name": author.realname or author.nickname if author else "Unknown",
            "view_count": n.view_count or 0,
            "created_at": n.created_at.isoformat() if n.created_at else None,
            "updated_at": n.updated_at.isoformat() if n.updated_at else None,
        })
    return {
        "data": notice_list,
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": (total + limit - 1) // limit if limit > 0 else 0,
    }


@router.get("/clubs/{club_id}/regulations")
async def get_admin_club_regulations(
    club_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 규정 조회"""
    from models import RegulationCategory, Regulation
    club = _resolve_club(db, club_id)
    categories = db.query(RegulationCategory).filter(
        RegulationCategory.club_id == club.id
    ).order_by(RegulationCategory.order).all()
    result_categories = []
    for cat in categories:
        regulations = db.query(Regulation).filter(
            Regulation.category_id == cat.id
        ).order_by(Regulation.created_at).all()
        regulations_list = [
            {
                "id": r.id,
                "title": r.title,
                "content": r.content,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in regulations
        ]
        result_categories.append({
            "id": cat.id,
            "name": cat.name,
            "order": cat.order,
            "regulations": regulations_list,
        })
    return {"categories": result_categories, "total_categories": len(result_categories)}


@router.get("/clubs/{club_id}/fees")
async def get_admin_club_fees(
    club_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 회비 항목 조회"""
    from models import ClubFee
    club = _resolve_club(db, club_id)
    fees = db.query(ClubFee).filter(ClubFee.club_id == club.id).order_by(ClubFee.created_at.desc()).all()
    fee_list = []
    for f in fees:
        cycle_val = f.cycle.value if hasattr(f.cycle, "value") else str(f.cycle) if f.cycle else None
        fee_list.append({
            "id": f.id,
            "club_id": f.club_id,
            "name": f.name,
            "amount": float(f.amount) if f.amount else 0,
            "cycle": cycle_val,
            "description": f.description,
            "is_active": f.is_active if f.is_active is not None else True,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        })
    return fee_list


def _get_club_posting_user_id(db: Session, club_id: int) -> int:
    """관리자 대신 게시할 때 사용할 User ID (클럽 리더 우선)"""
    leader = db.query(ClubMembership).filter(
        ClubMembership.club_id == club_id,
        ClubMembership.role == ClubRole.LEADER,
        ClubMembership.status.in_(["ACTIVE", "APPROVED"]),
    ).first()
    if leader:
        return leader.user_id
    member = db.query(ClubMembership).filter(
        ClubMembership.club_id == club_id,
        ClubMembership.status.in_(["ACTIVE", "APPROVED"]),
    ).first()
    if member:
        return member.user_id
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="클럽에 등록된 멤버가 없어 공지/회비를 등록할 수 없습니다. 먼저 멤버를 추가해주세요.",
    )


@router.post("/clubs/{club_id}/notices")
async def create_admin_club_notice(
    club_id: str,
    notice_data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 공지사항 생성"""
    from models import ClubNotice, User
    from schemas import ClubNoticeCreate
    club = _resolve_club(db, club_id)
    author_id = _get_club_posting_user_id(db, club.id)
    data = ClubNoticeCreate(**notice_data)
    notice = ClubNotice(
        club_id=club.id,
        title=data.title,
        content=data.content,
        is_important=data.is_important,
        is_private=data.is_private,
        author_id=author_id,
        view_count=0,
    )
    db.add(notice)
    db.commit()
    db.refresh(notice)
    author = db.query(User).filter(User.id == notice.author_id).first()
    return {
        "id": notice.id,
        "club_id": notice.club_id,
        "title": notice.title,
        "content": notice.content,
        "is_important": notice.is_important,
        "is_private": notice.is_private,
        "author_id": notice.author_id,
        "author_name": author.realname or author.nickname if author else "Unknown",
        "view_count": notice.view_count or 0,
        "created_at": notice.created_at.isoformat() if notice.created_at else None,
        "updated_at": notice.updated_at.isoformat() if notice.updated_at else None,
    }


@router.get("/clubs/{club_id}/notices/{notice_id}")
async def get_admin_club_notice_detail(
    club_id: str,
    notice_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 공지사항 상세 조회"""
    from models import ClubNotice, User
    club = _resolve_club(db, club_id)
    notice = db.query(ClubNotice).filter(
        ClubNotice.id == notice_id,
        ClubNotice.club_id == club.id,
    ).first()
    if not notice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공지사항을 찾을 수 없습니다")
    author = db.query(User).filter(User.id == notice.author_id).first()
    return {
        "id": notice.id,
        "club_id": notice.club_id,
        "title": notice.title,
        "content": notice.content,
        "is_important": notice.is_important,
        "is_private": notice.is_private,
        "author_id": notice.author_id,
        "author_name": author.realname or author.nickname if author else "Unknown",
        "view_count": notice.view_count or 0,
        "created_at": notice.created_at.isoformat() if notice.created_at else None,
        "updated_at": notice.updated_at.isoformat() if notice.updated_at else None,
    }


@router.put("/clubs/{club_id}/notices/{notice_id}")
async def update_admin_club_notice(
    club_id: str,
    notice_id: int,
    notice_data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 공지사항 수정"""
    from models import ClubNotice, User
    from schemas import ClubNoticeUpdate
    club = _resolve_club(db, club_id)
    notice = db.query(ClubNotice).filter(
        ClubNotice.id == notice_id,
        ClubNotice.club_id == club.id,
    ).first()
    if not notice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공지사항을 찾을 수 없습니다")
    data = ClubNoticeUpdate(**{k: v for k, v in notice_data.items() if k in ("title", "content", "is_important", "is_private")})
    if data.title is not None:
        notice.title = data.title
    if data.content is not None:
        notice.content = data.content
    if data.is_important is not None:
        notice.is_important = data.is_important
    if data.is_private is not None:
        notice.is_private = data.is_private
    notice.updated_at = get_kst_now()
    db.commit()
    db.refresh(notice)
    author = db.query(User).filter(User.id == notice.author_id).first()
    return {
        "id": notice.id,
        "club_id": notice.club_id,
        "title": notice.title,
        "content": notice.content,
        "is_important": notice.is_important,
        "is_private": notice.is_private,
        "author_id": notice.author_id,
        "author_name": author.realname or author.nickname if author else "Unknown",
        "view_count": notice.view_count or 0,
        "created_at": notice.created_at.isoformat() if notice.created_at else None,
        "updated_at": notice.updated_at.isoformat() if notice.updated_at else None,
    }


@router.delete("/clubs/{club_id}/notices/{notice_id}")
async def delete_admin_club_notice(
    club_id: str,
    notice_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 공지사항 삭제"""
    from models import ClubNotice
    club = _resolve_club(db, club_id)
    notice = db.query(ClubNotice).filter(
        ClubNotice.id == notice_id,
        ClubNotice.club_id == club.id,
    ).first()
    if not notice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공지사항을 찾을 수 없습니다")
    db.delete(notice)
    db.commit()
    return {"message": "공지사항이 삭제되었습니다", "success": True}


@router.post("/clubs/{club_id}/fees")
async def create_admin_club_fee(
    club_id: str,
    fee_data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 회비 항목 생성"""
    from models import ClubFee
    from schemas import ClubFeeCreate, BillingCycle
    club = _resolve_club(db, club_id)
    created_by = _get_club_posting_user_id(db, club.id)
    data = ClubFeeCreate(**fee_data)
    cycle_val = None
    if data.cycle:
        cycle_val = BillingCycle[data.cycle] if isinstance(data.cycle, str) else data.cycle
    fee = ClubFee(
        club_id=club.id,
        name=data.name,
        amount=data.amount,
        cycle=cycle_val,
        description=data.description,
        is_active=data.is_active,
        created_by=created_by,
    )
    db.add(fee)
    db.commit()
    db.refresh(fee)
    return {
        "id": fee.id,
        "club_id": fee.club_id,
        "name": fee.name,
        "amount": float(fee.amount),
        "cycle": fee.cycle.value if fee.cycle else None,
        "description": fee.description,
        "is_active": fee.is_active,
        "created_by": fee.created_by,
        "created_at": fee.created_at.isoformat() if fee.created_at else None,
        "updated_at": fee.updated_at.isoformat() if fee.updated_at else None,
    }


@router.put("/clubs/{club_id}/fees/{fee_id}")
async def update_admin_club_fee(
    club_id: str,
    fee_id: int,
    fee_data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 회비 항목 수정"""
    from models import ClubFee
    from schemas import ClubFeeUpdate, BillingCycle
    club = _resolve_club(db, club_id)
    fee = db.query(ClubFee).filter(
        ClubFee.id == fee_id,
        ClubFee.club_id == club.id,
    ).first()
    if not fee:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="회비 항목을 찾을 수 없습니다")
    data = ClubFeeUpdate(**{k: v for k, v in fee_data.items() if k in ("name", "amount", "cycle", "description", "is_active")})
    if data.name is not None:
        fee.name = data.name
    if data.amount is not None:
        fee.amount = data.amount
    if data.cycle is not None:
        fee.cycle = BillingCycle[data.cycle] if isinstance(data.cycle, str) else data.cycle
    if data.description is not None:
        fee.description = data.description
    if data.is_active is not None:
        fee.is_active = data.is_active
    fee.updated_at = get_kst_now()
    db.commit()
    db.refresh(fee)
    return {
        "id": fee.id,
        "club_id": fee.club_id,
        "name": fee.name,
        "amount": float(fee.amount),
        "cycle": fee.cycle.value if fee.cycle else None,
        "description": fee.description,
        "is_active": fee.is_active,
        "created_by": fee.created_by,
        "created_at": fee.created_at.isoformat() if fee.created_at else None,
        "updated_at": fee.updated_at.isoformat() if fee.updated_at else None,
    }


@router.delete("/clubs/{club_id}/fees/{fee_id}")
async def delete_admin_club_fee(
    club_id: str,
    fee_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 회비 항목 삭제"""
    from models import ClubFee
    club = _resolve_club(db, club_id)
    fee = db.query(ClubFee).filter(
        ClubFee.id == fee_id,
        ClubFee.club_id == club.id,
    ).first()
    if not fee:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="회비 항목을 찾을 수 없습니다")
    db.delete(fee)
    db.commit()
    return {"message": "회비 항목이 삭제되었습니다", "success": True}


# 규정: Regulation의 created_by는 User FK가 아니므로 관리자 이름 사용 가능
@router.get("/clubs/{club_id}/regulations/categories")
async def get_admin_club_regulation_categories(
    club_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 규정 카테고리 목록 조회"""
    from models import RegulationCategory
    club = _resolve_club(db, club_id)
    categories = db.query(RegulationCategory).filter(
        RegulationCategory.club_id == club.id
    ).order_by(RegulationCategory.order).all()
    return [
        {"id": c.id, "name": c.name, "order": c.order}
        for c in categories
    ]


@router.post("/clubs/{club_id}/regulations/categories")
async def create_admin_club_regulation_category(
    club_id: str,
    category_data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 규정 카테고리 생성"""
    from models import RegulationCategory
    club = _resolve_club(db, club_id)
    name = category_data.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="카테고리 이름을 입력해주세요")
    max_order = db.query(RegulationCategory).filter(
        RegulationCategory.club_id == club.id
    ).count()
    cat = RegulationCategory(club_id=club.id, name=name, order=max_order)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return {"id": cat.id, "name": cat.name, "order": cat.order}


@router.put("/clubs/{club_id}/regulations/categories/{category_id}")
async def update_admin_club_regulation_category(
    club_id: str,
    category_id: int,
    category_data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 규정 카테고리 수정"""
    from models import RegulationCategory
    club = _resolve_club(db, club_id)
    cat = db.query(RegulationCategory).filter(
        RegulationCategory.id == category_id,
        RegulationCategory.club_id == club.id,
    ).first()
    if not cat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="카테고리를 찾을 수 없습니다")
    if "name" in category_data and category_data["name"]:
        cat.name = category_data["name"].strip()
    if "order" in category_data:
        cat.order = int(category_data["order"])
    db.commit()
    db.refresh(cat)
    return {"id": cat.id, "name": cat.name, "order": cat.order}


@router.delete("/clubs/{club_id}/regulations/categories/{category_id}")
async def delete_admin_club_regulation_category(
    club_id: str,
    category_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 규정 카테고리 삭제"""
    from models import RegulationCategory
    club = _resolve_club(db, club_id)
    cat = db.query(RegulationCategory).filter(
        RegulationCategory.id == category_id,
        RegulationCategory.club_id == club.id,
    ).first()
    if not cat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="카테고리를 찾을 수 없습니다")
    db.delete(cat)
    db.commit()
    return {"message": "카테고리가 삭제되었습니다", "success": True}


@router.post("/clubs/{club_id}/regulations")
async def create_admin_club_regulation(
    club_id: str,
    regulation_data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 규정 생성"""
    from models import Regulation, RegulationCategory
    from schemas import ClubRegulationCreate
    club = _resolve_club(db, club_id)
    data = ClubRegulationCreate(**regulation_data)
    category = db.query(RegulationCategory).filter(
        RegulationCategory.id == data.category_id,
        RegulationCategory.club_id == club.id,
    ).first()
    if not category:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="규정 카테고리를 찾을 수 없습니다")
    admin_name = current_user.get("name", "관리자")
    reg = Regulation(
        club_id=club.id,
        category_id=data.category_id,
        title=data.title,
        content=data.content,
        status=data.status or "ACTIVE",
        created_by=0,
        created_by_name=f"{admin_name} (관리자)",
        published_at=datetime.now() if (data.status or "ACTIVE") == "ACTIVE" else None,
    )
    db.add(reg)
    db.commit()
    db.refresh(reg)
    return {
        "id": reg.id,
        "club_id": reg.club_id,
        "category_id": reg.category_id,
        "title": reg.title,
        "content": reg.content,
        "status": reg.status,
        "created_by": reg.created_by,
        "created_by_name": reg.created_by_name,
        "published_at": reg.published_at.isoformat() if reg.published_at else None,
        "created_at": reg.created_at.isoformat() if reg.created_at else None,
        "updated_at": reg.updated_at.isoformat() if reg.updated_at else None,
        "category_name": category.name,
    }


@router.get("/clubs/{club_id}/regulations/{regulation_id}")
async def get_admin_club_regulation_detail(
    club_id: str,
    regulation_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 규정 상세 조회"""
    from models import Regulation, RegulationCategory
    club = _resolve_club(db, club_id)
    reg = db.query(Regulation).filter(
        Regulation.id == regulation_id,
        Regulation.club_id == club.id,
    ).first()
    if not reg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="규정을 찾을 수 없습니다")
    cat = db.query(RegulationCategory).filter(RegulationCategory.id == reg.category_id).first()
    return {
        "id": reg.id,
        "club_id": reg.club_id,
        "category_id": reg.category_id,
        "title": reg.title,
        "content": reg.content,
        "status": reg.status,
        "created_by": reg.created_by,
        "created_by_name": reg.created_by_name,
        "published_at": reg.published_at.isoformat() if reg.published_at else None,
        "created_at": reg.created_at.isoformat() if reg.created_at else None,
        "updated_at": reg.updated_at.isoformat() if reg.updated_at else None,
        "category_name": cat.name if cat else None,
    }


@router.put("/clubs/{club_id}/regulations/{regulation_id}")
async def update_admin_club_regulation(
    club_id: str,
    regulation_id: int,
    regulation_data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 규정 수정"""
    from models import Regulation, RegulationCategory
    from schemas import ClubRegulationUpdate
    club = _resolve_club(db, club_id)
    reg = db.query(Regulation).filter(
        Regulation.id == regulation_id,
        Regulation.club_id == club.id,
    ).first()
    if not reg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="규정을 찾을 수 없습니다")
    data = ClubRegulationUpdate(**{k: v for k, v in regulation_data.items() if k in ("category_id", "title", "content", "status")})
    if data.category_id is not None:
        cat = db.query(RegulationCategory).filter(
            RegulationCategory.id == data.category_id,
            RegulationCategory.club_id == club.id,
        ).first()
        if not cat:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="규정 카테고리를 찾을 수 없습니다")
        reg.category_id = data.category_id
    if data.title is not None:
        reg.title = data.title
    if data.content is not None:
        reg.content = data.content
    if data.status is not None:
        reg.status = data.status
    reg.updated_at = get_kst_now()
    db.commit()
    db.refresh(reg)
    cat = db.query(RegulationCategory).filter(RegulationCategory.id == reg.category_id).first()
    return {
        "id": reg.id,
        "club_id": reg.club_id,
        "category_id": reg.category_id,
        "title": reg.title,
        "content": reg.content,
        "status": reg.status,
        "created_by": reg.created_by,
        "created_by_name": reg.created_by_name,
        "published_at": reg.published_at.isoformat() if reg.published_at else None,
        "created_at": reg.created_at.isoformat() if reg.created_at else None,
        "updated_at": reg.updated_at.isoformat() if reg.updated_at else None,
        "category_name": cat.name if cat else None,
    }


@router.delete("/clubs/{club_id}/regulations/{regulation_id}")
async def delete_admin_club_regulation(
    club_id: str,
    regulation_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 규정 삭제"""
    from models import Regulation
    club = _resolve_club(db, club_id)
    reg = db.query(Regulation).filter(
        Regulation.id == regulation_id,
        Regulation.club_id == club.id,
    ).first()
    if not reg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="규정을 찾을 수 없습니다")
    db.delete(reg)
    db.commit()
    return {"message": "규정이 삭제되었습니다", "success": True}


@router.post("/clubs")
async def create_admin_club(club_data: dict,
                            db: Session = Depends(get_db),
                            current_user: dict = Depends(get_admin_user)):
    """관리자용 클럽 생성"""

    try:
        # 대표자 사용자 조회
        representative_id = club_data.get('representative_id')
        if not representative_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="대표자 사용자 ID가 필요합니다.")

        representative_user = db.query(User).filter(User.id == representative_id, User.deleted_at.is_(None)).first()

        if not representative_user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="대표자 사용자를 찾을 수 없습니다.")

        # display_id 생성
        display_id = generate_club_display_id(db)

        # 정기 회비 정보 처리 (ClubFee로 생성)
        regular_fee = club_data.get('regular_fee')
        has_regular_fee = False
        regular_fee_amount = None
        regular_fee_cycle = None
        regular_fee_description = None

        if regular_fee:
            has_regular_fee = True
            regular_fee_amount = regular_fee.get('amount')
            regular_fee_cycle = regular_fee.get('cycle')
            regular_fee_description = regular_fee.get('description', '')
        else:
            # 직접 전달된 경우도 처리
            has_regular_fee = club_data.get('has_regular_fee', False)
            regular_fee_amount = club_data.get('regular_fee_amount')
            regular_fee_cycle = club_data.get('regular_fee_cycle')
            regular_fee_description = club_data.get('regular_fee_description', '')

        # 대표자명 결정: realname > nickname > email 우선순위
        representative_name = (getattr(representative_user, 'realname', None) or representative_user.nickname
                               or representative_user.email)

        if not club_data.get("sido_code"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="시도를 선택하셔야 합니다.")

        gungu_codes = club_data.get("gungu_codes") or []
        unique_gungu_codes = validate_gungu_codes(db, club_data.get("sido_code"), gungu_codes)

        # 클럽 생성 (관리자가 생성하므로 바로 승인)
        club = Club(
            display_id=display_id,
            name=club_data['name'],
            sido_code=club_data["sido_code"],
            type=club_data['type'],
            description=club_data.get('description', ''),
            location=club_data.get('location', ''),
            contact_info=club_data.get('contact_info', ''),
            representative_name=representative_name,
            additional_info=club_data.get('additional_info', ''),
            member_count=club_data.get('member_count', 1),  # 대표자 포함 또는 지정된 값
            status=ClubStatus.ACTIVE  # 관리자가 생성하므로 바로 활성화
        )

        db.add(club)
        db.commit()
        db.refresh(club)

        for gungu_code in unique_gungu_codes:
            db.add(ClubRegion(club_id=club.id, gungu_code=gungu_code))

        # 지정된 사용자를 리더로 추가
        membership = ClubMembership(club_id=club.id,
                                    user_id=representative_id,
                                    role=ClubRole.LEADER,
                                    status=MembershipStatus.ACTIVE)

        db.add(membership)

        # 정기 회비가 있는 경우 ClubFee로 생성
        if has_regular_fee and regular_fee_amount and regular_fee_cycle:
            from models import ClubFee
            regular_fee_obj = ClubFee(club_id=club.id,
                                      name="정기 회비",
                                      amount=float(regular_fee_amount),
                                      cycle=regular_fee_cycle,
                                      description=regular_fee_description or '',
                                      is_active=True,
                                      created_by=representative_id)
            db.add(regular_fee_obj)

        db.commit()

        # 응답용 정기 회비 정보 조회
        from models import ClubFee as ClubFeeModel
        from models.enums import BillingCycle
        regular_fee_response = db.query(ClubFeeModel).filter(
            ClubFeeModel.club_id == club.id, ClubFeeModel.is_active == True,
            ClubFeeModel.cycle.in_([BillingCycle.MONTHLY, BillingCycle.QUARTERLY, BillingCycle.YEARLY])).first()

        return {
            "id":
            club.id,
            "display_id":
            club.display_id,
            "name":
            club.name,
            "sido_code":
            club.sido_code,
            "gungu_codes":
            [region.gungu_code for region in db.query(ClubRegion).filter(ClubRegion.club_id == club.id).all()],
            "type":
            club.type.value,
            "description":
            club.description,
            "status":
            club.status.value,
            "representative_name":
            club.representative_name,
            "representative_id":
            representative_id,
            "location":
            club.location,
            "contact_info":
            club.contact_info,
            "additional_info":
            club.additional_info,
            "member_count":
            club.member_count,
            "has_regular_fee":
            regular_fee_response is not None,
            "regular_fee_amount":
            float(regular_fee_response.amount) if regular_fee_response and regular_fee_response.amount else None,
            "regular_fee_cycle":
            regular_fee_response.cycle.value if regular_fee_response and regular_fee_response.cycle else None,
            "regular_fee_description":
            regular_fee_response.description if regular_fee_response else None,
            "created_at":
            club.created_at.isoformat(),
            "updated_at":
            club.updated_at.isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"관리자 클럽 생성 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"클럽 생성 중 오류가 발생했습니다: {str(e)}")


@router.get("/clubs/{club_id}/pending-members")
async def get_admin_club_pending_members(club_id: str,
                                         db: Session = Depends(get_db),
                                         current_user: dict = Depends(get_admin_user)):
    """관리자용 클럽 가입 신청 대기자 목록 조회"""
    try:
        from models import Club, ClubMembership, User
        from schemas import MembershipStatus

        # 클럽 조회 (display_id 또는 id로)
        club = db.query(Club).filter(Club.display_id == club_id).first()
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(Club.id == club_id_int).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None

        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다")

        # PENDING 상태의 멤버들 조회
        pending_memberships = db.query(ClubMembership).join(User).filter(
            ClubMembership.club_id == club.id, ClubMembership.status == MembershipStatus.PENDING,
            User.deleted_at.is_(None)).all()

        members = []
        for membership in pending_memberships:
            user = membership.user
            members.append({
                "membership_id": membership.id,
                "user_id": user.id,
                "user_nickname": user.nickname,
                "user_email": user.email,
                "user_realname": user.realname,
                "role": membership.role.value if membership.role else "MEMBER",
                "status": membership.status.value,
                "applied_at": membership.created_at.isoformat() if membership.created_at else None
            })

        return {"club_id": club.id, "club_name": club.name, "pending_members": members, "total_pending": len(members)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 클럽 가입 대기자 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="가입 대기자 조회 중 오류가 발생했습니다")


@router.get("/clubs/{club_id}/members")
async def get_admin_club_members(
    club_id: str,
    search: str | None = Query(None, description="닉네임/이메일/실명/전화번호 검색"),
    role_filter: str | None = Query(None, description="역할 필터: LEADER, MANAGER, MEMBER"),
    status_filter: str | None = Query(None, description="상태 필터: ACTIVE, PENDING 등"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 클럽 멤버 목록 조회 (검색·역할·상태 필터 지원)"""
    try:
        from models import Club, ClubMembership, ClubRole, User

        # display_id로 먼저 조회 시도, 없으면 id로 조회
        club = db.query(Club).filter(Club.display_id == club_id).first()

        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(Club.id == club_id_int).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None

        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다")

        # 멤버십과 사용자 정보를 JOIN으로 한번에 조회
        query = db.query(ClubMembership, User).join(User, ClubMembership.user_id == User.id).filter(
            ClubMembership.club_id == club.id,
            User.deleted_at.is_(None),
            ~User.email.like('%@guest.local'),  # 게스트 이메일 제외
            ~User.nickname.like('guest_%')  # 게스트 닉네임 제외
        )

        # 검색 필터 (닉네임, 이메일, 실명, 전화번호)
        if search and search.strip():
            term = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    User.nickname.ilike(term),
                    User.email.ilike(term),
                    User.realname.ilike(term),
                    User.phone_number.ilike(term),
                )
            )
        # 역할 필터
        if role_filter and role_filter.upper() in ("LEADER", "MANAGER", "MEMBER"):
            query = query.filter(ClubMembership.role == ClubRole[role_filter.upper()])
        # 상태 필터
        if status_filter:
            try:
                status_enum = MembershipStatus(status_filter)
                query = query.filter(ClubMembership.status == status_enum)
            except (ValueError, KeyError):
                pass

        results = query.all()

        # 멤버 데이터 구성
        members = []
        for membership, user in results:
            # Enum인지 문자열인지 확인하여 적절히 처리
            role_value = membership.role.value if hasattr(membership.role, 'value') else str(membership.role)
            status_value = membership.status.value if hasattr(membership.status, 'value') else str(membership.status)

            # 리더는 항상 APPROVED 상태로 강제 설정
            leader_role = ClubRole.LEADER.value if hasattr(ClubRole.LEADER, 'value') else "LEADER"
            final_status = "APPROVED" if role_value == leader_role else status_value

            member = {
                "id": membership.id,
                "club_id": membership.club_id,
                "user_id": membership.user_id,
                "role": role_value,
                "status": final_status,
                "created_at": membership.created_at.isoformat(),
                "updated_at": membership.updated_at.isoformat(),
                "user_nickname": user.nickname,
                "user_email": user.email,
                "user_realname": user.realname,  # 관리자용이므로 마스킹하지 않음
                "user_phone_number": user.phone_number,
                "user_birthdate": user.birthdate.isoformat() if user.birthdate else None,
                "user_gender": user.gender,
                "user_handicap": user.handicap,
                "user_average_score": user.average_score
            }
            members.append(member)

        return {"members": members, "total_members": len(members)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 클럽 멤버 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"클럽 멤버 조회 중 오류가 발생했습니다: {str(e)}")


@router.put("/clubs/{club_id}/members/{user_id}/approve")
async def approve_admin_club_membership(club_id: str,
                                        user_id: int,
                                        db: Session = Depends(get_db),
                                        current_user: dict = Depends(get_admin_user)):
    """관리자용 클럽 가입 승인"""
    try:
        from models import Club, ClubMembership, User
        from schemas import MembershipStatus

        # 클럽 조회 (display_id 또는 id로)
        club = db.query(Club).filter(Club.display_id == club_id).first()
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(Club.id == club_id_int).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None

        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다")

        # 가입 신청 조회
        membership = db.query(ClubMembership).filter(ClubMembership.club_id == club.id,
                                                     ClubMembership.user_id == user_id,
                                                     ClubMembership.status == MembershipStatus.PENDING).first()

        if not membership:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="가입 신청을 찾을 수 없습니다")

        before = membership_svc.describe(db, membership)

        # 가입 승인
        membership.status = MembershipStatus.ACTIVE
        membership.updated_at = get_kst_now()

        # 클럽 멤버 수 증가
        club.member_count = (club.member_count or 0) + 1
        club.updated_at = get_kst_now()

        db.commit()
        audit.record("APPROVE", "CLUB_MEMBERSHIP", membership.id, before=before, after={"status": "ACTIVE"})
        activity.log(user_id, "CLUB_JOIN", f"{club.name} 가입", target_type="CLUB", target_id=club.id)

        # status 값 추출 (Enum인지 문자열인지 확인)
        status_value = membership.status.value if hasattr(membership.status, 'value') else str(membership.status)

        return {"message": "클럽 가입이 승인되었습니다", "membership_id": membership.id, "user_id": user_id, "status": status_value}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 클럽 가입 승인 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"가입 승인 중 오류가 발생했습니다: {str(e)}")


@router.put("/clubs/{club_id}/members/{user_id}/reject")
async def reject_admin_club_membership(club_id: str,
                                       user_id: int,
                                       db: Session = Depends(get_db),
                                       current_user: dict = Depends(get_admin_user)):
    """관리자용 클럽 가입 거절"""
    try:
        from models import Club, ClubMembership, User
        from schemas import MembershipStatus

        # 클럽 조회 (display_id 또는 id로)
        club = db.query(Club).filter(Club.display_id == club_id).first()
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(Club.id == club_id_int).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None

        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다")

        # 가입 신청 조회
        membership = db.query(ClubMembership).filter(ClubMembership.club_id == club.id,
                                                     ClubMembership.user_id == user_id,
                                                     ClubMembership.status == MembershipStatus.PENDING).first()

        if not membership:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="가입 신청을 찾을 수 없습니다")

        before = membership_svc.describe(db, membership)

        # 가입 거절 (멤버십 삭제)
        db.delete(membership)
        db.commit()
        audit.record("REJECT", "CLUB_MEMBERSHIP", before["membership_id"], before=before)

        return {"message": "클럽 가입이 거절되었습니다", "user_id": user_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 클럽 가입 거절 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"가입 거절 중 오류가 발생했습니다: {str(e)}")


@router.post("/clubs/{club_id}/members")
async def add_admin_club_member(club_id: str,
                                user_id: int,
                                role: str = "MEMBER",
                                db: Session = Depends(get_db),
                                current_user: dict = Depends(get_admin_user)):
    """관리자용 클럽 멤버 추가"""
    try:
        from models import Club, ClubMembership, ClubRole, User
        from schemas import MembershipStatus
        from datetime import datetime

        # 클럽 조회
        club = db.query(Club).filter(Club.display_id == club_id).first()
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(Club.id == club_id_int).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None

        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다")

        # 사용자 조회
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다")

        # 이미 멤버인지 확인
        existing_membership = db.query(ClubMembership).filter(ClubMembership.club_id == club.id,
                                                              ClubMembership.user_id == user_id).first()

        if existing_membership:
            if existing_membership.status == MembershipStatus.ACTIVE:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 해당 클럽의 멤버입니다")
            else:
                # 승인되지 않은 상태라면 승인 상태로 변경
                existing_membership.status = MembershipStatus.ACTIVE
                existing_membership.role = ClubRole[role] if role in ['LEADER', 'MANAGER', 'MEMBER'
                                                                      ] else ClubRole.MEMBER
                existing_membership.updated_at = get_kst_now()
                db.commit()

                return {
                    "message": "기존 멤버십이 승인되었습니다",
                    "membership_id": existing_membership.id,
                    "user_id": user_id,
                    "role": existing_membership.role.value,
                    "status": existing_membership.status.value
                }

        # 새 멤버십 생성
        membership = ClubMembership(
            club_id=club.id,
            user_id=user_id,
            role=ClubRole[role] if role in ['LEADER', 'MANAGER', 'MEMBER'] else ClubRole.MEMBER,
            status=MembershipStatus.ACTIVE,  # 관리자가 추가하므로 바로 승인
            created_at=get_kst_now(),
            updated_at=get_kst_now())

        db.add(membership)
        db.commit()

        return {
            "message": "멤버가 성공적으로 추가되었습니다",
            "membership_id": membership.id,
            "user_id": user_id,
            "role": membership.role.value,
            "status": membership.status.value
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 클럽 멤버 추가 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"멤버 추가 중 오류가 발생했습니다: {str(e)}")


@router.put("/clubs/{club_id}/members/{user_id}/role")
async def update_admin_club_member_role(club_id: str,
                                        user_id: int,
                                        role_data: dict,
                                        db: Session = Depends(get_db),
                                        current_user: dict = Depends(get_admin_user)):
    """관리자용 클럽 멤버 역할 변경"""
    try:
        from models import Club, ClubMembership, ClubRole, User
        from datetime import datetime, timezone

        # 클럽 조회 (display_id 또는 id로)
        club = db.query(Club).filter(Club.display_id == club_id).first()
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(Club.id == club_id_int).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None

        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다")

        # 대상 멤버 조회
        target_membership = db.query(ClubMembership).filter(ClubMembership.club_id == club.id,
                                                            ClubMembership.user_id == user_id).first()

        if not target_membership:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="해당 사용자가 클럽 멤버가 아닙니다")

        # 역할 변경
        new_role = role_data.get("new_role") or role_data.get("role")
        if not new_role:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="역할을 지정해주세요")

        # 유효한 역할인지 확인
        if new_role not in ['MEMBER', 'MANAGER', 'LEADER']:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효하지 않은 역할입니다")

        target_membership.role = ClubRole[new_role]
        target_membership.updated_at = get_kst_now()
        db.commit()

        return {"message": f"멤버 역할이 {new_role}로 변경되었습니다", "success": True}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"관리자 멤버 역할 변경 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"멤버 역할 변경 중 오류가 발생했습니다: {str(e)}")


@router.delete("/clubs/{club_id}/members/{user_id}")
async def remove_admin_club_member(club_id: str,
                                   user_id: int,
                                   db: Session = Depends(get_db),
                                   current_user: dict = Depends(get_admin_user)):
    """관리자용 클럽 멤버 내보내기"""
    try:
        from models import Club, ClubMembership, User

        # 클럽 조회 (display_id 또는 id로)
        club = db.query(Club).filter(Club.display_id == club_id).first()
        if not club:
            # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(Club.id == club_id_int).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None

        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다")

        # 대상 멤버 조회
        target_membership = db.query(ClubMembership).filter(ClubMembership.club_id == club.id,
                                                            ClubMembership.user_id == user_id).first()

        if not target_membership:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="해당 사용자가 클럽 멤버가 아닙니다")

        # 멤버십 삭제
        db.delete(target_membership)

        # 클럽 멤버 수 감소
        club.member_count = max(0, club.member_count - 1)
        db.commit()

        return {"message": "멤버가 성공적으로 내보내졌습니다", "success": True}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"관리자 멤버 내보내기 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"멤버 내보내기 중 오류가 발생했습니다: {str(e)}")


@router.put("/clubs/{club_id}")
async def update_admin_club(club_id: str,
                            club_data: dict,
                            db: Session = Depends(get_db),
                            current_user: dict = Depends(get_admin_user)):
    """관리자용 클럽 수정 (display_id 또는 id로 조회)"""
    try:
        from models import Club, ClubStatus, ClubRegion
        from schemas import ClubResponse

        # display_id로 먼저 조회 시도, 없으면 id로 조회
        club = db.query(Club).filter(Club.display_id == club_id, Club.deleted_at.is_(None)).first()

        if not club:
            # display_id로 찾지 못했으면 id로 조회
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(Club.id == club_id_int, Club.deleted_at.is_(None)).first()
            except ValueError:
                # 숫자로 변환 불가능한 경우
                club = None

        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다")

        # 클럽 정보 수정
        if club_data.get("name") is not None:
            club.name = club_data["name"]
        if club_data.get("type") is not None:
            club.type = club_data["type"]
        if club_data.get("description") is not None:
            club.description = club_data["description"]
        if club_data.get("sido_code") is not None and not club_data.get("sido_code"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="시도를 선택하셔야 합니다.")
        if club_data.get("sido_code") is not None and "gungu_codes" not in club_data:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="sido_code 변경 시 gungu_codes는 필수입니다.")
        if club_data.get("sido_code") is not None:
            club.sido_code = club_data["sido_code"]
        if club_data.get("member_count") is not None:
            club.member_count = club_data["member_count"]
        if club_data.get("location") is not None:
            club.location = club_data["location"]
        if club_data.get("contact_info") is not None:
            club.contact_info = club_data["contact_info"]
        if club_data.get("additional_info") is not None:
            club.additional_info = club_data["additional_info"]
        if club_data.get("application_deadline") is not None:
            club.application_deadline = club_data["application_deadline"]
        if club_data.get("status") is not None:
            club.status = ClubStatus(club_data["status"])

        if "gungu_codes" in club_data:
            target_sido_code = club_data.get("sido_code") or club.sido_code
            if not target_sido_code:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="시도를 선택하셔야 합니다.")
            gungu_codes = club_data.get("gungu_codes") or []
            unique_gungu_codes = validate_gungu_codes(db, target_sido_code, gungu_codes)
            db.query(ClubRegion).filter(ClubRegion.club_id == club.id).delete(synchronize_session=False)
            for gungu_code in unique_gungu_codes:
                db.add(ClubRegion(club_id=club.id, gungu_code=gungu_code))

        club.updated_at = get_kst_now()
        db.commit()
        db.refresh(club)

        club.gungu_codes = [
            region.gungu_code for region in db.query(ClubRegion).filter(ClubRegion.club_id == club.id).all()
        ]
        return club

    except HTTPException:
        raise
    except ValueError as e:
        db.rollback()
        logger.error(f"클럽 수정 중 값 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"잘못된 값입니다: {str(e)}")
    except Exception as e:
        db.rollback()
        logger.error(f"클럽 수정 중 오류: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 내부 오류가 발생했습니다: {str(e)}")


@router.delete("/clubs/{club_id}")
async def delete_admin_club(club_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """관리자용 클럽 삭제"""
    try:
        club = db.query(Club).filter(Club.display_id == club_id).first()
        if not club:
            try:
                club_id_int = int(club_id)
                club = db.query(Club).filter(Club.id == club_id_int).first()
            except ValueError:
                club = None

        if not club:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="클럽을 찾을 수 없습니다.")

        approved_members = db.query(ClubMembership).filter(ClubMembership.club_id == club.id,
                                                           ClubMembership.status == MembershipStatus.ACTIVE).count()

        if approved_members > 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"승인된 멤버가 {approved_members-1}명 있습니다. 모든 멤버를 제거한 후 클럽을 삭제해주세요.")

        club.deleted_at = get_kst_now()
        club.updated_at = get_kst_now()

        memberships = db.query(ClubMembership).filter(ClubMembership.club_id == club.id).all()
        for membership in memberships:
            membership.deleted_at = get_kst_now()
            membership.updated_at = get_kst_now()

        db.commit()
        logger.info(f"관리자가 클럽 삭제: {club.display_id} ({club.name})")
        return {"success": True, "message": f"클럽 '{club.name}'이 성공적으로 삭제되었습니다."}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"클럽 삭제 중 오류 발생: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"클럽 삭제 중 오류가 발생했습니다: {str(e)}")