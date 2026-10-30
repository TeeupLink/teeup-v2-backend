"""
관리자 회원 등록·수정 (backoffice F-031, SCR-031)

앱의 프로필 수정(routers/users.py update_user)과 분리한다.
- 관리자는 성별·상태처럼 앱에서 막힌 항목도 고칠 수 있다.
- 중복·형식 오류는 필드 단위로 돌려준다: detail=[{field, message}] (409·422)
- 감사 로그용 전·후 스냅샷을 돌려준다 (호출부에서 audit.record)
"""
import re
from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from models import Gender, HandicapUpdateMethod, Provider, User, UserStatus
from utils.datetime_utils import get_kst_now
from utils.handicap_calculator import calculate_handicap_from_average_score
from utils.passwords import hash_password

# 감사 스냅샷·수정 대상 항목 (비밀번호는 넣지 않는다)
FIELDS = ("email", "realname", "nickname", "phone_number", "birthdate", "gender", "average_score_init", "handicap_init", "status")
_EDITABLE_STATUS = (UserStatus.ACTIVE.value, UserStatus.DEACTIVATED.value)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AdminUserUpdate(BaseModel):
    email: str | None = None
    realname: str | None = None
    nickname: str | None = None
    phone_number: str | None = None
    birthdate: str | None = Field(None, description="YYYY-MM-DD")
    gender: str | None = None
    average_score_init: int | None = Field(None, ge=55, le=144)
    handicap_init: float | None = Field(None, ge=0, le=72, description="비우면 평균스코어로 계산")
    status: str | None = None
    reason: str | None = Field(None, max_length=500, description="감사 로그 사유")


class AdminUserCreate(AdminUserUpdate):
    email: str
    password: str
    realname: str
    nickname: str


def _field_error(code: int, field: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail=[{"field": field, "message": message}])


def _plain(v):
    if hasattr(v, "value"):
        return v.value
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def snapshot(user: User, fields=FIELDS) -> dict:
    return {f: _plain(getattr(user, f)) for f in fields}


def validate_password(password: str) -> None:
    """8자 이상, 영문·숫자·특수문자 모두 포함"""
    ok = (8 <= len(password) <= 64 and re.search(r"[A-Za-z]", password) and re.search(r"\d", password)
          and re.search(r"[^A-Za-z0-9]", password))
    if not ok:
        raise _field_error(422, "password", "비밀번호는 8자 이상이며 영문·숫자·특수문자를 모두 포함해야 합니다")


def _clean(data: dict) -> dict:
    """형식 검사 + 값 정리. 빈 문자열은 비움(None)으로 본다"""
    out = {}
    for k, v in data.items():
        if isinstance(v, str):
            v = v.strip() or None
        out[k] = v
    if out.get("email") is not None and not _EMAIL.match(out["email"]):
        raise _field_error(422, "email", "이메일 형식이 올바르지 않습니다")
    for required in ("email", "nickname", "realname"):
        if required in out and out[required] is None:
            raise _field_error(422, required, "필수 항목입니다")
    name = out.get("realname")
    if name is not None:
        hangul = re.search(r"[가-힣]", name)
        if len(name) < 2 or not re.fullmatch(r"[가-힣]+" if hangul else r"[A-Za-z ]+", name):
            raise _field_error(422, "realname", "실명은 2자 이상, 한글(공백 없이) 또는 영문으로 입력해주세요")
    if out.get("birthdate") is not None:
        try:
            out["birthdate"] = datetime.strptime(out["birthdate"], "%Y-%m-%d")
        except ValueError:
            raise _field_error(422, "birthdate", "생년월일은 YYYY-MM-DD 형식입니다")
    if out.get("gender") is not None:
        if out["gender"] not in Gender.__members__:
            raise _field_error(422, "gender", "성별 값이 올바르지 않습니다")
        out["gender"] = Gender(out["gender"])
    if "status" in out:
        if out["status"] not in _EDITABLE_STATUS:
            raise _field_error(422, "status", "상태는 ACTIVE 또는 DEACTIVATED 입니다 (탈퇴는 삭제로 처리)")
        out["status"] = UserStatus(out["status"])
    if out.get("handicap_init") is not None:
        out["handicap_init"] = Decimal(str(round(out["handicap_init"], 1)))
    return out


def _check_unique(db: Session, data: dict, user_id: int | None) -> None:
    labels = {"email": "이미 사용 중인 이메일입니다", "nickname": "이미 사용 중인 닉네임입니다", "phone_number": "이미 사용 중인 전화번호입니다"}
    for field, message in labels.items():
        value = data.get(field)
        if value is None:
            continue
        q = db.query(User.id).filter(getattr(User, field) == value)
        if user_id is not None:
            q = q.filter(User.id != user_id)
        if q.first():
            raise _field_error(409, field, message)


def _apply(user: User, data: dict) -> None:
    for k, v in data.items():
        setattr(user, k, v)
    # 평균스코어만 바꾸면 초기 핸디캡을 다시 계산한다. 둘 다 오면 관리자가 넣은 핸디캡을 쓴다.
    if data.get("average_score_init") is not None and "handicap_init" not in data:
        user.handicap_init = calculate_handicap_from_average_score(data["average_score_init"])
    if "average_score_init" in data or "handicap_init" in data:
        user.handicap_update_method = HandicapUpdateMethod.MANUAL
    if "status" in data:
        user.deactivated_at = get_kst_now().replace(tzinfo=None) if data["status"] == UserStatus.DEACTIVATED else None


def create_user(db: Session, body: AdminUserCreate) -> User:
    validate_password(body.password)
    data = _clean(body.model_dump(exclude={"password", "reason"}, exclude_unset=True))
    data.setdefault("status", UserStatus.ACTIVE)
    _check_unique(db, data, None)
    user = User(
        password=hash_password(body.password),
        provider=Provider.LOCAL,
        needs_terms_agreement=True,
    )
    _apply(user, data)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def update_user(db: Session, user: User, body: AdminUserUpdate) -> tuple[dict, dict]:
    """바뀐 항목만 (before, after) 로 돌려준다"""
    if user.status == UserStatus.DELETED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="탈퇴한 회원은 수정할 수 없습니다")
    data = _clean(body.model_dump(exclude={"reason"}, exclude_unset=True))
    _check_unique(db, data, user.id)
    before = snapshot(user)
    _apply(user, data)
    after = snapshot(user)
    changed = [f for f in FIELDS if before[f] != after[f]]
    if changed:
        db.commit()
        db.refresh(user)
    else:
        db.rollback()
    return {f: before[f] for f in changed}, {f: after[f] for f in changed}
