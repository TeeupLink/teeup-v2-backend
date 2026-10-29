"""
백오피스 FAQ 관리 API
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
import logging

from database import get_db
from models import FAQ, FAQCategory
from schemas import (
    FAQResponse, FAQPageResponse, FAQCategoryResponse,
    FAQCreate, FAQUpdate, FAQCategoryCreate, FAQCategoryUpdate,
    MessageResponse,
)
from utils import audit
from .deps import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-faq"])


def _to_faq_response(faq: FAQ) -> FAQResponse:
    return FAQResponse(
        id=faq.id, question=faq.question, answer=faq.answer,
        category_id=faq.category_id,
        category_title=getattr(getattr(faq, "category", None), "title", None) if faq.category else None,
        order=faq.order, is_active=faq.is_active, is_deleted=faq.is_deleted,
        view_count=faq.view_count or 0, created_at=faq.created_at, updated_at=faq.updated_at,
        deleted_at=faq.deleted_at,
    )


def _ensure_admin(admin_data: dict):
    if not admin_data or admin_data.get("type") != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")


@router.get("/faq-categories", response_model=list[FAQCategoryResponse])
async def admin_faq_categories_api(
    db: Session = Depends(get_db),
    admin_data: dict = Depends(get_admin_user),
):
    """FAQ 카테고리 목록 조회"""
    _ensure_admin(admin_data)
    cats = db.query(FAQCategory).order_by(FAQCategory.order.asc()).all()
    counts = dict(db.query(FAQ.category_id, func.count(FAQ.id)).filter(FAQ.is_deleted == False).group_by(FAQ.category_id).all())  # noqa: E712
    return [FAQCategoryResponse(id=c.id, title=c.title, order=c.order, is_active=c.is_active, faq_count=counts.get(c.id, 0),
                                created_at=c.created_at, updated_at=c.updated_at) for c in cats]


@router.post("/faq-categories", response_model=MessageResponse)
async def admin_faq_category_create_api(
    data: FAQCategoryCreate,
    db: Session = Depends(get_db),
    admin_data: dict = Depends(get_admin_user),
):
    """FAQ 카테고리 생성"""
    _ensure_admin(admin_data)
    title = data.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="카테고리명은 필수입니다.")
    if len(title) > 50:
        raise HTTPException(status_code=400, detail="카테고리명은 50자 이하로 입력해주세요.")
    if db.query(FAQCategory).filter(FAQCategory.title == title).first():
        raise HTTPException(status_code=400, detail="이미 존재하는 카테고리명입니다.")
    current_count = db.query(FAQCategory).count()
    adjusted_order = 1 if current_count == 0 or data.order <= 0 else (min(data.order, current_count + 1) if data.order > current_count + 1 else data.order)
    for c in db.query(FAQCategory).filter(FAQCategory.order >= adjusted_order).all():
        c.order += 1
    cat = FAQCategory(title=title, order=adjusted_order, is_active=data.is_active)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return MessageResponse(message="FAQ 카테고리가 생성되었습니다.", success=True)


@router.put("/faq-categories/{category_id}", response_model=MessageResponse)
async def admin_faq_category_update_api(
    category_id: int,
    data: FAQCategoryUpdate,
    db: Session = Depends(get_db),
    admin_data: dict = Depends(get_admin_user),
):
    """FAQ 카테고리 수정"""
    _ensure_admin(admin_data)
    cat = db.query(FAQCategory).filter(FAQCategory.id == category_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="FAQ 카테고리를 찾을 수 없습니다.")
    if data.title is not None:
        title = data.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="카테고리명은 필수입니다.")
        if len(title) > 50:
            raise HTTPException(status_code=400, detail="카테고리명은 50자 이하로 입력해주세요.")
        if db.query(FAQCategory).filter(FAQCategory.title == title, FAQCategory.id != category_id).first():
            raise HTTPException(status_code=400, detail="이미 존재하는 카테고리명입니다.")
        cat.title = title
    if data.order is not None:
        new_order, old_order = int(data.order), cat.order
        if new_order != old_order:
            if new_order > old_order:
                db.query(FAQCategory).filter(FAQCategory.order > old_order, FAQCategory.order <= new_order, FAQCategory.id != category_id).update({FAQCategory.order: FAQCategory.order - 1}, synchronize_session=False)
            else:
                db.query(FAQCategory).filter(FAQCategory.order >= new_order, FAQCategory.order < old_order, FAQCategory.id != category_id).update({FAQCategory.order: FAQCategory.order + 1}, synchronize_session=False)
            cat.order = new_order
    if data.is_active is not None:
        cat.is_active = bool(data.is_active)
    db.commit()
    return MessageResponse(message="FAQ 카테고리가 수정되었습니다.", success=True)


@router.delete("/faq-categories/{category_id}", response_model=MessageResponse)
async def admin_faq_category_delete_api(
    category_id: int,
    db: Session = Depends(get_db),
    admin_data: dict = Depends(get_admin_user),
):
    """FAQ 카테고리 삭제"""
    _ensure_admin(admin_data)
    cat = db.query(FAQCategory).filter(FAQCategory.id == category_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="FAQ 카테고리를 찾을 수 없습니다.")
    if db.query(FAQ).filter(FAQ.category_id == category_id, FAQ.is_deleted == False).count() > 0:
        raise HTTPException(status_code=400, detail="이 카테고리를 사용하는 FAQ가 있습니다.")
    deleted_order = cat.order
    db.delete(cat)
    db.query(FAQCategory).filter(FAQCategory.order > deleted_order).update({FAQCategory.order: FAQCategory.order - 1})
    db.commit()
    return MessageResponse(message="FAQ 카테고리가 삭제되었습니다.", success=True)


@router.get("/faq", response_model=FAQPageResponse)
async def admin_faq_list_api(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    search: str | None = None,
    category_id: str | None = Query(None, description="카테고리 id, none 이면 미분류"),
    is_active: bool | None = Query(None, description="공개 여부"),
    db: Session = Depends(get_db),
    admin_data: dict = Depends(get_admin_user),
):
    """FAQ 목록 조회 (카테고리 안 순서대로)"""
    _ensure_admin(admin_data)
    q = db.query(FAQ).outerjoin(FAQCategory, FAQ.category_id == FAQCategory.id).filter(FAQ.is_deleted == False)
    if search:
        q = q.filter(or_(FAQ.question.contains(search), FAQ.answer.contains(search)))
    if is_active is not None:
        q = q.filter(FAQ.is_active == is_active)
    if category_id == "none":
        q = q.filter(FAQ.category_id.is_(None))
    elif category_id and str(category_id).strip():
        try:
            q = q.filter(FAQ.category_id == int(category_id))
        except ValueError:
            pass
    q = q.order_by(FAQ.order.asc(), FAQ.created_at.desc())
    total = q.count()
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    page = max(1, min(page, total_pages) if total_pages > 0 else 1)
    items = q.offset((page - 1) * limit).limit(limit).all()
    return FAQPageResponse(items=[_to_faq_response(f) for f in items], total=total, page=page, limit=limit, total_pages=total_pages)


@router.get("/faq/{faq_id}", response_model=FAQResponse)
async def admin_faq_detail_api(
    faq_id: int,
    db: Session = Depends(get_db),
    admin_data: dict = Depends(get_admin_user),
):
    """FAQ 상세 조회"""
    _ensure_admin(admin_data)
    faq = db.query(FAQ).filter(FAQ.id == faq_id, FAQ.is_deleted == False).first()
    if not faq:
        raise HTTPException(status_code=404, detail="FAQ를 찾을 수 없습니다.")
    return _to_faq_response(faq)


@router.post("/faq", response_model=MessageResponse)
async def admin_faq_create_api(
    data: FAQCreate,
    db: Session = Depends(get_db),
    admin_data: dict = Depends(get_admin_user),
):
    """FAQ 생성"""
    _ensure_admin(admin_data)
    question, answer = data.question.strip(), data.answer.strip()
    if not question:
        raise HTTPException(status_code=400, detail="질문은 필수입니다.")
    if len(question) > 300:
        raise HTTPException(status_code=400, detail="질문은 300자 이하로 입력해주세요.")
    if not answer:
        raise HTTPException(status_code=400, detail="답변은 필수입니다.")
    category_id = data.category_id
    if category_id is not None and category_id != "":
        last = db.query(FAQ).filter(FAQ.category_id == category_id, FAQ.is_deleted == False).order_by(FAQ.order.desc()).first()
        auto_order = (last.order + 1) if last else 1
        cat_id = category_id
    else:
        last = db.query(FAQ).filter(FAQ.category_id.is_(None), FAQ.is_deleted == False).order_by(FAQ.order.desc()).first()
        auto_order = (last.order + 1) if last else 1
        cat_id = None
    faq = FAQ(question=question, answer=answer, category_id=cat_id, order=auto_order, is_active=data.is_active, created_by=admin_data["id"])
    db.add(faq)
    db.commit()
    return MessageResponse(message="FAQ가 생성되었습니다", success=True)


@router.put("/faq/{faq_id}", response_model=MessageResponse)
async def admin_faq_update_api(
    faq_id: int,
    data: FAQUpdate,
    db: Session = Depends(get_db),
    admin_data: dict = Depends(get_admin_user),
):
    """FAQ 수정"""
    _ensure_admin(admin_data)
    faq = db.query(FAQ).filter(FAQ.id == faq_id, FAQ.is_deleted == False).first()
    if not faq:
        raise HTTPException(status_code=404, detail="FAQ를 찾을 수 없습니다.")
    if data.question is not None:
        q = data.question.strip()
        if not q:
            raise HTTPException(status_code=400, detail="질문은 필수입니다.")
        if len(q) > 300:
            raise HTTPException(status_code=400, detail="질문은 300자 이하로 입력해주세요.")
        faq.question = q
    if data.answer is not None:
        a = data.answer.strip()
        if not a:
            raise HTTPException(status_code=400, detail="답변은 필수입니다.")
        faq.answer = a
    if "category_id" in data.model_fields_set:
        # null 이면 미분류로 옮긴다 (예전에는 null 을 보내도 무시됐다)
        faq.category_id = data.category_id or None
    if data.order is not None:
        faq.order = int(data.order)
    if data.is_active is not None:
        faq.is_active = bool(data.is_active)
    db.commit()
    return MessageResponse(message="FAQ가 수정되었습니다.", success=True)


@router.delete("/faq/{faq_id}", response_model=MessageResponse)
async def admin_faq_delete_api(
    faq_id: int,
    db: Session = Depends(get_db),
    admin_data: dict = Depends(get_admin_user),
):
    """FAQ 삭제"""
    _ensure_admin(admin_data)
    faq = db.query(FAQ).filter(FAQ.id == faq_id, FAQ.is_deleted == False).first()
    if not faq:
        raise HTTPException(status_code=404, detail="FAQ를 찾을 수 없습니다.")
    faq.is_deleted = True
    faq.deleted_at = datetime.now()
    db.commit()
    audit.record("DELETE", "FAQ", faq_id, before={"question": faq.question, "category_id": faq.category_id, "is_active": faq.is_active})
    return MessageResponse(message="FAQ가 삭제되었습니다.", success=True)


class FAQReorder(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=500, description="새 순서대로 나열한 FAQ id (같은 카테고리)")


@router.put("/faq-order", response_model=MessageResponse)
async def admin_faq_reorder_api(
    data: FAQReorder,
    db: Session = Depends(get_db),
    admin_data: dict = Depends(get_admin_user),
):
    """한 카테고리 안의 FAQ 순서를 한 번에 바꾼다 (위·아래 이동을 요청 두 번으로 나누지 않도록)"""
    _ensure_admin(admin_data)
    if len(set(data.ids)) != len(data.ids):
        raise HTTPException(status_code=422, detail="같은 FAQ 가 두 번 들어 있습니다.")
    faqs = {f.id: f for f in db.query(FAQ).filter(FAQ.id.in_(data.ids), FAQ.is_deleted == False).all()}  # noqa: E712
    if len(faqs) != len(data.ids):
        raise HTTPException(status_code=404, detail="FAQ를 찾을 수 없습니다.")
    if len({f.category_id for f in faqs.values()}) > 1:
        raise HTTPException(status_code=422, detail="같은 카테고리의 FAQ만 순서를 바꿀 수 있습니다.")
    before = {fid: faqs[fid].order for fid in data.ids}
    for i, fid in enumerate(data.ids, start=1):
        faqs[fid].order = i
    db.commit()
    audit.record("UPDATE", "FAQ_ORDER", next(iter(faqs.values())).category_id, before={"order": before},
                 after={"order": {fid: i for i, fid in enumerate(data.ids, start=1)}})
    return MessageResponse(message="FAQ 순서를 바꿨습니다.", success=True)


@router.patch("/faq/{faq_id}/toggle-active", response_model=MessageResponse)
async def admin_faq_toggle_active_api(
    faq_id: int,
    db: Session = Depends(get_db),
    admin_data: dict = Depends(get_admin_user),
):
    """FAQ 공개/비공개 토글"""
    _ensure_admin(admin_data)
    faq = db.query(FAQ).filter(FAQ.id == faq_id, FAQ.is_deleted == False).first()
    if not faq:
        raise HTTPException(status_code=404, detail="FAQ를 찾을 수 없습니다.")
    faq.is_active = not faq.is_active
    db.commit()
    return MessageResponse(message=f"FAQ가 {'공개' if faq.is_active else '비공개'}로 변경되었습니다.", success=True)
