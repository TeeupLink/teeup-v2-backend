"""
백오피스 공지사항 API (시스템 공지)
- content는 TipTap 등 리치 텍스트 에디터의 HTML을 그대로 저장
- 화면 표시 시 프론트에서 dangerouslySetInnerHTML 또는 v-html로 렌더링
"""
import logging
import os

import requests
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Query
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from utils import audit
from utils.datetime_utils import get_kst_now
from utils.google_oauth import get_mime_type, load_access_token
from .deps import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-notices"])


def _system_notices(db: Session):
    """시스템 공지만 (클럽 공지는 club_id 가 있고 클럽 화면에서 관리한다)"""
    from models import Notice
    return db.query(Notice).filter(Notice.club_id.is_(None))


def _get_or_404(db: Session, notice_id: int):
    notice = _system_notices(db).filter_by(id=notice_id).first()
    if not notice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공지사항을 찾을 수 없습니다")
    return notice


def _row(n, author_names: dict | None = None) -> dict:
    return {
        "id": n.id,
        "title": n.title,
        "type": n.type.value if hasattr(n.type, "value") else str(n.type),
        "is_important": bool(n.is_important),
        "is_published": bool(n.is_published),
        "view_count": n.view_count or 0,
        "author_id": n.author_id,
        "author_name": (author_names or {}).get(n.author_id),
        "published_at": n.published_at.isoformat() if n.published_at else None,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "updated_at": n.updated_at.isoformat() if n.updated_at else None,
    }


def _validate(data: dict, partial: bool) -> None:
    errors = []
    for field, label, limit in (("title", "제목", 255), ("content", "내용", None)):
        if partial and field not in data:
            continue
        value = str(data.get(field) or "").strip()
        # 빈 단락만 있는 에디터 HTML 도 비어 있는 것으로 본다
        if not value or value in ("<p></p>", "<p><br></p>"):
            errors.append({"field": field, "message": f"{label}을(를) 입력해주세요"})
        elif limit and len(value) > limit:
            errors.append({"field": field, "message": f"{label}은(는) {limit}자 이하로 입력해주세요"})
    if errors:
        raise HTTPException(status_code=422, detail=errors)


def _notify_published(db: Session, notice) -> None:
    """게시될 때 한 번 활성 회원 전체에게 알린다. 임시 저장(미게시)에는 보내지 않는다"""
    try:
        from models import User, UserStatus
        from utils.notification_service import create_notice_notification
        for (user_id,) in db.query(User.id).filter(User.status == UserStatus.ACTIVE).all():
            create_notice_notification(db=db, user_id=user_id, notice_title=notice.title, notice_id=notice.id)
    except Exception as e:
        logger.error(f"시스템 공지사항 알림 전송 실패: {str(e)}")


@router.get("/notices")
async def get_admin_notices(
        page: int = Query(1, ge=1),
        limit: int = Query(20, ge=1, le=100),
        q: str | None = Query(None, description="제목·내용 검색"),
        is_published: bool | None = Query(None),
        is_important: bool | None = Query(None),
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 시스템 공지 목록 (중요 공지 먼저, 최신순)"""
    from models import Admin, Notice
    from sqlalchemy import desc, or_

    query = _system_notices(db)
    if q:
        query = query.filter(or_(Notice.title.contains(q), Notice.content.contains(q)))
    if is_published is not None:
        query = query.filter(Notice.is_published == is_published)
    if is_important is not None:
        query = query.filter(Notice.is_important == is_important)
    total = query.count()
    rows = query.order_by(desc(Notice.is_important), desc(Notice.created_at), desc(Notice.id)).offset((page - 1) * limit).limit(limit).all()
    ids = {n.author_id for n in rows if n.author_id}
    names = dict(db.query(Admin.id, Admin.name).filter(Admin.id.in_(ids)).all()) if ids else {}
    return {"data": [_row(n, names) for n in rows], "total": total, "page": page, "limit": limit}


@router.post("/notices/upload")
async def upload_notice_file(
    file: UploadFile = File(...),
    share: str | None = Query(default=None, description="'link' 지정 시 anyone-with-link viewer 권한 부여"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """공지사항 첨부파일 Google Drive 업로드 (관리자)"""
    from routers.upload import MAX_FILE_SIZE, generate_filename, validate_file

    try:
        if not validate_file(file):
            raise HTTPException(status_code=400, detail="지원하지 않는 파일 형식입니다. PNG, JPG, JPEG, PDF만 업로드 가능합니다.")
        file_bytes = await file.read()
        if len(file_bytes) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="파일 크기가 10MB를 초과합니다.")
        new_filename = generate_filename(file.filename)
        file_ext = os.path.splitext(new_filename)[1]
        mime_type = get_mime_type(file_ext)
        folder_id = settings.GOOGLE_DRIVE_NOTICE_FOLDER_ID
        if not folder_id:
            raise HTTPException(status_code=500, detail="공지사항용 Google Drive 폴더 ID가 설정되지 않았습니다.")
        try:
            access_token = load_access_token()
        except Exception as e:
            error_msg = str(e)
            if "리프레시 토큰이 만료" in error_msg or "토큰을 재발급" in error_msg:
                raise HTTPException(status_code=503, detail="Google Drive 토큰이 만료되었습니다.")
            raise HTTPException(status_code=500, detail=f"파일 업로드 중 오류: {error_msg}")
        import json as _json
        boundary = "teeuplink_boundary"
        metadata = {"name": new_filename, "parents": [folder_id]}
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{_json.dumps(metadata)}\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n").encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
        params = {"uploadType": "multipart", "fields": "id,webViewLink,webContentLink,name,size,mimeType"}
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": f"multipart/related; boundary={boundary}"}
        resp = requests.post("https://www.googleapis.com/upload/drive/v3/files", params=params, headers=headers, data=body, timeout=60)
        if resp.status_code not in (200, 201):
            logger.error("Drive 업로드 실패: %s %s", resp.status_code, resp.text)
            raise HTTPException(status_code=500, detail="파일 업로드 중 오류가 발생했습니다.")
        file_id = resp.json().get("id")
        if not file_id:
            raise HTTPException(status_code=500, detail="파일 ID를 받지 못했습니다.")
        resp_meta = requests.get(f"https://www.googleapis.com/drive/v3/files/{file_id}", params={"fields": "id,webViewLink,webContentLink,name,size,mimeType"}, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
        meta = resp_meta.json() if resp_meta.status_code == 200 else resp.json()
        result = {"success": True, "message": "파일이 성공적으로 업로드되었습니다.", "filename": new_filename, "original_filename": file.filename, "file_size": len(file_bytes), "file_id": meta.get("id"), "web_view_link": meta.get("webViewLink"), "web_content_link": meta.get("webContentLink"), "mime_type": meta.get("mimeType", mime_type)}
        if share == "link" and file_id:
            try:
                perm_resp = requests.post(f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions", headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}, params={"fields": "id"}, json={"role": "reader", "type": "anyone"}, timeout=20)
                if perm_resp.status_code in (200, 201):
                    resp_meta2 = requests.get(f"https://www.googleapis.com/drive/v3/files/{file_id}", params={"fields": "id,webViewLink,webContentLink"}, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
                    if resp_meta2.status_code == 200:
                        m2 = resp_meta2.json()
                        result["web_view_link"] = m2.get("webViewLink")
                        result["web_content_link"] = m2.get("webContentLink")
            except Exception as perm_e:
                logger.warning("권한 부여 처리 경고: %s", perm_e)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("공지 파일 업로드 오류: %s", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"공지사항 파일 업로드 중 오류: {str(e)}")


@router.get("/notices/{notice_id}")
async def get_admin_notice(
    notice_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_admin_user),
):
    """관리자용 공지사항 상세 조회"""
    from models import Admin
    notice = _get_or_404(db, notice_id)
    author = db.query(Admin.name).filter(Admin.id == notice.author_id).scalar() if notice.author_id else None
    return {
        **_row(notice, {notice.author_id: author}),
        "content": notice.content,
        "attachment_file": notice.attachment_file,
        "web_view_link": notice.web_view_link,
    }


@router.post("/notices")
async def create_admin_notice(
        notice_data: dict,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 공지사항 생성. 게시로 저장하면 회원 알림을 보낸다"""
    from models import Notice
    from models.enums import NoticeType

    _validate(notice_data, partial=False)
    is_published = bool(notice_data.get("is_published", False))
    notice = Notice(
        title=str(notice_data["title"]).strip(),
        content=notice_data["content"],
        type=NoticeType.SYSTEM,
        author_id=current_user["id"],
        is_important=bool(notice_data.get("is_important", False)),
        is_published=is_published,
        published_at=get_kst_now() if is_published else None,
        attachment_file=notice_data.get("attachment_file") or None,
        web_view_link=notice_data.get("web_view_link") or None,
    )
    db.add(notice)
    db.commit()
    db.refresh(notice)
    audit.record("CREATE", "NOTICE", notice.id, after={"title": notice.title, "is_published": is_published, "is_important": notice.is_important})
    if is_published:
        _notify_published(db, notice)
    return {**_row(notice), "content": notice.content}


_EDITABLE = ("title", "content", "is_important", "is_published", "attachment_file", "web_view_link")


@router.put("/notices/{notice_id}")
async def update_admin_notice(
        notice_id: int,
        notice_data: dict,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 공지사항 수정. 미게시 → 게시로 바뀌면 그때 회원 알림을 보낸다"""
    notice = _get_or_404(db, notice_id)
    _validate(notice_data, partial=True)
    was_published = bool(notice.is_published)
    before, after = {}, {}
    for field in _EDITABLE:
        if field not in notice_data:
            continue
        value = notice_data[field]
        if field in ("is_important", "is_published"):
            value = bool(value)
        elif field == "title":
            value = str(value).strip()
        elif field in ("attachment_file", "web_view_link"):
            value = value or None
        old = getattr(notice, field)
        if old != value:
            # 본문은 길어서 감사 로그에는 바뀌었다는 사실만 남긴다
            before[field] = "(본문)" if field == "content" else old
            after[field] = "(본문 변경)" if field == "content" else value
            setattr(notice, field, value)
    if "is_published" in after:
        notice.published_at = get_kst_now() if notice.is_published else None
    notice.updated_at = get_kst_now()
    db.commit()
    db.refresh(notice)
    if after:
        audit.record("UPDATE", "NOTICE", notice.id, before=before, after=after)
    if notice.is_published and not was_published:
        _notify_published(db, notice)
    return {**_row(notice), "content": notice.content}


@router.delete("/notices/{notice_id}")
async def delete_admin_notice(
        notice_id: int,
        reason: str | None = Query(None, max_length=500),
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 공지사항 삭제 (영구 삭제 — 감사 로그에 제목과 게시 상태를 남긴다)"""
    notice = _get_or_404(db, notice_id)
    snapshot = {"title": notice.title, "is_published": bool(notice.is_published), "view_count": notice.view_count or 0}
    db.delete(notice)
    db.commit()
    audit.record("DELETE", "NOTICE", notice_id, before=snapshot, reason=reason)
    return {"message": "공지사항이 성공적으로 삭제되었습니다", "success": True}
