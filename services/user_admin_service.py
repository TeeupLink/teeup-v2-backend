"""
관리자용 사용자 생성/수정 비즈니스 로직
(admin/users.py에서 사용)
"""
from datetime import datetime
from sqlalchemy.orm import Session

from fastapi import HTTPException, status
from models import User, UserStatus, Provider
from schemas import UserCreate, UserUpdate, UserResponse


def create_user_impl(user_data: UserCreate, db: Session) -> User:
    """사용자 생성 (관리자 전용 로직)"""
    import re
    from utils.passwords import hash_password
    from routers.auth import validate_nickname, validate_birthdate

    # 유효성 검사
    if user_data.realname is not None:
        name = user_data.realname.strip()
        if name and len(name) < 2:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="실명은 2자 이상이어야 합니다")
        if name:
            if re.search(r"[\uAC00-\uD7A3]", name):
                if " " in name or not re.fullmatch(r"[\uAC00-\uD7A3]+", name):
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="한글 이름은 공백 없이 한글만 입력해주세요")
            else:
                if not re.fullmatch(r"[A-Za-z ]+", name):
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="영문 이름은 알파벳과 공백만 입력해주세요")

    if user_data.nickname:
        if user_data.nickname.strip() != user_data.nickname:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="닉네임은 앞뒤 공백을 포함할 수 없습니다")
        if " " in user_data.nickname:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="닉네임에는 공백을 사용할 수 없습니다")
        if not validate_nickname(user_data.nickname):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="닉네임은 영문 대소문자, 한글, 숫자만 사용 가능하며 2-20자여야 합니다")

    if user_data.handicap is not None and (user_data.handicap < 0 or user_data.handicap > 72):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="핸디캡은 0-72 사이여야 합니다")
    if user_data.average_score is not None and (user_data.average_score < 55 or user_data.average_score > 144):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="평균 스코어는 55-144 사이여야 합니다")

    if db.query(User).filter(User.email == user_data.email).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 존재하는 이메일입니다")
    if db.query(User).filter(User.nickname == user_data.nickname).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 존재하는 닉네임입니다")

    password_hash = hash_password(user_data.password)
    birthdate_datetime = None
    if user_data.birthdate:
        birthdate_validation = validate_birthdate(user_data.birthdate)
        if not birthdate_validation["is_valid"]:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="; ".join(birthdate_validation["errors"]))
        birthdate_datetime = datetime.strptime(user_data.birthdate, "%Y-%m-%d")

    user = User(
        email=user_data.email,
        realname=user_data.realname,
        nickname=user_data.nickname,
        password=password_hash,
        phone_number=user_data.phone_number,
        birthdate=birthdate_datetime,
        gender=user_data.gender,
        handicap=user_data.handicap,
        average_score=user_data.average_score,
        status=UserStatus(user_data.status) if user_data.status else UserStatus.ACTIVE,
        provider=Provider.LOCAL,
        needs_terms_agreement=user_data.needs_terms_agreement if user_data.needs_terms_agreement is not None else True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def update_user_impl(user_id: int, user_data: UserUpdate, db: Session) -> User:
    """사용자 수정 (관리자 전용 로직)"""
    import re
    from routers.auth import validate_nickname

    user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다")

    if user_data.realname is not None:
        name = user_data.realname.strip()
        if len(name) < 2:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="실명은 2자 이상이어야 합니다")
        if re.search(r"[\uAC00-\uD7A3]", name):
            if not re.fullmatch(r"[\uAC00-\uD7A3]+", name):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="한글 이름은 공백 없이 한글만 입력해주세요")
        else:
            if not re.fullmatch(r"[A-Za-z ]+", name):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="영문 이름은 알파벳과 공백만 입력해주세요")

    if user_data.handicap is not None and (user_data.handicap < 0 or user_data.handicap > 72):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="핸디캡은 0-72 사이여야 합니다")
    if user_data.average_score is not None and (user_data.average_score < 55 or user_data.average_score > 144):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="평균 스코어는 55-144 사이여야 합니다")

    if user_data.email and user_data.email != user.email:
        if db.query(User).filter(User.email == user_data.email, User.id != user_id).first():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 존재하는 이메일입니다")
    if user_data.nickname and user_data.nickname != user.nickname:
        if db.query(User).filter(User.nickname == user_data.nickname, User.id != user_id).first():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 존재하는 닉네임입니다")
    if user_data.phone_number and user_data.phone_number != user.phone_number:
        if db.query(User).filter(User.phone_number == user_data.phone_number, User.id != user_id).first():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 사용 중인 전화번호입니다")

    update_data = user_data.dict(exclude_unset=True)
    for k in ("birthdate", "phone_number", "gender"):
        if k in update_data and update_data[k] == "":
            update_data[k] = None

    for field, value in update_data.items():
        if field == "role":
            continue
        elif field == "status" and value:
            setattr(user, field, UserStatus(value))
        elif field == "gender":
            setattr(user, field, value if value else None)
        elif field == "birthdate":
            if value:
                try:
                    setattr(user, field, datetime.strptime(value, "%Y-%m-%d"))
                except (ValueError, TypeError):
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="생년월일 형식이 올바르지 않습니다")
            else:
                setattr(user, field, None)
        elif field == "phone_number":
            setattr(user, field, value if value else None)
        else:
            setattr(user, field, value)

    db.commit()
    db.refresh(user)
    return user
