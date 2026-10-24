# 모임 관련 라우터 패키지
# 모든 모임 관련 API를 한 곳에서 관리

# 기본 모임 관리
from .base import router as base_router

# 참가자 관리
from .participants import router as participants_router

# 워크플로우 관리
from .workflow import router as workflow_router

# 정산 관리
from .settlement import router as settlement_router

# 팀 관리
from .teams import router as teams_router

# 점수 관리
from .scores import router as scores_router, meeting_score_router, round_score_router

# 비용 관리 (앱은 /rounds/{id}/expenses 를 호출하므로 같은 핸들러를 두 경로에 붙인다)
from .expenses import router as expenses_router, rounds_expenses_router

# 타입별 전용 API
from .types.rounds import router as rounds_router
from .types.socials import router as socials_router

__all__ = [
    "base_router",
    "participants_router",
    "workflow_router",
    "settlement_router",
    "teams_router",
    "scores_router",
    "meeting_score_router",
    "round_score_router",
    "expenses_router",
    "rounds_expenses_router",
    "rounds_router",
    "socials_router"
]
