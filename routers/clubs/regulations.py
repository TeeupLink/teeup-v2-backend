# 클럽 규정 관리 API들
from fastapi import APIRouter, Depends, HTTPException, status, Query
from utils.content_filter import reject_banned_content
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime
from utils.datetime_utils import get_kst_now

from database import get_db
from models import User, Club, ClubMembership, RegulationCategory, Regulation
from schemas import ClubRole, RegulationStatus
from schemas import (
    RegulationCategoryCreate, RegulationCategoryUpdate, RegulationCategoryResponse,
    RegulationCreate, RegulationUpdate, RegulationResponse, RegulationListResponse,
    ClubRegulationsResponse, MessageResponse,
    ClubRegulationResponse, ClubRegulationCreate, ClubRegulationUpdate, PaginatedResponse
)
from routers.auth import get_current_user, get_current_active_user

router = APIRouter(prefix="/clubs", tags=["클럽 규정"])

# ===== 규정 카테고리 관리 =====

@router.post("/{club_id}/regulations/categories", response_model=RegulationCategoryResponse, dependencies=[Depends(reject_banned_content)])
async def create_regulation_category(
    club_id: str,
    category_data: RegulationCategoryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """규정 카테고리 생성 (리더/매니저만 가능)"""
    try:
        # 클럽 조회 (display_id로 조회)
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
        
        # 클럽 멤버 권한 확인
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,  # 실제 클럽 ID 사용
            ClubMembership.user_id == current_user.id,
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER])
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 리더나 매니저만 규정 카테고리를 생성할 수 있습니다."
            )
        
        # order 결정: 미지정이거나 중복 시 자동 부여
        order = category_data.order
        existing_same_order = db.query(RegulationCategory).filter(
            RegulationCategory.club_id == club.id,
            RegulationCategory.order == order
        ).first()
        if existing_same_order:
            max_order = db.query(RegulationCategory).filter(
                RegulationCategory.club_id == club.id
            ).count()
            order = max_order

        # 카테고리 생성
        category = RegulationCategory(
            club_id=club.id,  # 실제 클럽 ID 사용
            name=category_data.name,
            order=order
        )
        
        db.add(category)
        db.commit()
        db.refresh(category)
        
        return category
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.get("/{club_id}/regulations/categories", response_model=list[RegulationCategoryResponse])
async def get_regulation_categories(
    club_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """규정 카테고리 목록 조회"""
    try:
        # 클럽 조회 (display_id로 조회)
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
            ClubMembership.club_id == club.id,  # 실제 클럽 ID 사용
            ClubMembership.user_id == current_user.id
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 멤버만 규정을 조회할 수 있습니다."
            )
        
        # 카테고리 목록 조회 (순서대로)
        categories = db.query(RegulationCategory).filter(
            RegulationCategory.club_id == club.id  # 실제 클럽 ID 사용
        ).order_by(RegulationCategory.order).all()
        
        # 각 카테고리의 규정들도 함께 조회
        result = []
        for category in categories:
            regulations = db.query(Regulation).filter(
                Regulation.category_id == category.id
            ).order_by(Regulation.created_at).all()
            
            category_dict = {
                "id": category.id,                "club_id": category.club_id,
                "name": category.name,
                "order": category.order,
                "created_at": category.created_at,
                "updated_at": category.updated_at,
                "regulations": regulations
            }
            result.append(category_dict)
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

def _resolve_club(db: Session, club_id: str):
    """club_id(display_id 또는 숫자)로 Club 조회"""
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
            pass
    return club


@router.put("/{club_id}/regulations/categories/{category_id}", response_model=RegulationCategoryResponse, dependencies=[Depends(reject_banned_content)])
async def update_regulation_category(
    club_id: str,
    category_id: int,
    category_data: RegulationCategoryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """규정 카테고리 수정 (리더/매니저만 가능)"""
    try:
        # 클럽 조회 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()

        # if not club:
        #     # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
        #     try:
        #         club_id_int = int(club_id)
        #         club = db.query(Club).filter(
        #             Club.id == club_id_int,
        #             Club.deleted_at.is_(None)
        #         ).first()
        #     except ValueError:
        #         club = None

        # if not club:
        #     raise HTTPException(
        #         status_code=status.HTTP_404_NOT_FOUND,
        #         detail="클럽을 찾을 수 없습니다."
        #     )

        # 클럽 멤버 권한 확인
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == current_user.id,
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER])
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 리더나 매니저만 규정 카테고리를 수정할 수 있습니다."
            )
        
        # 카테고리 조회
        category = db.query(RegulationCategory).filter(
            RegulationCategory.id == category_id,
            RegulationCategory.club_id == club.id
        ).first()
        
        if not category:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="규정 카테고리를 찾을 수 없습니다."
            )
        
        # 순서 변경 시 중복 확인
        if category_data.order is not None and category_data.order != category.order:
            existing_category = db.query(RegulationCategory).filter(
                RegulationCategory.club_id == club.id,
                RegulationCategory.order == category_data.order,
                RegulationCategory.id != category_id
            ).first()
            
            if existing_category:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="이미 해당 순서를 사용하는 카테고리가 있습니다."
                )
        
        # 카테고리 수정
        if category_data.name is not None:
            category.name = category_data.name
        if category_data.order is not None:
            category.order = category_data.order
        
        category.updated_at = get_kst_now()
        db.commit()
        db.refresh(category)
        
        return category
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.delete("/{club_id}/regulations/categories/{category_id}", response_model=MessageResponse)
async def delete_regulation_category(
    club_id: str,
    category_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """규정 카테고리 삭제 (리더/매니저만 가능)"""
    try:
        # 클럽 조회 (display_id로 먼저 조회)
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()

        # if not club:
        #     # display_id로 찾지 못했으면 숫자로 변환 가능한지 확인 후 id로 조회
        #     try:
        #         club_id_int = int(club_id)
        #         club = db.query(Club).filter(
        #             Club.id == club_id_int,
        #             Club.deleted_at.is_(None)
        #         ).first()
        #     except ValueError:
        #         club = None

        # if not club:
        #     raise HTTPException(
        #         status_code=status.HTTP_404_NOT_FOUND,
        #         detail="클럽을 찾을 수 없습니다."
        #     )

        # 클럽 멤버 권한 확인
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,
            ClubMembership.user_id == current_user.id,
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER])
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 리더나 매니저만 규정 카테고리를 삭제할 수 있습니다."
            )
        
        # 카테고리 조회
        category = db.query(RegulationCategory).filter(
            RegulationCategory.id == category_id,
            RegulationCategory.club_id == club.id
        ).first()
        
        if not category:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="규정 카테고리를 찾을 수 없습니다."
            )
        
        # 카테고리 삭제 (관련 규정들도 함께 삭제됨)
        db.delete(category)
        db.commit()
        
        return {
            "message": "규정 카테고리가 삭제되었습니다.",
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

# ===== 규정 관리 =====

# ===== 전체 규정 조회 =====

@router.get("/{club_id}/regulations", response_model=ClubRegulationsResponse)
async def get_club_regulations(
    club_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽 전체 규정 조회"""
    print(f"🔍 get_club_regulations 호출됨: club_id={club_id}, user={current_user.id}")
    try:
        # 클럽 조회 (display_id로 조회)
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
            ClubMembership.club_id == club.id,  # 실제 클럽 ID 사용
            ClubMembership.user_id == current_user.id
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 멤버만 규정을 조회할 수 있습니다."
            )
        
        # 카테고리 목록 조회 (순서대로)
        categories = db.query(RegulationCategory).filter(
            RegulationCategory.club_id == club.id  # 실제 클럽 ID 사용
        ).order_by(RegulationCategory.order).all()
        
        # 각 카테고리의 규정들도 함께 조회
        result_categories = []
        for category in categories:
            regulations = db.query(Regulation).filter(
                Regulation.category_id == category.id
            ).order_by(Regulation.created_at).all()
            
            # regulations를 딕셔너리로 변환
            regulations_list = []
            for regulation in regulations:
                regulations_list.append({
                    "id": regulation.id,
                    "club_id": regulation.club_id,
                    "category_id": regulation.category_id,
                    "title": regulation.title,
                    "content": regulation.content,
                    "status": regulation.status,
                    "created_by": regulation.created_by,
                    "created_by_name": regulation.created_by_name,
                    "published_at": regulation.published_at,
                    "created_at": regulation.created_at,
                    "updated_at": regulation.updated_at
                })
            
            category_dict = {
                "id": category.id,                "club_id": category.club_id,
                "name": category.name,
                "order": category.order,
                "created_at": category.created_at,
                "updated_at": category.updated_at,
                "regulations": regulations_list
            }
            result_categories.append(category_dict)
        
        return {
            "categories": result_categories,
            "total_categories": len(result_categories)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        error_msg = f"서버 내부 오류가 발생했습니다: {str(e)}"
        print(f"❌ 규정 조회 에러: {error_msg}")
        print(f"에러 타입: {type(e)}")
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_msg
        )

@router.get("/{club_id}/regulations/list", response_model=PaginatedResponse[ClubRegulationResponse])
async def get_club_regulations_list(
    club_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽 규정 목록 조회 (새로운 API)"""
    try:
        print(f"=== 규정 목록 조회 시작 ===")
        print(f"요청된 club_id: {club_id}")
        print(f"현재 사용자: {current_user}")
        
        # 클럽 조회 (display_id로 조회)
        print(f"1. display_id로 클럽 조회 시도: {club_id}")
        club = db.query(Club).filter(
            Club.display_id == club_id,
            Club.deleted_at.is_(None)
        ).first()
        
        if club:
            print(f"✅ display_id로 클럽 찾음: {club.id} (display_id: {club.display_id})")
        else:
            print(f"❌ display_id로 클럽을 찾지 못함, id로 재시도")
            # display_id로 찾지 못했으면 id로 조회
            club = db.query(Club).filter(
                Club.id == club_id,
                Club.deleted_at.is_(None)
            ).first()
            
            if club:
                print(f"✅ id로 클럽 찾음: {club.id} (display_id: {club.display_id})")
            else:
                print(f"❌ id로도 클럽을 찾지 못함")
        
        if not club:
            print(f"❌ 클럽을 찾을 수 없음 - 404 에러 반환")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="클럽을 찾을 수 없습니다."
            )
        
        # 클럽 멤버인지 확인
        user_id = current_user.id
        print(f"2. 멤버십 확인 시작")
        print(f"   클럽 ID: {club.id}")
        print(f"   사용자 ID: {user_id}")
        print(f"   사용자 정보: {current_user}")
        
        # 모든 멤버십 조회 (디버깅용)
        all_memberships = db.query(ClubMembership).filter(
            ClubMembership.user_id == user_id
        ).all()
        print(f"   사용자의 모든 멤버십: {[(m.club_id, m.role.value if m.role else None) for m in all_memberships]}")
        
        # 해당 클럽의 모든 멤버 조회 (디버깅용)
        all_club_members = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id
        ).all()
        print(f"   클럽의 모든 멤버: {[(m.user_id, m.role.value if m.role else None) for m in all_club_members]}")
        
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,  # 실제 클럽 ID 사용
            ClubMembership.user_id == user_id
        ).first()
        
        print(f"   멤버십 조회 결과: {membership}")
        if membership:
            print(f"   멤버십 상세: club_id={membership.club_id}, user_id={membership.user_id}, role={membership.role.value if membership.role else None}")
        
        if not membership:
            print(f"❌ 멤버십을 찾을 수 없음 - 403 에러 반환")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 멤버만 규정을 조회할 수 있습니다."
            )
        
        print(f"✅ 멤버십 확인 완료")
        
        # 규정 목록 조회 (실제 데이터 조회)
        print(f"3. 규정 목록 조회 시작")
        
        # Regulation을 카테고리와 조인하여 조회
        # 모든 규정을 조회한 후 페이지네이션 적용
        
        # 먼저 Regulation 조회 및 변환
        all_regulations_query = db.query(Regulation).join(
            RegulationCategory
        ).filter(
            RegulationCategory.club_id == club.id
        ).order_by(
            RegulationCategory.order,
            Regulation.created_at
        ).all()
        
        # Regulation을 ClubRegulationResponse 형태로 변환
        all_regulations = []
        for regulation in all_regulations_query:
            # 카테고리 정보 가져오기
            category = regulation.category
            
            # ClubRegulationResponse 형태로 변환
            regulation_data = {
                "id": regulation.id,
                "club_id": regulation.club_id,
                "category_id": regulation.category_id,
                "title": regulation.title,
                "content": regulation.content,
                "status": regulation.status,
                "created_by": regulation.created_by,
                "created_by_name": regulation.created_by_name,
                "published_at": regulation.published_at,
                "created_at": regulation.created_at,
                "updated_at": regulation.updated_at,
                # 추가 필드 (스키마에는 없지만 프론트엔드에서 사용)
                "category_name": category.name,
                # 정렬을 위한 필드
                "_sort_order": category.order * 1000  # 카테고리 순서
            }
            all_regulations.append(regulation_data)
        
        # 정렬
        all_regulations.sort(key=lambda x: x.get("_sort_order", 0))
        
        # 전체 개수
        total_count = len(all_regulations)
        print(f"   전체 규정 개수: {total_count}")
        
        # 페이지네이션 적용
        offset = (page - 1) * limit
        paginated_regulations = all_regulations[offset:offset + limit]
        
        # _sort_order 필드 제거 및 날짜 형식 변환
        regulations = []
        for reg in paginated_regulations:
            reg.pop("_sort_order", None)  # 정렬용 필드 제거
            # 날짜 형식 변환
            if isinstance(reg.get("created_at"), datetime):
                reg["created_at"] = reg["created_at"].isoformat()
            if isinstance(reg.get("updated_at"), datetime):
                reg["updated_at"] = reg["updated_at"].isoformat()
            regulations.append(reg)
        
        print(f"   조회된 조항 수: {len(regulations)} (페이지: {page}, limit: {limit})")
        
        print(f"=== 규정 목록 조회 완료 ===")
        return {
            "data": regulations,
            "total": total_count,
            "page": page,
            "limit": limit,
            "total_pages": (total_count + limit - 1) // limit
        }
        
    except HTTPException as he:
        print(f"❌ HTTPException 발생: {he.status_code} - {he.detail}")
        raise
    except Exception as e:
        print(f"❌ 예상치 못한 오류 발생: {type(e).__name__}: {str(e)}")
        import traceback
        print(f"스택 트레이스: {traceback.format_exc()}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.post("/{club_id}/regulations", response_model=ClubRegulationResponse, dependencies=[Depends(reject_banned_content)])
async def create_club_regulation(
    club_id: str,
    regulation_data: ClubRegulationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽 규정 생성 (리더/매니저만 가능)"""
    try:
        # 클럽 조회 (display_id로 조회)
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
        
        # 클럽 멤버 권한 확인 (리더/매니저만 가능)
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,  # 실제 클럽 ID 사용
            ClubMembership.user_id == current_user.id,
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER])
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 리더나 매니저만 규정을 생성할 수 있습니다."
            )
        
        # 카테고리 확인
        category = db.query(RegulationCategory).filter(
            RegulationCategory.id == regulation_data.category_id,
            RegulationCategory.club_id == club.id
        ).first()
        
        if not category:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="규정 카테고리를 찾을 수 없습니다."
            )
        
        # 작성자 이름 스냅샷 생성
        creator_name = current_user.realname if current_user.realname else (current_user.nickname if current_user.nickname else "시스템")
        
        # 실제 규정 데이터베이스에 저장
        # Regulation 테이블에 저장
        new_regulation = Regulation(
            club_id=club.id,
            category_id=regulation_data.category_id,
            title=regulation_data.title,
            content=regulation_data.content,
            status=regulation_data.status,
            created_by=current_user.id,
            created_by_name=creator_name,
            published_at=datetime.now() if regulation_data.status == "ACTIVE" else None
        )
        
        db.add(new_regulation)
        db.commit()
        db.refresh(new_regulation)
        
        regulation = ClubRegulationResponse(
            id=new_regulation.id,
            club_id=new_regulation.club_id,
            category_id=new_regulation.category_id,
            title=new_regulation.title,
            content=new_regulation.content,
            status=new_regulation.status,
            created_by=new_regulation.created_by,
            created_by_name=new_regulation.created_by_name,
            published_at=new_regulation.published_at,
            created_at=new_regulation.created_at,
            updated_at=new_regulation.updated_at,
            category_name=category.name
        )
        
        return regulation
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"규정 생성 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.get("/{club_id}/regulations/{regulation_id}", response_model=ClubRegulationResponse)
async def get_club_regulation(
    club_id: str,
    regulation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽 규정 상세 조회"""
    try:
        # 클럽 조회 (display_id로 조회)
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
            ClubMembership.club_id == club.id,  # 실제 클럽 ID 사용
            ClubMembership.user_id == current_user.id
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 멤버만 규정을 조회할 수 있습니다."
            )

        # 규정 조회 (클럽 소속 확인)
        regulation = db.query(Regulation).filter(
            Regulation.id == regulation_id,
            Regulation.club_id == club.id,
        ).first()

        if not regulation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="규정을 찾을 수 없습니다."
            )

        category = regulation.category
        return ClubRegulationResponse(
            id=regulation.id,
            club_id=regulation.club_id,
            category_id=regulation.category_id,
            title=regulation.title,
            content=regulation.content,
            status=regulation.status,
            created_by=regulation.created_by,
            created_by_name=regulation.created_by_name,
            published_at=regulation.published_at,
            created_at=regulation.created_at,
            updated_at=regulation.updated_at,
            category_name=category.name if category else None,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"규정 조회 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.put("/{club_id}/regulations/{regulation_id}", response_model=ClubRegulationResponse, dependencies=[Depends(reject_banned_content)])
async def update_club_regulation(
    club_id: str,
    regulation_id: int,
    regulation_data: ClubRegulationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽 규정 수정 (리더/매니저만 가능)"""
    try:
        # 클럽 조회 (display_id로 조회)
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
        
        # 클럽 멤버 권한 확인 (리더/매니저만 가능)
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,  # 실제 클럽 ID 사용
            ClubMembership.user_id == current_user.id,
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER])
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 리더나 매니저만 규정을 수정할 수 있습니다."
            )
        
        # 실제 규정 데이터베이스에서 조회 및 수정
        existing_regulation = db.query(Regulation).filter(
            Regulation.id == regulation_id,
            Regulation.club_id == club.id
        ).first()
        
        if not existing_regulation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="규정을 찾을 수 없습니다."
            )
        
        # 카테고리 확인 (변경 시)
        if regulation_data.category_id is not None:
            category = db.query(RegulationCategory).filter(
                RegulationCategory.id == regulation_data.category_id,
                RegulationCategory.club_id == club.id
            ).first()
            if not category:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="규정 카테고리를 찾을 수 없습니다."
                )
            existing_regulation.category_id = regulation_data.category_id
        
        # 규정 데이터 업데이트
        if regulation_data.title:
            existing_regulation.title = regulation_data.title
        if regulation_data.content:
            existing_regulation.content = regulation_data.content
        if regulation_data.status:
            existing_regulation.status = regulation_data.status
            if regulation_data.status == "ACTIVE" and not existing_regulation.published_at:
                existing_regulation.published_at = datetime.now()
        
        # updated_at 자동 업데이트
        existing_regulation.updated_at = get_kst_now()
        
        db.commit()
        db.refresh(existing_regulation)
        
        # 카테고리 정보 조회
        category = db.query(RegulationCategory).filter(
            RegulationCategory.id == existing_regulation.category_id
        ).first()
        
        regulation = ClubRegulationResponse(
            id=existing_regulation.id,
            club_id=existing_regulation.club_id,
            category_id=existing_regulation.category_id,
            title=existing_regulation.title,
            content=existing_regulation.content,
            status=existing_regulation.status,
            created_by=existing_regulation.created_by,
            created_by_name=existing_regulation.created_by_name,
            published_at=existing_regulation.published_at,
            created_at=existing_regulation.created_at,
            updated_at=existing_regulation.updated_at,
            category_name=category.name if category else None
        )
        
        return regulation
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"규정 수정 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )

@router.delete("/{club_id}/regulations/{regulation_id}")
async def delete_club_regulation(
    club_id: str,
    regulation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """클럽 규정 삭제 (리더/매니저만 가능)"""
    try:
        # 클럽 조회 (display_id로 조회)
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
        
        # 클럽 멤버 권한 확인 (리더/매니저만 가능)
        membership = db.query(ClubMembership).filter(
            ClubMembership.club_id == club.id,  # 실제 클럽 ID 사용
            ClubMembership.user_id == current_user.id,
            ClubMembership.role.in_([ClubRole.LEADER, ClubRole.MANAGER])
        ).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="클럽 리더나 매니저만 규정을 삭제할 수 있습니다."
            )
        
        # 실제 규정 데이터베이스에서 조회 및 삭제
        existing_regulation = db.query(Regulation).filter(
            Regulation.id == regulation_id,
            Regulation.club_id == club.id
        ).first()
        
        if not existing_regulation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="규정을 찾을 수 없습니다."
            )
        
        # 규정 삭제
        db.delete(existing_regulation)
        db.commit()
        
        return {"message": "규정이 성공적으로 삭제되었습니다."}
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"규정 삭제 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류가 발생했습니다."
        )
