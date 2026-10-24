# 스코어 관련 스키마
from datetime import datetime
from typing import List, Optional

from pydantic import AliasChoices, BaseModel, Field, model_validator


class _ScoreInputBase(BaseModel):
    hole_number: int = Field(..., ge=1, le=18, description="홀 번호")
    strokes: int = Field(
        ...,
        ge=1,
        le=20,
        description="타수",
        validation_alias=AliasChoices("strokes", "score"),  # 프런트 하위호환
    )
    par: int = Field(..., ge=3, le=6, description="파")
    score_to_par: Optional[int] = Field(None, description="파 대비 점수")

    @model_validator(mode="after")
    def validate_or_fill_score_to_par(self):
        computed = self.strokes - self.par
        if self.score_to_par is None:
            self.score_to_par = computed
            return self
        if self.score_to_par != computed:
            raise ValueError("score_to_par는 strokes - par 값과 같아야 합니다.")
        return self


class ScoreCreate(_ScoreInputBase):
    participant_id: int = Field(..., description="참가자 ID")


class MeetingParticipantScoreCreate(_ScoreInputBase):
    """meeting path 기반 입력용 (participant_id는 URL path 사용)"""


class ScoreUpdate(BaseModel):
    hole_number: Optional[int] = Field(None, ge=1, le=18, description="홀 번호")
    strokes: Optional[int] = Field(
        None,
        ge=1,
        le=20,
        description="타수",
        validation_alias=AliasChoices("strokes", "score"),  # 프런트 하위호환
    )
    par: Optional[int] = Field(None, ge=3, le=6, description="파")
    score_to_par: Optional[int] = Field(None, description="파 대비 점수")


class ScoreResponse(BaseModel):
    id: int
    participant_id: int
    user_id: int
    user_name: str
    user_nickname: str
    meeting_id: int
    meeting_name: str
    hole_number: int
    strokes: int
    par: int
    score_to_par: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SimpleScoreCreate(BaseModel):
    gross_score: int = Field(..., ge=55, le=144, description="라운딩 스코어 (55-144)")


class SimpleScoreResponse(BaseModel):
    message: str
    score_history_id: Optional[int] = None
    updated_handicap: Optional[float] = None


class ScoreListResponse(BaseModel):
    scores: List[ScoreResponse]
    total: int
    page: int
    limit: int
    total_pages: int


class ScoreStats(BaseModel):
    total_strokes: int
    total_par: int
    total_score_to_par: int
    average_strokes: float
    average_par: float
    average_score_to_par: float
    best_hole: int
    worst_hole: int
    birdies: int
    pars: int
    bogeys: int
    double_bogeys: int
    triple_bogeys: int
    worse: int
