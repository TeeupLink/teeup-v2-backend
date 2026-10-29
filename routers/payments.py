from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import requests
import os
from datetime import datetime, timedelta
from utils.datetime_utils import get_kst_now
import logging
from sqlalchemy.orm import Session
from sqlalchemy import func
from config import settings
from database import get_db
from models import Payment, User
from schemas import PaymentStatus, PaymentMethod

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(tags=["payments"])
security = HTTPBearer()

# 토스페이먼츠 설정
TOSS_SECRET_KEY = settings.TOSS_PAYMENTS_SECRET_KEY
TOSS_BASE_URL = settings.TOSS_PAYMENTS_BASE_URL

class PaymentConfirmRequest(BaseModel):
    paymentKey: str
    orderId: str
    amount: int

class PaymentRequest(BaseModel):
    orderId: str
    orderName: str
    amount: int
    customerKey: str
    customerName: str
    customerEmail: str

class PaymentResponse(BaseModel):
    paymentKey: str
    orderId: str
    orderName: str
    status: str
    requestedAt: str
    approvedAt: str | None = None
    totalAmount: int
    method: str
    card: dict | None = None
    virtualAccount: dict | None = None
    transfer: dict | None = None
    mobilePhone: dict | None = None
    giftCertificate: dict | None = None
    cashReceipt: dict | None = None
    discount: dict | None = None
    cancels: list | None = None
    secret: str | None = None
    type: str
    easyPay: dict | None = None
    country: str
    failure: dict | None = None
    isPartialCancelable: bool
    receipt: dict | None = None
    checkoutUrl: str | None = None
    currency: str
    balanceAmount: int
    suppliedAmount: int
    vat: int
    taxFreeAmount: int
    taxExemptionAmount: int

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """토큰 검증 함수 (실제 구현에서는 JWT 토큰을 검증해야 함)"""
    token = credentials.credentials
    # 여기서 실제 토큰 검증 로직을 구현해야 함
    # 임시로 토큰이 있으면 통과
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증 토큰이 필요합니다."
        )
    return {"user_id": "test_user", "token": token}

@router.post("/confirm", response_model=PaymentResponse)
async def confirm_payment(
    request: PaymentConfirmRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    토스페이먼츠 결제 승인 API
    """
    try:
        logger.info(f"결제 승인 요청: {request.paymentKey}, {request.orderId}, {request.amount}")
        
        # 토스페이먼츠 결제 승인 API 호출
        import base64
        encoded_key = base64.b64encode(f"{TOSS_SECRET_KEY}:".encode()).decode()
        
        headers = {
            "Authorization": f"Basic {encoded_key}",
            "Content-Type": "application/json"
        }
        
        data = {
            "paymentKey": request.paymentKey,
            "orderId": request.orderId,
            "amount": request.amount
        }
        
        response = requests.post(
            f"{TOSS_BASE_URL}/payments/confirm",
            headers=headers,
            json=data,
            timeout=30
        )
        
        if response.status_code != 200:
            logger.error(f"토스페이먼츠 API 오류: {response.status_code}, {response.text}")
            
            # 토스페이먼츠 API 에러 응답 파싱
            try:
                error_data = response.json()
                error_message = error_data.get('message', '알 수 없는 오류가 발생했습니다.')
                error_code = error_data.get('code', 'UNKNOWN_ERROR')
            except Exception:
                error_message = response.text
                error_code = 'PARSE_ERROR'
            
            # 적절한 HTTP 상태 코드 매핑
            if response.status_code == 400:
                http_status = status.HTTP_400_BAD_REQUEST
            elif response.status_code == 401:
                http_status = status.HTTP_401_UNAUTHORIZED
            elif response.status_code == 404:
                http_status = status.HTTP_404_NOT_FOUND
            else:
                http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
            
            raise HTTPException(
                status_code=http_status,
                detail={
                    "message": f"결제 승인 실패: {error_message}",
                    "code": error_code,
                    "toss_status_code": response.status_code
                }
            )
        
        payment_data = response.json()
        logger.info(f"결제 승인 성공: {payment_data.get('paymentKey')}")
        
        # 데이터베이스에 결제 정보 저장
        try:
            # 결제 정보를 데이터베이스에 저장
            payment_record = Payment(
                user_id=current_user.id,
                amount=payment_data.get('totalAmount', 0),
                currency=payment_data.get('currency', 'KRW'),
                status=PaymentStatus.SUCCEEDED if payment_data.get('status') == 'DONE' else PaymentStatus.PENDING,
                payment_provider="TOSS_PAYMENTS",
                provider_payment_id=payment_data.get('paymentKey', ''),
                payment_method=PaymentMethod.TOSS_PAY,  # 토스페이먼츠 사용
                payment_key=payment_data.get('paymentKey'),
                order_id=payment_data.get('orderId'),
                order_name=payment_data.get('orderName'),
                transaction_id=payment_data.get('transactionKey'),
                paid_at=datetime.fromisoformat(payment_data.get('approvedAt').replace('Z', '+00:00')) if payment_data.get('approvedAt') else None,
                description=f"토스페이먼츠 결제 - {payment_data.get('orderName', '')}",
                extra_data=payment_data  # 전체 응답 데이터 저장
            )
            
            db.add(payment_record)
            db.commit()
            db.refresh(payment_record)
            
            logger.info(f"결제 정보 데이터베이스 저장 완료: {payment_record.id}")
            
            # PaymentResponse 생성 시 uuid 포함            
        except Exception as db_error:
            logger.error(f"결제 정보 데이터베이스 저장 실패: {str(db_error)}")
            # 결제는 성공했지만 DB 저장 실패 시에도 결제 응답은 반환
            # 실제 운영에서는 이 경우 별도 처리 필요
        
        return PaymentResponse(**payment_data)
        
    except requests.exceptions.RequestException as e:
        logger.error(f"토스페이먼츠 API 요청 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="결제 서버와의 통신 중 오류가 발생했습니다."
        )
    except Exception as e:
        logger.error(f"결제 승인 처리 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="결제 처리 중 오류가 발생했습니다."
        )

@router.get("/{payment_key}")
async def get_payment(
    payment_key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    결제 정보 조회 API
    """
    try:
        logger.info(f"결제 정보 조회: {payment_key}")
        
        headers = {
            "Authorization": f"Basic {TOSS_SECRET_KEY}",
            "Content-Type": "application/json"
        }
        
        response = requests.get(
            f"{TOSS_BASE_URL}/payments/{payment_key}",
            headers=headers,
            timeout=30
        )
        
        if response.status_code != 200:
            logger.error(f"토스페이먼츠 API 오류: {response.status_code}, {response.text}")
            
            # 토스페이먼츠 API 에러 응답 파싱
            try:
                error_data = response.json()
                error_message = error_data.get('message', '알 수 없는 오류가 발생했습니다.')
                error_code = error_data.get('code', 'UNKNOWN_ERROR')
            except Exception:
                error_message = response.text
                error_code = 'PARSE_ERROR'
            
            # 적절한 HTTP 상태 코드 매핑
            if response.status_code == 400:
                http_status = status.HTTP_400_BAD_REQUEST
            elif response.status_code == 401:
                http_status = status.HTTP_401_UNAUTHORIZED
            elif response.status_code == 404:
                http_status = status.HTTP_404_NOT_FOUND
            else:
                http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
            
            raise HTTPException(
                status_code=http_status,
                detail={
                    "message": f"결제 정보 조회 실패: {error_message}",
                    "code": error_code,
                    "toss_status_code": response.status_code
                }
            )
        
        payment_data = response.json()
        
        # 데이터베이스에서도 결제 정보 조회 (로컬 데이터와 비교)
        try:
            local_payment = db.query(Payment).filter(
                Payment.payment_key == payment_key,
                Payment.user_id == current_user.id
            ).first()
            
            if local_payment:
                # 로컬 데이터와 토스페이먼츠 데이터를 결합하여 반환
                combined_data = {
                    **payment_data,                    "local_id": local_payment.id,
                    "local_status": local_payment.status.value if local_payment.status else None,
                    "local_created_at": local_payment.created_at.isoformat() if local_payment.created_at else None,
                    "local_updated_at": local_payment.updated_at.isoformat() if local_payment.updated_at else None
                }
                return PaymentResponse(**combined_data)
            else:
                logger.warning(f"결제 정보 조회: 로컬 데이터베이스에서 결제 정보를 찾을 수 없음 - {payment_key}")
                
        except Exception as db_error:
            logger.error(f"결제 정보 조회: 데이터베이스 조회 실패: {str(db_error)}")
        
        return PaymentResponse(**payment_data)
        
    except requests.exceptions.RequestException as e:
        logger.error(f"토스페이먼츠 API 요청 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="결제 서버와의 통신 중 오류가 발생했습니다."
        )
    except Exception as e:
        logger.error(f"결제 정보 조회 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="결제 정보 조회 중 오류가 발생했습니다."
        )

@router.post("/cancel")
async def cancel_payment(
    payment_key: str,
    cancel_reason: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    결제 취소 API
    """
    try:
        logger.info(f"결제 취소 요청: {payment_key}, 사유: {cancel_reason}")
        
        headers = {
            "Authorization": f"Basic {TOSS_SECRET_KEY}",
            "Content-Type": "application/json"
        }
        
        data = {
            "cancelReason": cancel_reason
        }
        
        response = requests.post(
            f"{TOSS_BASE_URL}/payments/{payment_key}/cancel",
            headers=headers,
            json=data,
            timeout=30
        )
        
        if response.status_code != 200:
            logger.error(f"토스페이먼츠 API 오류: {response.status_code}, {response.text}")
            
            # 토스페이먼츠 API 에러 응답 파싱
            try:
                error_data = response.json()
                error_message = error_data.get('message', '알 수 없는 오류가 발생했습니다.')
                error_code = error_data.get('code', 'UNKNOWN_ERROR')
            except Exception:
                error_message = response.text
                error_code = 'PARSE_ERROR'
            
            # 적절한 HTTP 상태 코드 매핑
            if response.status_code == 400:
                http_status = status.HTTP_400_BAD_REQUEST
            elif response.status_code == 401:
                http_status = status.HTTP_401_UNAUTHORIZED
            elif response.status_code == 404:
                http_status = status.HTTP_404_NOT_FOUND
            else:
                http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
            
            raise HTTPException(
                status_code=http_status,
                detail={
                    "message": f"결제 취소 실패: {error_message}",
                    "code": error_code,
                    "toss_status_code": response.status_code
                }
            )
        
        cancel_data = response.json()
        logger.info(f"결제 취소 성공: {payment_key}")
        
        # 데이터베이스에서 결제 정보 업데이트
        try:
            payment_record = db.query(Payment).filter(
                Payment.payment_key == payment_key,
                Payment.user_id == current_user.id
            ).first()
            
            if payment_record:
                payment_record.status = PaymentStatus.CANCELED
                payment_record.canceled_at = get_kst_now()
                payment_record.cancel_reason = cancel_reason
                payment_record.updated_at = get_kst_now()
                
                db.commit()
                logger.info(f"결제 취소 정보 데이터베이스 업데이트 완료: {payment_record.id}")
            else:
                logger.warning(f"결제 취소: 데이터베이스에서 결제 정보를 찾을 수 없음 - {payment_key}")
                
        except Exception as db_error:
            logger.error(f"결제 취소 정보 데이터베이스 업데이트 실패: {str(db_error)}")
            # 취소는 성공했지만 DB 업데이트 실패 시에도 취소 응답은 반환
        
        return cancel_data
        
    except requests.exceptions.RequestException as e:
        logger.error(f"토스페이먼츠 API 요청 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="결제 서버와의 통신 중 오류가 발생했습니다."
        )
    except Exception as e:
        logger.error(f"결제 취소 처리 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="결제 취소 중 오류가 발생했습니다."
        )

@router.get("/")
async def get_payment_list(
    start_date: str | None = None,
    end_date: str | None = None,
    status_filter: str | None = None,
    payment_method_filter: str | None = None,
    page: int = 1,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    결제 내역 조회 API
    """
    try:
        from models import Payment, PaymentStatus, PaymentMethod
        from datetime import datetime
        from sqlalchemy import and_, or_
        
        logger.info(f"결제 내역 조회 시작 - user_id: {current_user.id}")
        
        # 페이지네이션 계산
        offset = (page - 1) * limit
        
        # 기본 쿼리 - 사용자별 결제 내역만 조회
        query = db.query(Payment).filter(Payment.user_id == current_user.id)
        
        # 날짜 필터링
        if start_date:
            try:
                start_datetime = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                query = query.filter(Payment.created_at >= start_datetime)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="시작 날짜 형식이 올바르지 않습니다. (YYYY-MM-DD 형식 사용)"
                )
        
        if end_date:
            try:
                end_datetime = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                query = query.filter(Payment.created_at <= end_datetime)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="종료 날짜 형식이 올바르지 않습니다. (YYYY-MM-DD 형식 사용)"
                )
        
        # 상태 필터링
        if status_filter:
            try:
                status_enum = PaymentStatus(status_filter)
                query = query.filter(Payment.status == status_enum)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"올바르지 않은 결제 상태입니다. 사용 가능한 상태: {[s.value for s in PaymentStatus]}"
                )
        
        # 결제 수단 필터링
        if payment_method_filter:
            try:
                method_enum = PaymentMethod(payment_method_filter)
                query = query.filter(Payment.payment_method == method_enum)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"올바르지 않은 결제 수단입니다. 사용 가능한 수단: {[m.value for m in PaymentMethod]}"
                )
        
        # 총 개수 조회
        total = query.count()
        
        # 결제 내역 조회 (최신순)
        payments = query.order_by(Payment.created_at.desc()).offset(offset).limit(limit).all()
        
        # 응답 데이터 구성
        payment_list = []
        for payment in payments:
            payment_data = {
                "id": payment.id,                "user_id": payment.user_id,
                "subscription_id": payment.subscription_id,
                "amount": float(payment.amount) if payment.amount else 0.0,
                "currency": payment.currency,
                "status": payment.status.value if payment.status else None,
                "payment_method": payment.payment_method.value if payment.payment_method else None,
                "payment_key": payment.payment_key,
                "order_id": payment.order_id,
                "order_name": payment.order_name,
                "transaction_id": payment.transaction_id,
                "paid_at": payment.paid_at,
                "canceled_at": payment.canceled_at,
                "cancel_reason": payment.cancel_reason,
                "description": payment.description,
                "extra_data": payment.extra_data,
                "created_at": payment.created_at,
                "updated_at": payment.updated_at
            }
            payment_list.append(payment_data)
        
        # 통계 정보 계산
        total_amount = db.query(Payment).filter(
            Payment.user_id == current_user.id,
            Payment.status == PaymentStatus.SUCCEEDED
        ).with_entities(func.sum(Payment.amount)).scalar() or 0
        
        successful_payments = db.query(Payment).filter(
            Payment.user_id == current_user.id,
            Payment.status == PaymentStatus.SUCCEEDED
        ).count()
        
        failed_payments = db.query(Payment).filter(
            Payment.user_id == current_user.id,
            Payment.status == PaymentStatus.FAILED
        ).count()
        
        pending_payments = db.query(Payment).filter(
            Payment.user_id == current_user.id,
            Payment.status == PaymentStatus.PENDING
        ).count()
        
        total_pages = (total + limit - 1) // limit
        
        logger.info(f"결제 내역 조회 완료 - 총 {len(payment_list)}개")
        
        return {
            "payments": payment_list,
            "pagination": {
                "total": total,
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_prev": page > 1
            },
            "statistics": {
                "total_amount": float(total_amount),
                "successful_payments": successful_payments,
                "failed_payments": failed_payments,
                "pending_payments": pending_payments,
                "total_payments": total
            },
            "filters": {
                "start_date": start_date,
                "end_date": end_date,
                "status": status_filter,
                "payment_method": payment_method_filter
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"결제 내역 조회 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="결제 내역 조회 중 오류가 발생했습니다."
        )

@router.get("/statistics")
async def get_payment_statistics(
    start_date: str | None = None,
    end_date: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    결제 통계 조회 API
    """
    try:
        from datetime import datetime, timedelta
        
        logger.info(f"결제 통계 조회 시작 - user_id: {current_user.id}")
        
        # 기본 쿼리
        query = db.query(Payment).filter(Payment.user_id == current_user.id)
        
        # 날짜 필터링
        if start_date:
            try:
                start_datetime = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                query = query.filter(Payment.created_at >= start_datetime)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="시작 날짜 형식이 올바르지 않습니다."
                )
        
        if end_date:
            try:
                end_datetime = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                query = query.filter(Payment.created_at <= end_datetime)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="종료 날짜 형식이 올바르지 않습니다."
                )
        
        # 전체 통계
        total_payments = query.count()
        total_amount = query.filter(Payment.status == PaymentStatus.SUCCEEDED).with_entities(func.sum(Payment.amount)).scalar() or 0
        
        # 상태별 통계
        status_stats = {}
        for payment_status in PaymentStatus:
            count = query.filter(Payment.status == payment_status).count()
            amount = query.filter(Payment.status == payment_status).with_entities(func.sum(Payment.amount)).scalar() or 0
            status_stats[payment_status.value] = {
                "count": count,
                "amount": float(amount)
            }
        
        # 결제 수단별 통계
        method_stats = {}
        for method in PaymentMethod:
            count = query.filter(Payment.payment_method == method).count()
            amount = query.filter(Payment.payment_method == method).with_entities(func.sum(Payment.amount)).scalar() or 0
            method_stats[method.value] = {
                "count": count,
                "amount": float(amount)
            }
        
        # 월별 통계 (최근 12개월)
        monthly_stats = []
        for i in range(12):
            month_start = get_kst_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=30*i)
            month_end = month_start + timedelta(days=32)
            month_end = month_end.replace(day=1) - timedelta(days=1)
            
            monthly_payments = query.filter(
                Payment.created_at >= month_start,
                Payment.created_at <= month_end
            ).count()
            
            monthly_amount = query.filter(
                Payment.created_at >= month_start,
                Payment.created_at <= month_end,
                Payment.status == PaymentStatus.SUCCEEDED
            ).with_entities(func.sum(Payment.amount)).scalar() or 0
            
            monthly_stats.append({
                "month": month_start.strftime("%Y-%m"),
                "count": monthly_payments,
                "amount": float(monthly_amount)
            })
        
        monthly_stats.reverse()  # 최신순으로 정렬
        
        logger.info(f"결제 통계 조회 완료 - user_id: {current_user.id}")
        
        return {
            "summary": {
                "total_payments": total_payments,
                "total_amount": float(total_amount),
                "average_amount": float(total_amount / total_payments) if total_payments > 0 else 0
            },
            "status_breakdown": status_stats,
            "method_breakdown": method_stats,
            "monthly_trend": monthly_stats,
            "period": {
                "start_date": start_date,
                "end_date": end_date
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"결제 통계 조회 오류: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="결제 통계 조회 중 오류가 발생했습니다."
        )