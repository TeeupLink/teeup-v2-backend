# 팀 관련 스키마
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator
from typing import Any, TYPE_CHECKING
from datetime import datetime
from .enums import TeamFormationMode, TeamStatus

# 순환 참조를 피하기 위해 지연 import
if TYPE_CHECKING:
    from .meeting import MeetingParticipantResponse
else:
    # 런타임에 forward reference를 해결하기 위해 실제 import
    try:
        from .meeting import MeetingParticipantResponse
    except ImportError:
        MeetingParticipantResponse = None


class TeamCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="팀 이름")
    formation_mode: TeamFormationMode = Field(..., description="팀 편성 모드")
    formation_notes: str | None = Field(None, description="팀 편성 메모")


class TeamUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255, description="팀 이름")
    formation_mode: TeamFormationMode | None = Field(None, description="팀 편성 모드")
    formation_notes: str | None = Field(None, description="팀 편성 메모")


class TeamResponse(BaseModel):
    id: int
    name: str
    meeting_id: int
    formation_mode: TeamFormationMode
    status: TeamStatus
    formation_notes: str | None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    member_count: int
    members: list["TeamMemberResponse"] | None = Field(default_factory=list, description="팀 멤버 목록")
    meeting_name: str | None = Field(None, description="모임 이름")
    total_handicap: float | None = Field(None, description="총 핸디캡")
    tee_off_order: int | None = Field(None, description="티오프 순서")

    model_config = {"from_attributes": True}


class TeamMemberResponse(BaseModel):
    id: int
    team_id: int
    user_id: int | None = None
    guest_id: int | None = Field(None, description="게스트 ID")
    participant_id: int | None = Field(None, description="MeetingParticipant.id (정산/매칭용)")
    user_name: str
    user_nickname: str
    order: int | None
    gender: str | None = Field(None, description="성별 (MALE, FEMALE, OTHER)")
    handicap: int | None = Field(None, description="핸디캡 인덱스")
    average_score: int | None = Field(None, description="직전대회 평균 타수")
    is_guest: bool | None = Field(None, description="게스트 여부")
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class GuestCreate(BaseModel):
    """게스트 생성 스키마"""
    name: str = Field(..., min_length=1, max_length=255, description="게스트 이름")
    birthdate: str | None = Field(None, description="생년월일 (YYYY-MM-DD 형식, 만 14세 이상)")
    gender: str | None = Field(None, description="성별 (MALE 또는 FEMALE만 허용)")
    average_score: int | None = Field(None, ge=50, le=150, description="평균 타수 (핸디캡 계산용)")
    handicap: float | None = Field(None, ge=0, le=72, description="게스트 핸디캡 (평균 타수 - 72, 자동 계산 가능)")

    @field_validator('gender')
    @classmethod
    def validate_gender(cls, v):
        if v is not None and v not in ['MALE', 'FEMALE']:
            raise ValueError('성별은 MALE 또는 FEMALE만 허용됩니다.')
        return v


class GuestUpdate(BaseModel):
    """게스트 수정 스키마"""
    name: str | None = Field(None, min_length=1, max_length=255, description="게스트 이름")
    birthdate: str | None = Field(None, description="생년월일 (YYYY-MM-DD 형식, 만 14세 이상)")
    gender: str | None = Field(None, description="성별 (MALE 또는 FEMALE만 허용)")
    average_score: int | None = Field(None, ge=50, le=150, description="평균 타수 (핸디캡 계산용)")
    handicap: float | None = Field(None, ge=0, le=72, description="게스트 핸디캡 (평균 타수 - 72, 자동 계산 가능)")

    @field_validator('gender')
    @classmethod
    def validate_gender(cls, v):
        if v is not None and v not in ['MALE', 'FEMALE']:
            raise ValueError('성별은 MALE 또는 FEMALE만 허용됩니다.')
        return v


class GuestResponse(BaseModel):
    """게스트 응답 스키마"""
    id: int
    meeting_id: int
    guest_name: str
    guest_handicap: float | None
    guest_birthdate: datetime | None = None
    guest_gender: str | None = None
    is_guest: bool = True
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class TeamMemberAddRequest(BaseModel):
    """팀 멤버 추가 요청 (user_id, guest_id, participant_id 중 하나 필수)"""
    user_id: int | None = Field(None, description="사용자 ID")
    guest_id: int | None = Field(None, description="게스트 ID")
    participant_id: int | None = Field(None, description="참가자 ID (MeetingParticipant.id)")


class TeamBulkItem(BaseModel):
    """모임 단위 팀 상태 저장용 팀 항목"""
    name: str = Field(..., min_length=1, max_length=255, description="팀 이름")
    members: list[TeamMemberAddRequest] = Field(default_factory=list, description="팀 멤버 목록")


class TeamBulkUpdateRequest(BaseModel):
    """모임 단위 팀 상태 전체 저장 요청"""
    teams: list[TeamBulkItem] = Field(default_factory=list, description="최종 팀 목록")


class TeamFormationRequest(BaseModel):
    formation_mode: TeamFormationMode = Field(..., description="편성 모드")
    team_size: int = Field(..., ge=2, le=4, description="팀 크기")
    preferences: dict[str, Any] | None = Field(None, description="편성 선호사항")
    guests: list[GuestCreate] | None = Field(None, description="게스트 목록 (선택적)")


class TeamFormationResponse(BaseModel):
    teams: list[TeamResponse]
    total_teams: int
    unassigned_participants: list["MeetingParticipantResponse"]  # 순환 참조 방지
    total_participants: int | None = Field(None, description="총 참가자 수")
    formation_summary: dict[str, Any] | None = Field(None, description="편성 요약 정보")

    model_config = {"from_attributes": True}


# Forward reference 해결을 위해 model_rebuild 호출
def _update_forward_refs():
    """Forward reference를 해결하기 위한 함수"""
    try:
        from .meeting import MeetingParticipantResponse
        TeamFormationResponse.model_rebuild()
    except (ImportError, NameError):
        pass


# 모듈이 로드된 후 forward reference 업데이트
_update_forward_refs()
