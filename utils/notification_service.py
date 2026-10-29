"""
알림 서비스 유틸리티
다양한 이벤트에 대한 알림 생성 및 전송 기능
"""
from sqlalchemy.orm import Session
from models import Notification, NotificationType, NotificationStatus, User, Meeting, MeetingParticipant
from datetime import datetime
import logging
from services.push_delivery_service import send_push_to_user

logger = logging.getLogger(__name__)


def _is_meeting_recruitment_closed(meeting: Meeting) -> bool:
    """모집 완료 단계(조기 마감 또는 신청 마감일 경과) 여부 확인"""
    deadline_passed = meeting.application_deadline is not None and datetime.now() > meeting.application_deadline
    return bool(meeting.application_closed_early or deadline_passed)


def notify_organizer_participant_added_after_recruitment_closed(
    db: Session,
    meeting_id: int,
    participant_user_id: int | None = None,
    participant_guest_name: str | None = None,
) -> Notification | None:
    """
    모집 완료 단계에서 참가자가 추가되면 모임 organizer(created_by)에게 알림 전송
    """
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        return None

    if not _is_meeting_recruitment_closed(meeting):
        return None

    organizer_user_id = meeting.created_by
    if organizer_user_id is None:
        logger.info(f"organizer 알림 생략 - organizer(created_by) 없음, meeting_id: {meeting_id}")
        return None

    if participant_user_id is not None and participant_user_id == organizer_user_id:
        return None

    participant_label = "새 참가자"
    if participant_user_id is not None:
        participant_user = db.query(User).filter(User.id == participant_user_id).first()
        if participant_user:
            participant_label = participant_user.nickname or participant_user.realname or f"user_id={participant_user_id}"
        else:
            participant_label = f"user_id={participant_user_id}"
    elif participant_guest_name:
        participant_label = f"{participant_guest_name}(게스트)"

    title = f"모집 마감 후 참가자 추가 - {meeting.name}"
    content = f"모임 '{meeting.name}'에 모집 마감 이후 참가자 '{participant_label}'이(가) 추가되었습니다."

    return create_notification(
        db=db,
        user_id=organizer_user_id,
        notification_type=NotificationType.MEETING_REMINDER,
        title=title,
        content=content,
        category="meeting",
        target_id=meeting_id,
    )


def _status_to_label(status_value: str | None) -> str:
    if status_value == "SCHEDULED":
        return "모집중"
    if status_value == "IN_PROGRESS":
        return "진행중"
    if status_value == "COMPLETED":
        return "완료"
    if status_value == "CANCELED":
        return "취소"
    return status_value or "알 수 없음"


def notify_round_participants_status_changed(
    db: Session,
    meeting_id: int,
    previous_status: str | None,
    current_status: str | None,
) -> int:
    """라운딩 상태 변경 시 참가자 전원에게 알림 전송"""
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        return 0

    meeting_type_value = meeting.meeting_type.value if hasattr(meeting.meeting_type, "value") else str(meeting.meeting_type)
    if meeting_type_value != "ROUND":
        return 0

    prev_status_value = previous_status.value if hasattr(previous_status, "value") else previous_status
    curr_status_value = current_status.value if hasattr(current_status, "value") else current_status
    if prev_status_value == curr_status_value:
        return 0

    participants = db.query(MeetingParticipant).filter(
        MeetingParticipant.meeting_id == meeting_id,
        MeetingParticipant.user_id.isnot(None),
    ).all()
    if not participants:
        return 0

    title = f"라운딩 상태 변경 - {meeting.name}"
    content = (
        f"라운딩 '{meeting.name}' 상태가 "
        f"'{_status_to_label(prev_status_value)}'에서 '{_status_to_label(curr_status_value)}'(으)로 변경되었습니다."
    )

    sent_count = 0
    for participant in participants:
        try:
            create_notification(
                db=db,
                user_id=participant.user_id,
                notification_type=NotificationType.MEETING_REMINDER,
                title=title,
                content=content,
                category="meeting",
                target_id=meeting_id,
                extra_data={
                    "meeting_type": "round",
                    "previous_status": prev_status_value or "",
                    "current_status": curr_status_value or "",
                },
            )
            sent_count += 1
        except Exception as e:
            logger.error(
                f"라운딩 상태 변경 알림 전송 실패 - meeting_id: {meeting_id}, user_id: {participant.user_id}, error: {str(e)}"
            )

    logger.info(
        f"라운딩 상태 변경 알림 전송 완료 - meeting_id: {meeting_id}, previous_status: {prev_status_value}, current_status: {curr_status_value}, sent_count: {sent_count}"
    )
    return sent_count


def create_notification(
    db: Session,
    user_id: int,
    notification_type: NotificationType,
    title: str,
    content: str,
    category: str = "system",
    target_id: int = None,
    extra_data: dict = None,
) -> Notification:
    """알림 생성"""
    try:
        notification = Notification(
            user_id=user_id,
            type=notification_type.value if hasattr(notification_type, 'value') else str(notification_type),
            title=title,
            content=content,
            status=NotificationStatus.UNREAD.value if hasattr(NotificationStatus.UNREAD, 'value') else str(NotificationStatus.UNREAD)
        )
        
        db.add(notification)
        db.commit()
        db.refresh(notification)

        try:
            push_sent_count = send_push_to_user(db=db,
                                                user_id=user_id,
                                                title=title,
                                                content=content,
                                                category=category,
                                                target_id=target_id,
                                                extra_data=extra_data)
            logger.info(f"푸시 전송 완료 - user_id: {user_id}, sent_count: {push_sent_count}")
        except Exception as push_error:
            logger.error(f"푸시 전송 실패 - user_id: {user_id}, error: {str(push_error)}")

        logger.info(f"알림 생성 완료 - user_id: {user_id}, type: {notification_type}, title: {title}")
        return notification
        
    except Exception as e:
        logger.error(f"알림 생성 실패 - user_id: {user_id}, error: {str(e)}")
        db.rollback()
        raise

def create_meeting_notification(
    db: Session,
    user_id: int,
    meeting_name: str,
    club_name: str,
    notification_type: str,  # "CREATED", "UPDATED", "CANCELED", "PARTICIPANT_STATUS_CHANGED"
    meeting_id: int = None,
    extra_info: str = None
):
    """모임 관련 알림"""
    if notification_type == "CREATED":
        title = f"새 모임이 등록되었습니다"
        content = f"소속 클럽 '{club_name}'에 새 모임 '{meeting_name}'이 등록되었습니다."
    elif notification_type == "UPDATED":
        title = f"모임 정보가 변경되었습니다"
        content = f"'{club_name}' 클럽의 모임 '{meeting_name}' 정보가 변경되었습니다."
    elif notification_type == "CANCELED":
        title = f"모임이 취소되었습니다"
        content = f"'{club_name}' 클럽의 모임 '{meeting_name}'이 취소되었습니다."
        if extra_info:
            content += f"\n취소 사유: {extra_info}"
    elif notification_type == "PARTICIPANT_STATUS_CHANGED":
        title = f"모임 참가 상태가 변경되었습니다"
        content = f"'{club_name}' 클럽의 모임 '{meeting_name}' 참가 상태가 변경되었습니다."
        if extra_info:
            content += f"\n변경 내용: {extra_info}"
    else:
        title = f"모임 알림"
        content = f"'{club_name}' 클럽의 모임 '{meeting_name}' 관련 알림입니다."
    
    return create_notification(
        db=db,
        user_id=user_id,
        notification_type=NotificationType.MEETING_REMINDER,
        title=title,
        content=content,
        category="meeting",
        target_id=meeting_id,
    )

def create_social_notification(
    db: Session,
    user_id: int,
    social_name: str,
    club_name: str,
    notification_type: str,  # "CREATED", "UPDATED", "CANCELED", "PARTICIPANT_STATUS_CHANGED"
    meeting_id: int = None,
    extra_info: str = None
):
    """소셜 모임 관련 알림 (이전 create_event_notification)"""
    if notification_type == "CREATED":
        title = f"새 소셜 모임이 등록되었습니다"
        content = f"소속 클럽 '{club_name}'에 새 소셜 모임 '{social_name}'이 등록되었습니다."
    elif notification_type == "UPDATED":
        title = f"소셜 모임 정보가 변경되었습니다"
        content = f"'{club_name}' 클럽의 소셜 모임 '{social_name}' 정보가 변경되었습니다."
    elif notification_type == "CANCELED":
        title = f"소셜 모임이 취소되었습니다"
        content = f"'{club_name}' 클럽의 소셜 모임 '{social_name}'이 취소되었습니다."
        if extra_info:
            content += f"\n취소 사유: {extra_info}"
    elif notification_type == "PARTICIPANT_STATUS_CHANGED":
        title = f"소셜 모임 참가 상태가 변경되었습니다"
        content = f"'{club_name}' 클럽의 소셜 모임 '{social_name}' 참가 상태가 변경되었습니다."
        if extra_info:
            content += f"\n변경 내용: {extra_info}"
    else:
        title = f"소셜 모임 알림"
        content = f"'{club_name}' 클럽의 소셜 모임 '{social_name}' 관련 알림입니다."
    
    return create_notification(
        db=db,
        user_id=user_id,
        notification_type=NotificationType.OTHER,
        title=title,
        content=content,
        category="meeting",
        target_id=meeting_id,
        extra_data={"meeting_type": "social"},
    )

def create_notice_notification(
    db: Session,
    user_id: int,
    notice_title: str,
    club_name: str = None,
    club_id: int = None,
    notice_id: int = None
):
    """공지사항 알림"""
    if club_name:
        title = f"새 공지사항이 등록되었습니다"
        content = f"소속 클럽 '{club_name}'에 새 공지사항 '{notice_title}'이 등록되었습니다."
    else:
        title = f"새 공지사항이 등록되었습니다"
        content = f"새 공지사항 '{notice_title}'이 등록되었습니다."
    
    return create_notification(
        db=db,
        user_id=user_id,
        notification_type=NotificationType.NEW_NOTICE,
        title=title,
        content=content,
        category="notice",
        target_id=notice_id,
        extra_data={"club_id": str(club_id)} if club_id is not None else None,
    )

def create_inquiry_response_notification(
    db: Session,
    user_id: int,
    inquiry_title: str,
    response_content: str
):
    """문의 답변 알림"""
    title = f"문의에 답변이 등록되었습니다"
    content = f"문의 '{inquiry_title}'에 답변이 등록되었습니다.\n\n답변 내용: {response_content[:100]}{'...' if len(response_content) > 100 else ''}"
    
    return create_notification(
        db=db,
        user_id=user_id,
        notification_type=NotificationType.SYSTEM,
        title=title,
        content=content,
        category="system",
    )

def create_admin_notification(
    db: Session,
    notification_type: str,
    title: str,
    content: str,
    link: str = None,
):
    """관리자 알림 벨로 보낸다 (utils.admin_notify).

    예전 구현은 관리자 id 를 회원 id 로 써서 회원 알림(notifications)을 만들고 푸시까지 보냈다 —
    번호가 같은 회원이 관리자용 알림을 받았다. db 인자는 호환을 위해 남겨 두고 쓰지 않는다.
    """
    from utils import admin_notify

    return admin_notify.notify(notification_type, title, content, link)


def create_inquiry_admin_notification(
    db: Session,
    inquiry_title: str,
    inquiry_type: str,
    user_name: str,
    inquiry_id: int
):
    """새 문의 등록 관리자 알림"""
    return create_admin_notification(
        db=db,
        notification_type="INQUIRY",
        title="새 문의가 등록되었습니다",
        content=f"{user_name}님 · {inquiry_title}",
        link=f"/inquiries/{inquiry_id}",
    )

def create_club_membership_notification(
    db: Session,
    user_id: int,
    club_name: str,
    status: str,  # "ACTIVE" or "APPROVED" (호환성) or "REJECTED"
    rejection_reason: str = None,
    club_id: int = None
):
    """클럽 가입 승인/거절 알림"""
    if status == "ACTIVE" or status == "APPROVED":
        title = f"클럽 가입이 승인되었습니다"
        content = f"축하합니다! '{club_name}' 클럽 가입이 승인되었습니다."
        notification_type = NotificationType.CLUB_MEMBERSHIP_APPROVED
    else:  # REJECTED
        title = f"클럽 가입이 거절되었습니다"
        content = f"죄송합니다. '{club_name}' 클럽 가입이 거절되었습니다."
        if rejection_reason:
            content += f"\n\n거절 사유: {rejection_reason}"
        notification_type = NotificationType.CLUB_MEMBERSHIP_REJECTED
    
    return create_notification(
        db=db,
        user_id=user_id,
        notification_type=notification_type,
        title=title,
        content=content,
        category="club",
        target_id=club_id,
    )

def create_club_membership_request_notification(
    db: Session,
    leader_manager_ids: list,
    club_name: str,
    applicant_name: str,
    club_id: int
):
    """클럽 새 회원 가입신청 알림 (리더/매니저용)"""
    title = f"새 회원 가입 신청이 있습니다"
    content = f"'{applicant_name}'님이 '{club_name}' 클럽 가입을 신청했습니다. 승인/거절을 처리해주세요."
    
    notifications = []
    for user_id in leader_manager_ids:
        notification = create_notification(
            db=db,
            user_id=user_id,
            notification_type=NotificationType.CLUB_MEMBERSHIP_REQUEST,
            title=title,
            content=content,
            category="club",
            target_id=club_id,
        )
        notifications.append(notification)
    
    logger.info(f"클럽 가입 신청 알림 생성 완료 - {len(notifications)}명에게 전송")
    return notifications

def create_team_formation_completed_notification(
    db: Session,
    user_id: int,
    meeting_name: str,
    club_name: str,
    meeting_id: int
):
    """팀편성 완료 알림"""
    try:
        title = f"팀 편성이 완료되었습니다"
        content = f"'{club_name}' 클럽의 라운딩 모임 '{meeting_name}' 팀 편성이 완료되었습니다. 팀 편성 결과를 확인해보세요."
        
        notification = create_notification(
            db=db,
            user_id=user_id,
            notification_type=NotificationType.TEAM_FORMATION_COMPLETED,
            title=title,
            content=content,
            category="meeting",
            target_id=meeting_id,
            extra_data={"meeting_type": "rounding"},
        )
        
        logger.info(f"팀 편성 완료 알림 생성 성공 - user_id: {user_id}, meeting_id: {meeting_id}")
        return notification
        
    except Exception as e:
        logger.error(f"팀 편성 완료 알림 생성 실패 - user_id: {user_id}, meeting_id: {meeting_id}, error: {str(e)}", exc_info=True)
        raise

def create_social_settlement_completed_notification(
    db: Session,
    user_id: int,
    meeting_name: str,
    club_name: str,
    meeting_id: int
):
    """소셜모임 정산 완료 알림"""
    title = f"소셜 모임 정산이 완료되었습니다"
    content = f"'{club_name}' 클럽의 소셜 모임 '{meeting_name}' 정산이 완료되었습니다. 정산 내역을 확인해보세요."
    
    return create_notification(
        db=db,
        user_id=user_id,
        notification_type=NotificationType.SOCIAL_SETTLEMENT_COMPLETED,
        title=title,
        content=content,
        category="meeting",
        target_id=meeting_id,
        extra_data={"meeting_type": "social"},
    )
