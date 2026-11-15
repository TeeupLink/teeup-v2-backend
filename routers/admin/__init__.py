"""
백오피스 API 라우터 패키지
- auth, search, admins: 인증, 검색, 관리자 CRUD
- users, dashboard, upload, profile: 사용자, 대시보드, 업로드, 프로필
- payments, terms, notices, inquiries, notifications, scores: 결제, 약관, 공지, 문의, 알림, 스코어
- clubs, meetings: 클럽, 모임
"""
from fastapi import APIRouter, Depends

from .deps import get_admin_user, require_super_admin
from .permissions import require_resource
from .tokens import is_token_used, mark_token_as_used

from .auth import router as auth_router
from .search import router as search_router
from .admins import router as admins_router
from .users import router as users_router
from .dashboard import router as dashboard_router
from .kpi import router as kpi_router
from .upload import router as upload_router
from .profile import router as profile_router
from .payments import router as payments_router
from .billing import router as billing_router
from .billing_sandbox import router as billing_sandbox_router
from .terms import router as terms_router
from .notices import router as notices_router
from .inquiries import router as inquiries_router
from .notifications import router as notifications_router
from .scores import router as scores_router
from .clubs import router as clubs_router
from .meetings import router as meetings_router
from .rounds import router as rounds_router
from .faq import router as faq_router
from .reports import router as reports_router
from .join_requests import router as join_requests_router
from .lookup import router as lookup_router
from .audit_logs import router as audit_logs_router
from .activities import router as activities_router

router = APIRouter(prefix="/admin", tags=["admin"])

# 인증/프로필/설정: 모든 역할 접근 가능
router.include_router(auth_router)
router.include_router(profile_router)
router.include_router(search_router)

# 관리자 CRUD: 슈퍼어드민만
router.include_router(admins_router, dependencies=[Depends(require_super_admin)])

# 분야별 라우터: 권한 표(permissions.py) 기준. 조회는 read, 변경은 write 권한이 필요하다
router.include_router(users_router, dependencies=[Depends(require_resource("users"))])
router.include_router(activities_router, dependencies=[Depends(require_resource("users"))])
router.include_router(clubs_router, dependencies=[Depends(require_resource("clubs"))])
router.include_router(join_requests_router, dependencies=[Depends(require_resource("joinRequests"))])
# rounds 는 meetings 의 /scores/{participant_id} 보다 먼저 (/scores/bulk)
router.include_router(rounds_router, dependencies=[Depends(require_resource("meetings"))])
router.include_router(meetings_router, dependencies=[Depends(require_resource("meetings"))])
router.include_router(scores_router, dependencies=[Depends(require_resource("meetings"))])
router.include_router(notices_router, dependencies=[Depends(require_resource("content"))])
router.include_router(faq_router, dependencies=[Depends(require_resource("content"))])
router.include_router(terms_router, dependencies=[Depends(require_resource("content"))])
router.include_router(inquiries_router, dependencies=[Depends(require_resource("inquiries"))])
router.include_router(reports_router, dependencies=[Depends(require_resource("reports"))])
router.include_router(payments_router, dependencies=[Depends(require_resource("payments"))])
router.include_router(billing_router, dependencies=[Depends(require_resource("payments"))])
router.include_router(billing_sandbox_router, dependencies=[Depends(require_resource("billingSandbox"))])
router.include_router(audit_logs_router, dependencies=[Depends(require_resource("auditLogs"))])

# 대시보드/업로드/알림/선택용 조회: 모든 역할 접근 가능 (항목별 범위는 각 엔드포인트가 거른다)
router.include_router(dashboard_router)
router.include_router(kpi_router, dependencies=[Depends(require_resource("kpi"))])
router.include_router(upload_router)
router.include_router(notifications_router)
router.include_router(lookup_router)

__all__ = ["router", "get_admin_user"]
