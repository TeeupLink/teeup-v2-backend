"""
FAQ 클라이언트 API (조회용)
- 관리자 FAQ API는 routers/admin/faq.py
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_
import logging

from database import get_db
from models import FAQ, FAQCategory
from schemas import FAQResponse, FAQPageResponse, FAQCategoryResponse

logger = logging.getLogger(__name__)

client_router = APIRouter(prefix="", tags=["faq"])


def _to_faq_response(faq: FAQ) -> FAQResponse:
    """FAQ 모델을 응답 스키마로 변환"""
    return FAQResponse(
        id=faq.id,
        question=faq.question,
        answer=faq.answer,
        category_id=faq.category_id,
        category_title=getattr(getattr(faq, "category", None), "title", None) if faq.category else None,
        order=faq.order,
        is_active=faq.is_active,
        is_deleted=faq.is_deleted,
        view_count=faq.view_count or 0,
        created_at=faq.created_at,
        updated_at=faq.updated_at,
        deleted_at=faq.deleted_at,
    )


@client_router.get("/faq", response_model=FAQPageResponse)
async def client_faq_list_api(
    page: int = Query(1, ge=1),
    limit: int = Query(5, ge=1, le=100),
    search: str | None = None,
    category_id: str | None = None,
    db: Session = Depends(get_db),
):
    """FAQ 목록 조회 (클라이언트 - 공개만)"""
    q = db.query(FAQ).outerjoin(FAQCategory, FAQ.category_id == FAQCategory.id)
    q = q.filter(FAQ.is_deleted == False, FAQ.is_active == True)

    if search:
        q = q.filter(or_(FAQ.question.contains(search), FAQ.answer.contains(search)))

    if category_id and str(category_id).strip():
        try:
            cid = int(category_id)
            q = q.filter(FAQ.category_id == cid)
        except ValueError:
            pass

    q = q.order_by(FAQ.order.asc(), FAQ.created_at.desc())
    total = q.count()

    # 페이지 보정
    if page < 1:
        page = 1
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    if total_pages > 0 and page > total_pages:
        page = total_pages

    items = q.offset((page - 1) * limit).limit(limit).all()
    dto_items = [_to_faq_response(f) for f in items]

    return FAQPageResponse(
        items=dto_items,
        total=total,
        page=page,
        limit=limit,
        total_pages=total_pages
    )

@client_router.get("/faq/{faq_id}", response_model=FAQResponse)
async def client_faq_detail_api(
    faq_id: int, db: Session = Depends(get_db)
):
    """FAQ 상세 조회 (클라이언트 - 공개만)"""
    faq = db.query(FAQ).filter(
        FAQ.id == faq_id, FAQ.is_deleted == False, FAQ.is_active == True
    ).first()
    if not faq:
        raise HTTPException(status_code=404, detail="FAQ를 찾을 수 없습니다.")
    
    # 조회수 증가
    faq.view_count = (faq.view_count or 0) + 1
    db.commit()
    
    return _to_faq_response(faq)

@client_router.get("/faq-categories", response_model=list[FAQCategoryResponse])
async def client_faq_categories_api(
    db: Session = Depends(get_db)
):
    """FAQ 카테고리 목록 조회 (클라이언트 - 활성만)"""
    cats = (
        db.query(FAQCategory)
        .filter(FAQCategory.is_active == True)
        .order_by(FAQCategory.order.asc())
        .all()
    )
    return [
        FAQCategoryResponse(
            id=c.id, title=c.title, order=c.order, is_active=c.is_active,
            created_at=c.created_at, updated_at=c.updated_at
        )
        for c in cats
    ]

