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
from utils.datetime_utils import get_kst_now
from utils.google_oauth import get_mime_type, load_access_token
from .deps import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-notices"])


@router.get("/notices")
async def get_admin_notices(
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 공지사항 목록 조회"""
    try:
        from models import Notice
        from sqlalchemy import desc

        notices = db.query(Notice).order_by(desc(Notice.created_at)).limit(20).all()
        notice_list = []
        for n in notices:
            notice_list.append({
                "id": n.id,
                "title": n.title,
                "type": n.type.value,
                "is_published": n.is_published,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            })
        return {"data": notice_list, "total": len(notice_list)}
    except Exception as e:
        logger.error(f"관리자 공지사항 목록 조회 중 오류: {str(e)}")
        return {"data": [], "total": 0}


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
    try:
        from models import Notice

        notice = db.query(Notice).filter(Notice.id == notice_id).first()
        if not notice:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공지사항을 찾을 수 없습니다")
        notice_type = notice.type.value if hasattr(notice.type, "value") else str(notice.type)
        return {
            "id": notice.id,
            "title": notice.title,
            "content": notice.content,
            "type": notice_type,
            "is_important": getattr(notice, "is_important", False),
            "is_published": getattr(notice, "is_published", False),
            "view_count": getattr(notice, "view_count", 0),
            "published_at": getattr(notice, "published_at", None),
            "attachment_file": getattr(notice, "attachment_file", None),
            "web_view_link": getattr(notice, "web_view_link", None),
            "created_at": notice.created_at.isoformat() if notice.created_at else None,
            "updated_at": notice.updated_at.isoformat() if notice.updated_at else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"관리자 공지사항 상세 조회 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="공지사항 조회 중 오류가 발생했습니다")


@router.post("/notices")
async def create_admin_notice(
        notice_data: dict,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 공지사항 생성"""
    try:
        from models import Notice
        from models.enums import NoticeType

        for field in ["title", "content"]:
            if field not in notice_data or not notice_data[field]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field} 필드는 필수입니다")
        is_published = bool(notice_data.get("is_published", False))
        new_notice = Notice(
            title=notice_data["title"],
            content=notice_data["content"],
            type=NoticeType.SYSTEM,
            author_id=current_user["id"],
            is_important=notice_data.get("is_important", False),
            is_published=is_published,
            published_at=get_kst_now() if is_published else None,
        )
        db.add(new_notice)
        db.commit()
        db.refresh(new_notice)
        try:
            from utils.notification_service import create_notice_notification
            from models import User, UserStatus
            for user in db.query(User).filter(User.status == UserStatus.ACTIVE).all():
                create_notice_notification(db=db, user_id=user.id, notice_title=notice_data["title"])
        except Exception as e:
            logger.error(f"시스템 공지사항 알림 전송 실패: {str(e)}")
        return {
            "id": new_notice.id,
            "title": new_notice.title,
            "content": new_notice.content,
            "is_important": new_notice.is_important,
            "is_published": new_notice.is_published,
            "published_at": new_notice.published_at.isoformat() if new_notice.published_at else None,
            "created_at": new_notice.created_at.isoformat() if new_notice.created_at else None,
            "updated_at": new_notice.updated_at.isoformat() if new_notice.updated_at else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"관리자 공지사항 생성 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"공지사항 생성 중 오류가 발생했습니다: {str(e)}")


@router.put("/notices/{notice_id}")
async def update_admin_notice(
        notice_id: int,
        notice_data: dict,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 공지사항 수정"""
    try:
        from models import Notice

        notice = db.query(Notice).filter(Notice.id == notice_id).first()
        if not notice:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공지사항을 찾을 수 없습니다")
        if "title" in notice_data:
            notice.title = notice_data["title"]
        if "content" in notice_data:
            notice.content = notice_data["content"]
        if "is_important" in notice_data:
            notice.is_important = notice_data["is_important"]
        if "is_published" in notice_data:
            notice.is_published = bool(notice_data["is_published"])
            if notice.is_published and not notice.published_at:
                notice.published_at = get_kst_now()
            elif not notice.is_published:
                notice.published_at = None
        notice.updated_at = get_kst_now()
        db.commit()
        db.refresh(notice)
        return {
            "id": notice.id,
            "title": notice.title,
            "content": notice.content,
            "is_important": notice.is_important,
            "is_published": notice.is_published,
            "published_at": notice.published_at.isoformat() if notice.published_at else None,
            "created_at": notice.created_at.isoformat() if notice.created_at else None,
            "updated_at": notice.updated_at.isoformat() if notice.updated_at else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"관리자 공지사항 수정 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="공지사항 수정 중 오류가 발생했습니다")


@router.delete("/notices/{notice_id}")
async def delete_admin_notice(
        notice_id: int,
        db: Session = Depends(get_db),
        current_user: dict = Depends(get_admin_user),
):
    """관리자용 공지사항 삭제"""
    try:
        from models import Notice

        notice = db.query(Notice).filter(Notice.id == notice_id).first()
        if not notice:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공지사항을 찾을 수 없습니다")
        if hasattr(notice, "deleted_at"):
            notice.deleted_at = get_kst_now()
        else:
            db.delete(notice)
        db.commit()
        return {"message": "공지사항이 성공적으로 삭제되었습니다", "success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"관리자 공지사항 삭제 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="공지사항 삭제 중 오류가 발생했습니다")
