"""
백오피스 약관 버전 관리 API (backoffice #30, SCR-075~076)

약관은 종류(서비스·개인정보처리방침·수집이용·마케팅)마다 버전 행을 쌓는다. 게시된 버전의 본문은 바꾸지 않는다 —
회원 동의 기록(terms_agreements.terms_id)이 그 본문을 가리키기 때문이다. 고치려면 새 버전(초안)을 만들어 게시한다.

상태 (컬럼 추가 없이 파생):
- 게시중   is_active
- 초안     not is_active, published_at 없음
- 지난 버전 not is_active, published_at 있음

예전 화면이 쓰는 GET/PUT /admin/terms/{type} 은 그대로 동작한다. 단 PUT 은 게시 버전을 덮어쓰지 않고
본문이 바뀌었으면 새 버전을 만들어 게시한다.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import Admin, Terms, TermsAgreement
from schemas import TermsType
from utils import audit
from utils.datetime_utils import get_kst_now

from .deps import get_admin_user

router = APIRouter(tags=["admin-terms"])

# 예전 화면의 경로 이름 → 종류
TYPE_MAPPING = {
    "service": TermsType.SERVICE,
    "privacy": TermsType.PRIVACY,
    "collection": TermsType.PRIVACY_COLLECTION,
    "privacy_collection": TermsType.PRIVACY_COLLECTION,
    "marketing": TermsType.MARKETING,
}
_DEFAULT_TITLE = {
    TermsType.SERVICE: "서비스 이용약관",
    TermsType.PRIVACY: "개인정보처리방침",
    TermsType.PRIVACY_COLLECTION: "개인정보 수집 및 이용동의",
    TermsType.MARKETING: "마케팅정보 수신동의",
}


def _now():
    return get_kst_now().replace(tzinfo=None)


def _state(t: Terms) -> str:
    if t.is_active:
        return "ACTIVE"
    return "RETIRED" if t.published_at else "DRAFT"


def _type(value: str) -> TermsType:
    try:
        return TermsType(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=[{"field": "type", "message": "약관 종류를 선택해주세요"}])


def _versions(db: Session, terms_type: TermsType) -> list[Terms]:
    """오래된 것부터 — 순서가 버전 번호다"""
    return db.query(Terms).filter(Terms.type == terms_type).order_by(Terms.created_at.asc(), Terms.id.asc()).all()


def _rows(db: Session, terms: list[Terms], *, content: bool = False) -> list[dict]:
    ids = [t.id for t in terms]
    agreed = dict(db.query(TermsAgreement.terms_id, func.count(TermsAgreement.id)).filter(TermsAgreement.terms_id.in_(ids))
                  .group_by(TermsAgreement.terms_id).all()) if ids else {}
    authors = {t.created_by for t in terms if t.created_by}
    names = dict(db.query(Admin.id, Admin.name).filter(Admin.id.in_(authors)).all()) if authors else {}
    numbers: dict[int, int] = {}
    for tt in {t.type for t in terms}:
        for i, v in enumerate(_versions(db, tt), start=1):
            numbers[v.id] = i
    out = []
    for t in terms:
        row = {
            "id": t.id,
            "type": t.type.value,
            "version": numbers.get(t.id),
            "title": t.title,
            "status": _state(t),
            "is_required": bool(t.is_required),
            "agreement_count": agreed.get(t.id, 0),
            "created_by_name": names.get(t.created_by),
            "published_at": t.published_at.isoformat() if t.published_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        }
        if content:
            row["content"] = t.content
        out.append(row)
    return out


def _get(db: Session, term_id: int) -> Terms:
    t = db.query(Terms).filter(Terms.id == term_id).first()
    if not t:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="약관을 찾을 수 없습니다")
    return t


def _check_text(title: str | None, content: str | None) -> None:
    errors = []
    if title is not None and not title.strip():
        errors.append({"field": "title", "message": "제목을 입력해주세요"})
    if content is not None and not content.replace("<p></p>", "").strip():
        errors.append({"field": "content", "message": "본문을 입력해주세요"})
    if errors:
        raise HTTPException(status_code=422, detail=errors)


def _publish(db: Session, t: Terms) -> Terms | None:
    """t 를 게시하고 같은 종류의 예전 게시 버전을 내린다. 내린 버전을 돌려준다"""
    previous = db.query(Terms).filter(Terms.type == t.type, Terms.is_active.is_(True), Terms.id != t.id).all()
    for p in previous:
        p.is_active = False
    t.is_active = True
    t.published_at = _now()
    t.updated_at = _now()
    db.commit()
    return previous[0] if previous else None


# ---- 버전 화면 (새 백오피스) ---------------------------------------------------

@router.get("/terms/versions")
async def list_versions(type: str | None = Query(None, description="SERVICE·PRIVACY·PRIVACY_COLLECTION·MARKETING"),
                        db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """종류별 버전 목록 (최신 버전 먼저). 종류를 안 주면 종류마다 게시중 버전 요약"""
    if type:
        return {"items": list(reversed(_rows(db, _versions(db, _type(type)))))}
    summary = []
    for tt in TermsType:
        versions = _versions(db, tt)
        rows = _rows(db, versions)
        active = next((r for r in rows if r["status"] == "ACTIVE"), None)
        summary.append({"type": tt.value, "default_title": _DEFAULT_TITLE.get(tt), "active": active, "version_count": len(rows),
                        "draft_count": sum(1 for r in rows if r["status"] == "DRAFT")})
    return {"items": summary}


@router.get("/terms/versions/{term_id}")
async def get_version(term_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    return _rows(db, [_get(db, term_id)], content=True)[0]


class VersionCreate(BaseModel):
    type: str
    title: str = Field(..., max_length=255)
    content: str
    is_required: bool = False


class VersionUpdate(BaseModel):
    title: str | None = Field(None, max_length=255)
    content: str | None = None
    is_required: bool | None = None


@router.post("/terms/versions", status_code=201)
async def create_version(body: VersionCreate, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """초안 만들기 — 게시 전에는 앱에 보이지 않는다"""
    tt = _type(body.type)
    _check_text(body.title, body.content)
    t = Terms(type=tt, title=body.title.strip(), content=body.content, is_active=False, is_required=body.is_required,
              published_at=None, created_by=current_user.get("id"), created_at=_now(), updated_at=_now())
    db.add(t)
    db.commit()
    db.refresh(t)
    audit.record("CREATE", "TERMS", t.id, after={"type": tt.value, "title": t.title, "is_required": t.is_required, "status": "DRAFT"})
    return _rows(db, [t], content=True)[0]


@router.put("/terms/versions/{term_id}")
async def update_version(term_id: int, body: VersionUpdate, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """초안만 고칠 수 있다. 게시됐던 버전은 본문을 바꾸지 않는다(동의 기록이 가리킨다)"""
    t = _get(db, term_id)
    if _state(t) != "DRAFT":
        raise HTTPException(status_code=409, detail="게시된 적 있는 버전은 고칠 수 없습니다. 새 버전을 만들어 게시하세요.")
    _check_text(body.title, body.content)
    before, after = {}, {}
    for field in body.model_fields_set:
        value = getattr(body, field)
        if field == "title":
            value = value.strip()
        if getattr(t, field) != value:
            before[field] = "(본문)" if field == "content" else getattr(t, field)
            after[field] = "(본문 변경)" if field == "content" else value
            setattr(t, field, value)
    t.updated_at = _now()
    db.commit()
    db.refresh(t)
    if after:
        audit.record("UPDATE", "TERMS", t.id, before=before, after=after)
    return _rows(db, [t], content=True)[0]


@router.post("/terms/versions/{term_id}/publish")
async def publish_version(term_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """게시 — 같은 종류의 지금 버전은 지난 버전이 된다. 지난 버전을 다시 게시(되돌리기)할 수도 있다"""
    t = _get(db, term_id)
    if t.is_active:
        raise HTTPException(status_code=409, detail="이미 게시중인 버전입니다")
    was = _state(t)
    previous = _publish(db, t)
    audit.record("STATUS_CHANGE", "TERMS", t.id, before={"status": was, "replaces": previous.id if previous else None},
                 after={"status": "ACTIVE"}, meta={"type": t.type.value})
    return _rows(db, [t], content=True)[0]


@router.delete("/terms/versions/{term_id}")
async def delete_version(term_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """초안만 지울 수 있다"""
    t = _get(db, term_id)
    if _state(t) != "DRAFT":
        raise HTTPException(status_code=409, detail="게시된 적 있는 버전은 지울 수 없습니다")
    snap = {"type": t.type.value, "title": t.title}
    db.delete(t)
    db.commit()
    audit.record("DELETE", "TERMS", term_id, before=snap)
    return {"message": "초안을 삭제했습니다", "success": True}


# ---- 예전 화면 호환 ---------------------------------------------------------

@router.get("/terms")
async def get_admin_terms(db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """(예전 화면) 최근 약관 20개"""
    terms = db.query(Terms).order_by(Terms.created_at.desc(), Terms.id.desc()).limit(20).all()
    return {"data": [{**r, "is_active": r["status"] == "ACTIVE"} for r in _rows(db, terms)], "total": len(terms)}


@router.get("/terms/{terms_type}")
async def get_terms(terms_type: str, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """(예전 화면) 종류별 게시중 약관. 없으면 기본 문구를 저장하지 않고 돌려준다 (예전에는 조회만 해도 행을 만들었다)"""
    if terms_type not in TYPE_MAPPING:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효하지 않은 약관 타입입니다")
    tt = TYPE_MAPPING[terms_type]
    t = db.query(Terms).filter(Terms.type == tt, Terms.is_active.is_(True)).order_by(Terms.published_at.desc()).first()
    if not t:
        return {"title": _DEFAULT_TITLE[tt], "content": "<p>약관 내용을 입력해주세요.</p>", "updated_at": None}
    return {"title": t.title, "content": t.content, "updated_at": t.updated_at.isoformat() if t.updated_at else None}


@router.put("/terms/{terms_type}")
async def update_terms(terms_type: str, terms_data: dict, db: Session = Depends(get_db), current_user: dict = Depends(get_admin_user)):
    """(예전 화면) 종류별 저장. 바뀌었으면 새 버전을 만들어 게시한다 — 게시 버전 본문을 덮어쓰지 않는다"""
    if terms_type not in TYPE_MAPPING:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효하지 않은 약관 타입입니다")
    if "title" not in terms_data or "content" not in terms_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="제목과 내용은 필수입니다")
    tt = TYPE_MAPPING[terms_type]
    title, content = str(terms_data["title"]).strip(), str(terms_data["content"])
    _check_text(title, content)
    current = db.query(Terms).filter(Terms.type == tt, Terms.is_active.is_(True)).order_by(Terms.published_at.desc()).first()
    if current and current.title == title and current.content == content:
        return {"message": "바뀐 내용이 없습니다", "success": True}
    t = Terms(type=tt, title=title, content=content, is_active=False, is_required=bool(current.is_required) if current else False,
              created_by=current_user.get("id"), created_at=_now(), updated_at=_now())
    db.add(t)
    db.flush()
    previous = _publish(db, t)
    audit.record("STATUS_CHANGE", "TERMS", t.id, before={"status": "DRAFT", "replaces": previous.id if previous else None},
                 after={"status": "ACTIVE"}, meta={"type": tt.value, "via": "legacy"})
    return {"message": "약관이 새 버전으로 게시되었습니다", "success": True, "id": t.id}
