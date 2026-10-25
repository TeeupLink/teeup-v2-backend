#!/usr/bin/env python3
"""
약관 관리 API 라우터
- 서비스이용약관, 개인정보처리방침, 개인정보 수집 및 활용동의, 마케팅정보수신동의 관리
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
import logging
import secrets
import string
from datetime import datetime

from database import get_db
from models import Terms, TermsAgreement, User, Notification, Admin
from schemas import TermsType, NotificationType, NotificationStatus
from schemas import (TermsCreate, TermsUpdate, TermsResponse, TermsListResponse, TermsAgreementCreate,
                     TermsAgreementResponse, TermsAgreementBulkCreate, TermsAgreementBulkResponse)
from routers.auth import get_current_user

# 인증 관련
security = HTTPBearer(auto_error=False)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/terms", tags=["terms"])


# 약관 동의 API용 인증 함수 (약관 동의 체크 제외)
def get_current_user_for_terms_agreement(credentials: HTTPAuthorizationCredentials | None = Depends(security),
                                         request: Request = None,
                                         db: Session = Depends(get_db)):
    """약관 동의 API용 인증 함수 (약관 동의 체크 제외)"""
    return get_current_user(credentials=credentials,
                            request=request,
                            db=db,
                            required_type="user",
                            check_status=True,
                            check_terms_agreement=False)


def generate_id(length=20):
    """ID 생성 함수"""
    chars = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))


@router.get("/", response_model=TermsListResponse)
async def get_terms(page: int = Query(1, ge=1, description="페이지 번호"),
                    size: int = Query(20, ge=1, le=100, description="페이지 크기"),
                    type_filter: str | None = Query(None, description="약관 타입 필터"),
                    is_active: bool | None = Query(None, description="활성화 여부 필터"),
                    db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    """약관 목록 조회"""
    try:
        # 관리자 권한 확인
        admin = db.query(Admin).filter(Admin.id == current_user.id, Admin.deleted_at.is_(None)).first()

        if not admin:
            raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다")

        # 쿼리 빌드
        query = db.query(Terms)

        # 필터 적용
        if type_filter:
            query = query.filter(Terms.type == type_filter)
        if is_active is not None:
            query = query.filter(Terms.is_active == is_active)

        # 총 개수 조회
        total = query.count()

        # 페이지네이션 적용
        offset = (page - 1) * size
        terms = query.order_by(Terms.created_at.desc()).offset(offset).limit(size).all()

        # 응답 데이터 변환
        terms_data = []
        for term in terms:
            terms_data.append(
                TermsResponse(id=term.id,
                              type=term.type.value,
                              title=term.title,
                              content=term.content,
                              is_active=term.is_active,
                              is_required=term.is_required,
                              published_at=term.published_at,
                              created_at=term.created_at,
                              updated_at=term.updated_at))

        total_pages = (total + size - 1) // size if size > 0 else 0
        return TermsListResponse(terms=terms_data, total=total, page=page, size=size, total_pages=total_pages)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"약관 목록 조회 중 오류 발생: {e}")
        raise HTTPException(status_code=500, detail="약관 목록 조회 중 오류가 발생했습니다")


@router.get("/{terms_id}", response_model=TermsResponse)
async def get_terms_detail(terms_id: int, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    """약관 상세 조회"""
    try:
        # 관리자 권한 확인
        admin = db.query(Admin).filter(Admin.id == current_user.id, Admin.deleted_at.is_(None)).first()

        if not admin:
            raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다")

        # 약관 조회
        terms = db.query(Terms).filter(Terms.id == terms_id).first()
        if not terms:
            raise HTTPException(status_code=404, detail="약관을 찾을 수 없습니다")

        return TermsResponse(id=terms.id,
                             type=terms.type,
                             title=terms.title,
                             content=terms.content,
                             is_active=terms.is_active,
                             is_required=terms.is_required,
                             published_at=terms.published_at,
                             created_at=terms.created_at,
                             updated_at=terms.updated_at)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"약관 상세 조회 중 오류 발생: {e}")
        raise HTTPException(status_code=500, detail="약관 상세 조회 중 오류가 발생했습니다")


@router.post("/", response_model=TermsResponse)
async def create_terms(terms_data: TermsCreate,
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """약관 등록"""
    try:
        # 관리자 권한 확인
        admin = db.query(Admin).filter(Admin.id == current_user.id, Admin.deleted_at.is_(None)).first()

        if not admin:
            raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다")

        # 약관 타입 유효성 검사
        try:
            terms_type = TermsType(terms_data.type)
        except ValueError:
            raise HTTPException(status_code=400, detail="유효하지 않은 약관 타입입니다")

        # 새 약관 생성
        new_terms = Terms(type=terms_type.value,
                          title=terms_data.title,
                          content=terms_data.content,
                          is_active=terms_data.is_active,
                          is_required=terms_data.is_required,
                          published_at=datetime.now(),
                          created_by=admin.id)

        db.add(new_terms)
        db.commit()
        db.refresh(new_terms)

        # 약관 변경 알림 전송
        try:
            await send_terms_updated_notification(new_terms.id, db)
        except Exception as e:
            logger.error(f"약관 변경 알림 전송 실패: {e}")

        logger.info(f"새 약관 생성됨: {new_terms.id} ({terms_data.type})")

        return TermsResponse(id=new_terms.id,
                             type=new_terms.type,
                             title=new_terms.title,
                             content=new_terms.content,
                             is_active=new_terms.is_active,
                             is_required=new_terms.is_required,
                             published_at=new_terms.published_at,
                             created_at=new_terms.created_at,
                             updated_at=new_terms.updated_at)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"약관 등록 중 오류 발생: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="약관 등록 중 오류가 발생했습니다")


@router.put("/{terms_id}", response_model=TermsResponse)
async def update_terms(terms_id: int,
                       terms_data: TermsUpdate,
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """약관 수정"""
    try:
        # 관리자 권한 확인
        admin = db.query(Admin).filter(Admin.id == current_user.id, Admin.deleted_at.is_(None)).first()

        if not admin:
            raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다")

        # 약관 조회
        terms = db.query(Terms).filter(Terms.id == terms_id).first()
        if not terms:
            raise HTTPException(status_code=404, detail="약관을 찾을 수 없습니다")

        # 약관 정보 업데이트
        update_data = terms_data.dict(exclude_unset=True)
        for field, value in update_data.items():
            setattr(terms, field, value)

        terms.updated_at = datetime.now()

        db.commit()
        db.refresh(terms)

        logger.info(f"약관 수정됨: {terms_id}")

        return TermsResponse(id=terms.id,
                             type=terms.type,
                             title=terms.title,
                             content=terms.content,
                             is_active=terms.is_active,
                             is_required=terms.is_required,
                             published_at=terms.published_at,
                             created_at=terms.created_at,
                             updated_at=terms.updated_at)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"약관 수정 중 오류 발생: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="약관 수정 중 오류가 발생했습니다")


@router.get("/types/", response_model=list[dict])
async def get_terms_types(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """약관 타입 목록 조회"""
    try:
        # 관리자 권한 확인
        admin = db.query(Admin).filter(Admin.id == current_user.id, Admin.deleted_at.is_(None)).first()

        if not admin:
            raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다")

        types = []
        for terms_type in TermsType:
            type_name_map = {
                TermsType.SERVICE: "서비스이용약관",
                TermsType.PRIVACY: "개인정보처리방침",
                TermsType.PRIVACY_COLLECTION: "개인정보 수집 및 이용동의",
                TermsType.MARKETING: "마케팅정보수신동의"
            }

            types.append({"value": terms_type.value, "label": type_name_map.get(terms_type, terms_type.value)})

        return types

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"약관 타입 목록 조회 중 오류 발생: {e}")
        raise HTTPException(status_code=500, detail="약관 타입 목록 조회 중 오류가 발생했습니다")


@router.get("/active/", response_model=list[TermsResponse])
async def get_active_terms(type_filter: str | None = Query(None, description="약관 타입 필터"),
                           db: Session = Depends(get_db)):
    """활성화된 약관 목록 조회 (공개 API)"""
    try:
        # 쿼리 빌드
        query = db.query(Terms).filter(Terms.is_active == True)

        # 타입 필터 적용
        if type_filter:
            # 프론트엔드 타입 이름을 백엔드 enum으로 매핑
            type_mapping = {
                "TERMS_OF_SERVICE": "SERVICE",
                "SERVICE": "SERVICE",
                "PRIVACY_POLICY": "PRIVACY",
                "PRIVACY": "PRIVACY",
                "PRIVACY_COLLECTION": "PRIVACY_COLLECTION",
                "MARKETING": "MARKETING",
                "MARKETING_OPT_IN": "MARKETING"  # 프론트엔드에서 사용하는 타입
            }

            # 매핑된 타입 이름 가져오기
            mapped_type = type_mapping.get(type_filter.upper(), type_filter.upper())

            try:
                terms_type = TermsType(mapped_type)
                query = query.filter(Terms.type == terms_type.value)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"유효하지 않은 약관 타입입니다. 지원되는 타입: SERVICE, PRIVACY, PRIVACY_COLLECTION, MARKETING")

        # 최신 버전만 조회 (타입별로 최신 버전)
        terms = query.order_by(Terms.type, Terms.published_at.desc()).all()

        # 타입별로 최신 버전만 필터링
        latest_terms = {}
        for term in terms:
            if term.type not in latest_terms:
                latest_terms[term.type] = term

        # 응답 데이터 변환
        terms_data = []
        for term in latest_terms.values():
            terms_data.append(
                TermsResponse(id=term.id,
                              type=term.type,
                              title=term.title,
                              content=term.content,
                              is_active=term.is_active,
                              is_required=term.is_required,
                              published_at=term.published_at,
                              created_at=term.created_at,
                              updated_at=term.updated_at))

        return terms_data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"활성화된 약관 목록 조회 중 오류 발생: {e}")
        raise HTTPException(status_code=500, detail="활성화된 약관 목록 조회 중 오류가 발생했습니다")


@router.get("/agreements/", response_model=list[TermsAgreementResponse])
async def get_user_terms_agreements(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """사용자의 약관 동의 내역 조회"""
    try:
        # 사용자의 약관 동의 내역 조회
        agreements = db.query(TermsAgreement).filter(TermsAgreement.user_id == current_user.id).order_by(
            TermsAgreement.created_at.desc()).all()

        # 응답 데이터 변환
        agreements_data = []
        for agreement in agreements:
            agreements_data.append(
                TermsAgreementResponse(id=agreement.id,
                                       user_id=agreement.user_id,
                                       terms_id=agreement.terms_id,
                                       terms_title=agreement.terms.title,
                                       agreed_at=agreement.agreed_at,
                                       ip_address=agreement.ip_address,
                                       user_agent=agreement.user_agent,
                                       created_at=agreement.created_at))

        return agreements_data

    except Exception as e:
        logger.error(f"사용자 약관 동의 내역 조회 중 오류 발생: {e}")
        raise HTTPException(status_code=500, detail="사용자 약관 동의 내역 조회 중 오류가 발생했습니다")


@router.post("/agreements/", response_model=TermsAgreementResponse)
async def create_terms_agreement(agreement_data: TermsAgreementCreate,
                                 db: Session = Depends(get_db),
                                 current_user: User = Depends(get_current_user_for_terms_agreement)):
    """약관 동의 등록"""
    try:
        # 약관 존재 확인
        terms = db.query(Terms).filter(Terms.id == agreement_data.terms_id).first()
        if not terms:
            raise HTTPException(status_code=404, detail="약관을 찾을 수 없습니다")

        # 이미 동의한 약관인지 확인
        existing_agreement = db.query(TermsAgreement).filter(
            and_(TermsAgreement.user_id == current_user.id,
                 TermsAgreement.terms_id == agreement_data.terms_id)).first()

        if existing_agreement:
            raise HTTPException(status_code=400, detail="이미 동의한 약관입니다")

        # 약관 동의 등록
        new_agreement = TermsAgreement(user_id=current_user.id,
                                       terms_id=agreement_data.terms_id,
                                       agreed_at=agreement_data.agreed_at,
                                       ip_address=agreement_data.ip_address,
                                       user_agent=agreement_data.user_agent)

        db.add(new_agreement)

        # User 테이블의 약관 필드 업데이트
        # 약관 타입에 따라 해당 필드 업데이트
        if terms.type == TermsType.SERVICE:
            current_user.terms_agreement = True
        elif terms.type == TermsType.PRIVACY:
            current_user.privacy_policy = True
        elif terms.type == TermsType.PRIVACY_COLLECTION:
            current_user.privacy_collection = True
        elif terms.type == TermsType.MARKETING:
            current_user.marketing_consent = True

        # 필수 약관 3개 모두 동의했는지 확인
        if (current_user.terms_agreement and current_user.privacy_policy and current_user.privacy_collection):
            current_user.needs_terms_agreement = False
            logger.info(f"사용자 {current_user.id}의 모든 필수 약관 동의 완료")

        db.commit()
        db.refresh(new_agreement)
        db.refresh(current_user)

        logger.info(f"약관 동의 등록됨: {new_agreement.id} "
                    f"(사용자: {current_user.id}, 약관: {agreement_data.terms_id}, 타입: {terms.type.value})")

        return TermsAgreementResponse(id=new_agreement.id,
                                      user_id=new_agreement.user_id,
                                      terms_id=new_agreement.terms_id,
                                      terms_title=terms.title,
                                      agreed_at=new_agreement.agreed_at,
                                      ip_address=new_agreement.ip_address,
                                      user_agent=new_agreement.user_agent,
                                      created_at=new_agreement.created_at)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"약관 동의 등록 중 오류 발생: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="약관 동의 등록 중 오류가 발생했습니다")


@router.post("/agreements/bulk", response_model=TermsAgreementBulkResponse)
async def create_terms_agreements_bulk(agreement_data: TermsAgreementBulkCreate,
                                       db: Session = Depends(get_db),
                                       current_user: User = Depends(get_current_user)):
    """여러 약관을 한 번에 동의 등록 (OAuth 가입 후 약관 동의용)"""
    try:
        if not agreement_data.terms_ids:
            raise HTTPException(status_code=400, detail="약관 ID 목록이 비어있습니다")

        # 동의 시간 설정
        agreed_at = agreement_data.agreed_at or datetime.now()

        # 약관 조회 및 검증
        terms_list = db.query(Terms).filter(Terms.id.in_(agreement_data.terms_ids)).all()
        if len(terms_list) != len(agreement_data.terms_ids):
            raise HTTPException(status_code=404, detail="일부 약관을 찾을 수 없습니다")

        # 이미 동의한 약관 확인
        existing_agreements = db.query(TermsAgreement).filter(
            and_(TermsAgreement.user_id == current_user.id,
                 TermsAgreement.terms_id.in_(agreement_data.terms_ids))).all()

        existing_terms_ids = {ag.terms_id for ag in existing_agreements}
        new_terms_ids = [tid for tid in agreement_data.terms_ids if tid not in existing_terms_ids]

        # if not new_terms_ids:
        #     raise HTTPException(status_code=400, detail="모든 약관에 이미 동의했습니다")

        # 약관 동의 등록
        new_agreements = []
        for terms_id in new_terms_ids:
            terms = next(t for t in terms_list if t.id == terms_id)

            new_agreement = TermsAgreement(user_id=current_user.id,
                                           terms_id=terms_id,
                                           agreed_at=agreed_at,
                                           ip_address=agreement_data.ip_address,
                                           user_agent=agreement_data.user_agent)
            db.add(new_agreement)
            new_agreements.append((new_agreement, terms))

            # User 테이블의 약관 필드 업데이트
            if terms.type == TermsType.SERVICE:
                current_user.terms_agreement = True
            elif terms.type == TermsType.PRIVACY:
                current_user.privacy_policy = True
            elif terms.type == TermsType.PRIVACY_COLLECTION:
                current_user.privacy_collection = True
            elif terms.type == TermsType.MARKETING:
                current_user.marketing_consent = True

        # 필수 약관 3개 모두 동의했는지 확인
        all_required_agreed = (current_user.terms_agreement and current_user.privacy_policy
                               and current_user.privacy_collection)

        if all_required_agreed:
            current_user.needs_terms_agreement = False
            logger.info(f"사용자 {current_user.id}의 모든 필수 약관 동의 완료 (일괄)")

        db.commit()

        # 응답 데이터 생성
        agreement_responses = []
        for new_agreement, terms in new_agreements:
            db.refresh(new_agreement)
            agreement_responses.append(
                TermsAgreementResponse(id=new_agreement.id,
                                       user_id=new_agreement.user_id,
                                       terms_id=new_agreement.terms_id,
                                       terms_title=terms.title,
                                       agreed_at=new_agreement.agreed_at,
                                       ip_address=new_agreement.ip_address,
                                       user_agent=new_agreement.user_agent,
                                       created_at=new_agreement.created_at))

        logger.info(f"약관 일괄 동의 등록됨: {len(agreement_responses)}개 "
                    f"(사용자: {current_user.id}, 약관 IDs: {new_terms_ids})")

        return TermsAgreementBulkResponse(agreements=agreement_responses, all_required_agreed=all_required_agreed)

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"약관 일괄 동의 등록 중 오류 발생: {e}")
        raise HTTPException(status_code=500, detail="약관 일괄 동의 등록 중 오류가 발생했습니다")


async def send_terms_updated_notification(terms_id: int, db: Session):
    """약관 변경 알림 전송"""
    try:
        from utils import generate_cuid
        from services.push_delivery_service import send_push_to_user

        # 약관 정보 조회
        terms = db.query(Terms).filter(Terms.id == terms_id).first()
        if not terms:
            logger.error(f"약관을 찾을 수 없습니다: {terms_id}")
            return

        # 모든 활성 사용자 조회
        users = db.query(User).filter(User.status == 'ACTIVE', User.deleted_at.is_(None)).all()

        if not users:
            logger.info("알림을 받을 사용자가 없습니다.")
            return

        # 각 사용자에게 알림 전송
        sent_count = 0
        for user in users:
            try:
                notification = Notification(
                    user_id=user.id,
                    type=NotificationType.SYSTEM.value,  # TERMS_UPDATED는 없으므로 SYSTEM 사용
                    title=f"약관이 변경되었습니다 - {terms.title}",
                    content=f"""
서비스 약관이 변경되었습니다.

약관명: {terms.title}
변경일: {terms.published_at.strftime('%Y년 %m월 %d일 %H:%M') if terms.published_at else '알 수 없음'}
{'필수 동의 약관입니다.' if terms.is_required else ''}

새로운 약관을 확인하고 동의해주세요.
                    """.strip(),
                    status=NotificationStatus.UNREAD.value)

                db.add(notification)
                send_push_to_user(db=db,
                                  user_id=user.id,
                                  title=notification.title,
                                  content=notification.content,
                                  category="system",
                                  target_id=terms.id)
                sent_count += 1

            except Exception as e:
                logger.error(f"약관 변경 알림 생성 실패 - user_id: {user.id}, error: {str(e)}")
                continue

        db.commit()
        logger.info(f"약관 변경 알림 전송 완료 - terms_id: {terms_id}, sent_count: {sent_count}")

    except Exception as e:
        logger.error(f"약관 변경 알림 전송 실패: {e}")
        db.rollback()
        raise
