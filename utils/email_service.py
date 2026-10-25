"""
SMTP를 사용한 이메일 전송 서비스
fastapi-mail을 사용하여 Gmail SMTP로 이메일을 전송합니다.
"""
import logging
from fastapi import HTTPException, status
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig

from config import settings

logger = logging.getLogger(__name__)

# 이메일 설정을 lazy loading으로 변경 (모듈 로드 시 생성하지 않음)
_conf = None

def get_email_config() -> ConnectionConfig | None:
    """이메일 설정을 가져옵니다 (필요할 때만 생성)"""
    global _conf
    if _conf is None:
        # 디버깅: 설정 값 확인
        username_preview = settings.MAIL_USERNAME[:3] + '***' if settings.MAIL_USERNAME and len(settings.MAIL_USERNAME) > 3 else (settings.MAIL_USERNAME if settings.MAIL_USERNAME else 'None')
        logger.info(f"[이메일 설정 확인] MAIL_USERNAME: {username_preview}")
        logger.info(f"[이메일 설정 확인] MAIL_PASSWORD: {'***' if settings.MAIL_PASSWORD else 'None'}")
        logger.info(f"[이메일 설정 확인] MAIL_FROM: {settings.MAIL_FROM or '(설정되지 않음)'}")
        logger.info(f"[이메일 설정 확인] MAIL_SERVER: {settings.MAIL_SERVER}")
        logger.info(f"[이메일 설정 확인] MAIL_PORT: {settings.MAIL_PORT}")
        logger.info(f"[이메일 설정 확인] MAIL_STARTTLS: {settings.MAIL_STARTTLS}")
        logger.info(f"[이메일 설정 확인] MAIL_SSL_TLS: {settings.MAIL_SSL_TLS}")
        
        # 이메일 설정이 없으면 에러 대신 경고만 출력하고 None 반환
        if not settings.MAIL_USERNAME or not settings.MAIL_PASSWORD:
            logger.warning("이메일 설정이 없습니다. MAIL_USERNAME과 MAIL_PASSWORD를 .env 파일에 설정해주세요.")
            logger.warning("개발 환경에서는 이메일 발송이 비활성화됩니다.")
            logger.warning(f"현재 MAIL_USERNAME 값: {repr(settings.MAIL_USERNAME)}")
            logger.warning(f"현재 MAIL_PASSWORD 값: {'설정됨' if settings.MAIL_PASSWORD else 'None'}")
            return None
        
        try:
            _conf = ConnectionConfig(
                MAIL_USERNAME=settings.MAIL_USERNAME,
                MAIL_PASSWORD=settings.MAIL_PASSWORD,
                MAIL_FROM=settings.MAIL_FROM or settings.MAIL_USERNAME,
                MAIL_PORT=settings.MAIL_PORT,
                MAIL_SERVER=settings.MAIL_SERVER,
                MAIL_STARTTLS=settings.MAIL_STARTTLS,
                MAIL_SSL_TLS=settings.MAIL_SSL_TLS,
                USE_CREDENTIALS=True,
                VALIDATE_CERTS=True,
                MAIL_FROM_NAME="티업링크"
            )
            logger.info(f"[이메일 설정 완료] 서버: {settings.MAIL_SERVER}:{settings.MAIL_PORT}, FROM: {settings.MAIL_FROM or settings.MAIL_USERNAME}")
        except Exception as e:
            logger.error(f"[이메일 설정 생성 실패] {str(e)}")
            raise
    return _conf

async def send_password_reset_email(to: str, nickname: str, reset_token: str) -> bool:
    """비밀번호 재설정 이메일 발송"""
    try:
        conf = get_email_config()
        if conf is None:
            logger.warning(f"이메일 설정이 없어 비밀번호 재설정 이메일을 발송할 수 없습니다. (대상: {to})")
            logger.warning("개발 환경에서는 이메일 발송을 건너뜁니다.")
            reset_url = f"{settings.frontend_base_url}/auth/reset-password?token={reset_token}"
            logger.warning(f"비밀번호 재설정 링크: {reset_url}")
            return True  # 개발 환경에서는 성공으로 처리
        
        reset_url = f"{settings.frontend_base_url}/auth/reset-password?token={reset_token}"
        
        message = MessageSchema(
            subject="[티업링크] 비밀번호 재설정 안내",
            recipients=[to],
            body=f"""
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <style>
        body {{
            font-family: Pretendard, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            line-height: 1.7;
            color: #1f2937;
        }}
        .container {{
            max-width: 640px;
            margin: 0 auto;
            padding: 32px 24px;
        }}
        .header {{
            text-align: center;
            margin-bottom: 32px;
        }}
        .header h1 {{
            font-size: 24px;
            font-weight: 700;
            color: #10b981;
            margin: 0;
        }}
        .content {{
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            padding: 32px 28px;
            box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
        }}
        .content h2 {{
            margin-top: 0;
            font-size: 20px;
            color: #111827;
        }}
        .cta {{
            display: inline-block;
            margin: 28px 0;
            padding: 14px 32px;
            background: linear-gradient(135deg, #10b981, #059669);
            color: #fff !important;
            text-decoration: none;
            border-radius: 9999px;
            font-weight: 700;
            box-shadow: 0 12px 24px rgba(16, 185, 129, 0.25);
        }}
        .cta:hover {{
            background: linear-gradient(135deg, #059669, #047857);
        }}
        .info-box {{
            background: #f8fafc;
            border-left: 4px solid #10b981;
            padding: 16px 18px;
            margin-top: 24px;
            border-radius: 8px;
            font-size: 14px;
            color: #475569;
        }}
        .footer {{
            margin-top: 48px;
            text-align: center;
            font-size: 12px;
            color: #9ca3af;
        }}
        .small {{
            font-size: 13px;
            color: #6b7280;
        }}
        .warning {{
            background-color: #fef3c7;
            border-left: 4px solid #f59e0b;
            padding: 15px;
            margin: 20px 0;
            border-radius: 8px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>티업링크</h1>
        </div>
        <div class="content">
            <h2>비밀번호 재설정 안내</h2>
            <p>안녕하세요, <strong>{nickname}</strong>님!</p>
            <p>티업링크 비밀번호 재설정을 요청하셨습니다.</p>
            <p>아래 버튼을 클릭하여 새로운 비밀번호를 설정해주세요:</p>
            <div style="text-align: center;">
                <a href="{reset_url}" class="cta" target="_blank" rel="noopener">비밀번호 재설정하기</a>
            </div>
            <p class="small">버튼이 동작하지 않는 경우 아래 링크를 복사해서 브라우저 주소창에 붙여넣어 주세요.</p>
            <p class="small" style="word-break: break-all; background: #f1f5f9; padding: 12px 14px; border-radius: 8px;">
                {reset_url}
            </p>
            <div class="warning">
                <strong>⚠️ 주의사항:</strong>
                <ul style="margin: 12px 0 0 16px; padding: 0;">
                    <li>이 링크는 <strong>1시간 후에 만료</strong>됩니다.</li>
                    <li>보안을 위해 이 링크는 <strong>한 번만 사용</strong>할 수 있습니다.</li>
                    <li>만약 비밀번호 재설정을 요청하지 않으셨다면, 이 이메일을 무시해주세요.</li>
                </ul>
            </div>
        </div>
        <div class="footer">
            <p>감사합니다.<br>티업링크 팀</p>
        </div>
    </div>
</body>
</html>
            """,
            subtype="html"
        )
        
        fm = FastMail(conf)
        await fm.send_message(message)
        logger.info(f"[이메일 발송 성공] 비밀번호 재설정 이메일 발송 완료: {to}")
        return True
    except Exception as e:
        logger.error(f"[이메일 발송 실패] 비밀번호 재설정 이메일 전송 실패: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"이메일 전송 중 오류가 발생했습니다: {str(e)}"
        )

# 전역 이메일 서비스 함수 (기존 코드와의 호환성을 위해)
class EmailService:
    """이메일 전송 서비스 클래스 (호환성을 위해 유지)"""
    
    def __init__(self):
        self.frontend_base_url = settings.frontend_base_url
    
    async def send_password_reset_email(self, to: str, nickname: str, reset_token: str) -> bool:
        """비밀번호 재설정 이메일 전송 (비동기)"""
        return await send_password_reset_email(to, nickname, reset_token)

# 전역 이메일 서비스 인스턴스
email_service = EmailService()
