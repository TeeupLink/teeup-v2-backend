"""
파일 업로드 API (Google Drive 연동)
"""

import os
from datetime import datetime
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
from routers.auth import get_current_active_user
from config import settings
from models import User
import logging
import requests

from utils.google_oauth import load_access_token, get_mime_type

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/upload", tags=["upload"])

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf"}
MAX_FILE_SIZE = settings.max_file_size


def validate_file(file: UploadFile) -> bool:
    if not file.filename:
        return False
    file_extension = os.path.splitext(file.filename.lower())[1]
    if file_extension not in ALLOWED_EXTENSIONS:
        return False
    return True


def generate_filename(original_filename: str) -> str:
    name, ext = os.path.splitext(original_filename)
    date_str = datetime.now().strftime("%y%m%d")
    return f"{name}-{date_str}{ext}"


# 이 라우터의 엔드포인트는 일부러 async 가 아니다. Google Drive 를
# requests(블로킹) 로 호출하므로 async 안에서 돌리면 이벤트 루프가 멈춘다.
# 동기 엔드포인트는 FastAPI 가 스레드풀에서 실행하므로 루프를 막지 않는다.
@router.post("/")
def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    share: str | None = Query(default=None, description="'link' 지정 시 anyone-with-link viewer 권한 부여"),
    folder_type: str = Query(default="club", description="폴더 타입: 'club' (클럽 등록), 'notice' (공지사항 첨부파일)")
):
    """파일을 Google Drive 지정 폴더로 업로드"""
    try:
        if not validate_file(file):
            raise HTTPException(status_code=400, detail="지원하지 않는 파일 형식입니다. PNG, JPG, JPEG, PDF만 업로드 가능합니다.")

        file_bytes = file.file.read()
        if len(file_bytes) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="파일 크기가 10MB를 초과합니다.")

        new_filename = generate_filename(file.filename)
        file_ext = os.path.splitext(new_filename)[1]
        mime_type = get_mime_type(file_ext)

        # folder_type에 따라 폴더 ID 선택
        if folder_type == "notice":
            folder_id = settings.GOOGLE_DRIVE_NOTICE_FOLDER_ID
            if not folder_id:
                raise HTTPException(status_code=500, detail="공지사항용 Google Drive 폴더 ID(GOOGLE_DRIVE_NOTICE_FOLDER_ID)가 설정되지 않았습니다.")
        else:  # club (기본값)
            folder_id = settings.GOOGLE_DRIVE_CLUB_FOLDER_ID
            if not folder_id:
                raise HTTPException(status_code=500, detail="클럽 등록용 Google Drive 폴더 ID(GOOGLE_DRIVE_CLUB_FOLDER_ID)가 설정되지 않았습니다.")

        try:
            access_token = load_access_token()
        except Exception as e:
            logger.error(f"파일 업로드 중 오류: {e}")
            error_msg = str(e)
            if "리프레시 토큰이 만료" in error_msg or "토큰을 재발급" in error_msg:
                raise HTTPException(
                    status_code=503,
                    detail="Google Drive 토큰이 만료되었습니다. 관리자에게 문의하거나 토큰을 재발급해주세요."
                )
            raise HTTPException(status_code=500, detail=f"파일 업로드 중 오류가 발생했습니다: {error_msg}")

        # multipart/related 구성
        boundary = "teeuplink_boundary"
        metadata = {
            "name": new_filename,
            "parents": [folder_id],
        }
        import json as _json
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{_json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

        params = {"uploadType": "multipart", "fields": "id,name,mimeType"}
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        }

        resp = requests.post(
            "https://www.googleapis.com/upload/drive/v3/files",
            params=params,
            headers=headers,
            data=body,
            timeout=60,
        )

        if resp.status_code not in (200, 201):
            logger.error("Drive 업로드 실패: %s %s", resp.status_code, resp.text)
            raise HTTPException(status_code=500, detail="파일 업로드 중 오류가 발생했습니다.")

        file_meta = resp.json()
        file_id = file_meta.get("id")

        # 링크 조회
        params_get = {"fields": "id,webViewLink,webContentLink,name,size,mimeType"}
        resp_meta = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params=params_get,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        if resp_meta.status_code != 200:
            logger.warning("Drive 파일 메타 조회 실패(링크 없음 가능): %s %s", resp_meta.status_code, resp_meta.text)
            meta = {"id": file_id, "name": new_filename, "mimeType": mime_type}
        else:
            meta = resp_meta.json()

        result = {
            "success": True,
            "message": "파일이 성공적으로 업로드되었습니다.",
            "filename": new_filename,
            "original_filename": file.filename,
            "file_size": len(file_bytes),
            "file_id": meta.get("id"),
            "web_view_link": meta.get("webViewLink"),
            "web_content_link": meta.get("webContentLink"),
            "mime_type": meta.get("mimeType", mime_type),
        }

        # 옵션: anyone-with-link viewer 권한 부여
        try:
            if share == "link" and file_id:
                perm_body = {"role": "reader", "type": "anyone"}
                perm_resp = requests.post(
                    f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    params={"fields": "id"},
                    json=perm_body,
                    timeout=20,
                )
                if perm_resp.status_code not in (200, 201):
                    logger.warning("권한 부여 실패: %s %s", perm_resp.status_code, perm_resp.text)
                else:
                    # 공개 링크 보장을 위해 메타 재조회
                    resp_meta2 = requests.get(
                        f"https://www.googleapis.com/drive/v3/files/{file_id}",
                        params={"fields": "id,webViewLink,webContentLink"},
                        headers={"Authorization": f"Bearer {access_token}"},
                        timeout=20,
                    )
                    if resp_meta2.status_code == 200:
                        m2 = resp_meta2.json()
                        result["web_view_link"] = m2.get("webViewLink")
                        result["web_content_link"] = m2.get("webContentLink")
        except Exception as perm_e:
            logger.warning("권한 부여 처리 중 경고: %s", perm_e)

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"파일 업로드 중 오류: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"파일 업로드 중 오류가 발생했습니다: {str(e)}")


@router.get("/{file_id}")
def download_file(
    file_id: int,
    current_user: User = Depends(get_current_active_user)
):
    """Google Drive 파일 다운로드 (프록시)"""
    try:
        access_token = load_access_token()
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
        params = {"alt": "media"}
        headers = {"Authorization": f"Bearer {access_token}"}
        resp = requests.get(url, params=params, headers=headers, stream=True, timeout=60)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail="파일을 찾을 수 없습니다.")
        return StreamingResponse(resp.iter_content(chunk_size=8192), media_type="application/octet-stream")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"파일 다운로드 중 오류: {str(e)}")
        raise HTTPException(status_code=500, detail="파일 다운로드 중 오류가 발생했습니다.")


@router.delete("/{file_id}")
def delete_file(
    file_id: int,
    current_user: User = Depends(get_current_active_user)
):
    """Google Drive 파일 삭제"""
    try:
        access_token = load_access_token()
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        resp = requests.delete(url, headers=headers, timeout=20)
        if resp.status_code not in (200, 204):
            raise HTTPException(status_code=resp.status_code, detail="파일을 찾을 수 없습니다.")
        logger.info(f"파일 삭제 성공: {file_id}")
        return {"success": True, "message": "파일이 성공적으로 삭제되었습니다."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"파일 삭제 중 오류: {str(e)}")
        raise HTTPException(status_code=500, detail="파일 삭제 중 오류가 발생했습니다.")
