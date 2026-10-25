"""
팀 관리 API 라우터

골프 라운딩 팀 편성 및 관리 기능
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_
from typing import Optional
# import uuid  # generate_cuid 사용으로 변경
import logging

# 로깅 설정
logger = logging.getLogger(__name__)

from database import get_db
from models import (Team, TeamMember, MeetingParticipant, Meeting, User, MeetingResult, UserScoreHistory, Guest)
from schemas import (TeamFormationMode, TeamStatus, ClubRole)
from schemas import (TeamCreate, TeamUpdate, TeamResponse, TeamMemberResponse, TeamFormationRequest,
                     TeamFormationResponse, MessageResponse)
from utils.team_formation import TeamFormationEngine
from utils.handicap_calculator import calculate_handicap_from_average_score
from routers.auth import get_current_user
from routers.meetings.workflow import (
    is_meeting_time_started,
    sync_rounding_completion_if_due,
    reset_team_formation_confirmation,
)

router = APIRouter(prefix="/teams", tags=["teams"])


def check_team_management_permission(user_id: int, meeting: Meeting, db: Session) -> bool:
    """팀 관리 권한 확인 (리더/매니저만 가능)"""
    try:
        from models import ClubMembership, ClubRole

        print(f"권한 확인 시작 - user_id: {user_id}, club_id: {meeting.club_id}")

        membership = db.query(ClubMembership).filter(ClubMembership.club_id == meeting.club_id,
                                                     ClubMembership.user_id == user_id,
                                                     ClubMembership.role.in_([ClubRole.LEADER,
                                                                              ClubRole.MANAGER])).first()

        print(f"멤버십 조회 결과: {membership is not None}")
        if membership:
            print(f"멤버십 역할: {membership.role}")

        return membership is not None
    except Exception as e:
        print(f"권한 확인 중 오류: {str(e)}")
        return False


@router.get("/", response_model=list[TeamResponse])
async def get_teams(meeting_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """팀 목록 조회"""
    try:
        print(f"팀 목록 조회 시작 - meeting_id: {meeting_id}")

        # 모임 존재 확인
        print(f"모임 조회 중 - meeting_id: {meeting_id}")
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        print(f"모임 조회 결과: {meeting is not None}")

        if not meeting:
            print(f"모임을 찾을 수 없음 - meeting_id: {meeting_id}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다")

        # 프라이빗 라운딩인 경우 권한 체크
        if meeting.is_private:
            user_id = current_user.get('id') if isinstance(current_user, dict) else current_user.id
            # 참가자 또는 생성자인지 확인
            is_participant = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id,
                                                                 MeetingParticipant.user_id == user_id).first()

            is_creator = meeting.created_by == user_id
            is_manager_or_leader = check_team_management_permission(user_id, meeting, db)

            if not is_participant and not is_creator and not is_manager_or_leader:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                    detail="프라이빗 라운딩의 팀 정보는 참가자/생성자/클럽 리더·매니저만 조회할 수 있습니다.")

        print(f"모임 정보 - id: {meeting.id}, name: {meeting.name}")
    except HTTPException:
        raise
    except Exception as e:
        print(f"모임 조회 중 오류 발생: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 오류: {str(e)}")

    try:
        # 팀 목록 조회
        logger.info(f"팀 목록 조회 중 - meeting_id: {meeting_id}")
        teams = db.query(Team).filter(Team.meeting_id == meeting_id).all()
        logger.info(f"팀 조회 결과 - 팀 수: {len(teams)}")

        team_responses = []
        for i, team in enumerate(teams):
            logger.info(f"팀 {i+1} 처리 중 - team_id: {team.id}, name: {team.name}")
            try:
                # 팀 멤버 정보 조회
                logger.info(f"팀 멤버 조회 중 - team_id: {team.id}")
                members = []
                team_members = db.query(TeamMember).filter(TeamMember.team_id == team.id).all()
                logger.info(f"팀 멤버 조회 결과 - 멤버 수: {len(team_members)}")

                for j, team_member in enumerate(team_members):
                    logger.info(f"멤버 {j+1} 처리 중 - team_member_id: {team_member.id}")

                    # user_id 또는 guest_id를 통해 MeetingParticipant 조회
                    if team_member.user_id:
                        participant = db.query(MeetingParticipant).filter(
                            MeetingParticipant.meeting_id == meeting_id,
                            or_((MeetingParticipant.user_id == team_member.user_id) if team_member.user_id else False,
                                (MeetingParticipant.guest_id == team_member.guest_id)
                                if team_member.guest_id else False)).first()
                    elif team_member.guest_id:
                        participant = db.query(MeetingParticipant).filter(
                            MeetingParticipant.meeting_id == meeting_id,
                            MeetingParticipant.guest_id == team_member.guest_id).first()
                    else:
                        participant = None

                    if participant:
                        logger.info(f"참가자 조회 성공 - participant_id: {participant.id}")
                        # 게스트인 경우와 멤버인 경우 구분
                        gender = None
                        handicap = None
                        average_score = None

                        if participant.guest_id:
                            # guest_id를 통해 Guest 모델에서 정보 조회
                            guest = db.query(Guest).filter(Guest.id == participant.guest_id).first()
                            if guest:
                                user_name = guest.name or "게스트"
                                user_nickname = guest.name or "게스트"
                                gender = guest.gender.value if guest.gender else None
                                handicap = int(guest.handicap) if guest.handicap else None
                            else:
                                user_name = "게스트"
                                user_nickname = "게스트"
                                gender = None
                                handicap = None
                        else:
                            if participant.user_id:
                                user = db.query(User).filter(User.id == participant.user_id).first()
                                if user:
                                    logger.info(f"사용자 조회 성공 - user_id: {user.id}, nickname: {user.nickname}")
                                    user_name = user.realname or user.nickname or "이름 없음"
                                    user_nickname = user.nickname or "닉네임 없음"
                                    gender = user.gender.value if user.gender else None

                                    # 핸디캡 우선순위: participant.handicap → user.handicap → user.handicap_init → user.average_score(공통 계산 유틸)
                                    if participant.handicap is not None:
                                        handicap = participant.handicap
                                    elif user.handicap is not None:
                                        handicap = int(user.handicap)
                                    elif user.handicap is not None:
                                        handicap = int(user.handicap)
                                    elif user.handicap_init is not None:
                                        handicap = int(user.handicap_init)
                                    elif user.average_score is not None:
                                        handicap = calculate_handicap_from_average_score(user.average_score)
                                    else:
                                        handicap = None

                                    # MeetingResult의 직전 경기 id를 찾은 뒤 UserScoreHistory.gross_score 조회
                                    average_score = None
                                    last_result = db.query(MeetingResult).filter(
                                        MeetingResult.user_id == participant.user_id,
                                        MeetingResult.meeting_id != meeting_id  # 현재 모임 제외
                                    ).order_by(MeetingResult.completed_at.desc()).first()

                                    if last_result:
                                        last_score = db.query(UserScoreHistory).filter(
                                            UserScoreHistory.user_id == participant.user_id,
                                            UserScoreHistory.meeting_id == last_result.meeting_id
                                        ).order_by(UserScoreHistory.played_at.desc()).first()
                                        if last_score:
                                            average_score = last_score.gross_score
                                else:
                                    logger.warning(f"사용자 조회 실패 - user_id: {participant.user_id}")
                                    continue
                            else:
                                logger.warning(f"참가자에 user_id와 guest_id가 모두 없음 - participant_id: {participant.id}")
                                continue

                        member_response = TeamMemberResponse(id=team_member.id,
                                                             team_id=team_member.team_id,
                                                             user_id=team_member.user_id,
                                                             guest_id=team_member.guest_id,
                                                             user_name=user_name,
                                                             user_nickname=user_nickname,
                                                             order=team_member.order,
                                                             gender=gender,
                                                             handicap=handicap,
                                                             average_score=average_score,
                                                             created_at=team_member.created_at)
                        members.append(member_response)
                    else:
                        logger.warning(f"참가자 조회 실패 - user_id: {team_member.user_id}, guest_id: {team_member.guest_id}")
            except Exception as e:
                logger.error(f"팀 멤버 처리 중 오류: {str(e)}")
                raise

            logger.info(f"팀 응답 객체 생성 중 - team_id: {team.id}")
            team_response = TeamResponse(id=team.id,
                                         name=team.name,
                                         meeting_id=team.meeting_id,
                                         meeting_name=meeting.name,
                                         formation_mode=team.formation_mode,
                                         status=team.status,
                                         total_handicap=float(team.total_handicap) if team.total_handicap else None,
                                         tee_off_order=team.tee_off_order,
                                         formation_notes=team.formation_notes,
                                         member_count=len(members),
                                         members=members,
                                         created_at=team.created_at,
                                         updated_at=team.updated_at)
            team_responses.append(team_response)
            logger.info(f"팀 응답 객체 생성 완료 - team_id: {team.id}")

    except Exception as e:
        logger.error(f"팀 목록 처리 중 오류: {str(e)}")
        raise

    logger.info(f"팀 목록 조회 완료 - 총 팀 수: {len(team_responses)}")
    return team_responses


@router.post("/", response_model=TeamResponse)
async def create_team(team_data: TeamCreate,
                      meeting_id: int,
                      db: Session = Depends(get_db),
                      current_user: dict = Depends(get_current_user)):
    """팀 생성"""
    print("=== 팀 생성 엔드포인트 호출됨 ===")
    print(f"meeting_id: {meeting_id}")
    print(f"team_data: {team_data}")
    print(f"current_user: {current_user}")

    try:
        print(f"팀 생성 시작 - meeting_id: {meeting_id}, user_id: {current_user.get('id')}")

        # 모임 존재 확인
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            print(f"모임을 찾을 수 없음: {meeting_id}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다")

        if sync_rounding_completion_if_due(meeting):
            db.commit()
            db.refresh(meeting)

        if is_meeting_time_started(meeting.meeting_time):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="티업시간 이후에는 팀 편성을 수정할 수 없습니다")

        print(f"모임 찾음: {meeting.name}")

        # 권한 확인 (임시 비활성화)
        if not check_team_management_permission(current_user.get('id'), meeting, db):
            print(f"권한 없음: user_id={current_user.get('id')}, meeting_id={meeting_id}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="팀 관리는 리더/매니저만 가능합니다")

        print("권한 확인 통과 (임시 비활성화)")

        # 팀 생성
        import secrets
        import string

        # 직접 짧은 ID 생성 (25자 이내)
        chars = string.ascii_lowercase + string.digits
        team_id = ''.join(secrets.choice(chars) for _ in range(20))
        print(f"생성된 팀 ID: {team_id} (길이: {len(team_id)})")
        print(f"ID가 UUID인지 확인: {'-' in team_id}")

        team = Team(id=team_id,
                    name=team_data.name,
                    meeting_id=meeting_id,
                    formation_mode=team_data.formation_mode,
                    status=TeamStatus.DRAFT,
                    formation_notes=team_data.formation_notes)

        print(f"팀 객체 생성: {team.name}, ID: {team.id}, ID 길이: {len(team.id)}")
        print(f"생성된 ID 타입: {type(team.id)}")
        print(f"객체 ID가 UUID인지 확인: {'-' in team.id}")

        db.add(team)
        reset_team_formation_confirmation(meeting, db)
        db.commit()
        db.refresh(team)

        print(f"팀 저장 완료: {team.id}")

    except HTTPException:
        raise
    except Exception as e:
        print(f"팀 생성 중 오류: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"서버 오류: {str(e)}")

    # 응답 생성
    team_response = TeamResponse(id=team.id,
                                 name=team.name,
                                 meeting_id=team.meeting_id,
                                 meeting_name=meeting.name,
                                 formation_mode=team.formation_mode,
                                 status=team.status,
                                 total_handicap=float(team.total_handicap) if team.total_handicap else None,
                                 tee_off_order=team.tee_off_order,
                                 formation_notes=team.formation_notes,
                                 member_count=0,
                                 members=[],
                                 created_at=team.created_at,
                                 updated_at=team.updated_at)

    return team_response


@router.get("/{team_id}", response_model=TeamResponse)
async def get_team(team_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """팀 상세 조회"""
    logger.info(f"팀 상세 조회 시작 - team_id: {team_id}, current_user: {current_user}")
    user_id = current_user.get('user_id') if isinstance(current_user, dict) else getattr(current_user, 'id', None)
    logger.info(f"user_id: {user_id}")

    try:
        # 팀 조회
        logger.info(f"팀 조회 중 - team_id: {team_id}")
        team = db.query(Team).options(joinedload(Team.meeting)).filter(Team.id == team_id).first()
        logger.info(f"팀 조회 결과: {team is not None}")

        if not team:
            logger.warning(f"팀을 찾을 수 없음 - team_id: {team_id}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="팀을 찾을 수 없습니다")

        logger.info(f"팀 정보 - id: {team.id}, name: {team.name}, meeting_id: {team.meeting_id}")
    except Exception as e:
        logger.error(f"팀 조회 중 오류 발생: {str(e)}")
        import traceback
        logger.error(f"스택 트레이스: {traceback.format_exc()}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"팀 조회 중 오류가 발생했습니다: {str(e)}")

    # 멤버 정보 구성
    members = []
    team_members = db.query(TeamMember).filter(TeamMember.team_id == team.id).all()

    for team_member in team_members:
        # user_id를 통해 MeetingParticipant 조회
        participant = db.query(MeetingParticipant).filter(
            MeetingParticipant.meeting_id == team.meeting_id,
            or_((MeetingParticipant.user_id == team_member.user_id) if team_member.user_id else False,
                (MeetingParticipant.guest_id == team_member.guest_id) if team_member.guest_id else False)).first()

        if not participant:
            continue

        # 게스트인 경우와 멤버인 경우 구분
        if participant.guest_id:
            # guest_id를 통해 Guest 모델에서 정보 조회
            guest = db.query(Guest).filter(Guest.id == participant.guest_id).first()
            if guest:
                user_name = guest.name or "게스트"
                user_nickname = guest.name or "게스트"
            else:
                user_name = "게스트"
                user_nickname = "게스트"
        else:
            if participant.user_id:
                user = participant.user
                if not user:
                    continue
                user_name = user.realname or user.nickname or "이름 없음"
                user_nickname = user.nickname or "닉네임 없음"
            else:
                continue

        member_response = TeamMemberResponse(id=team_member.id,
                                             team_id=team_member.team_id,
                                             user_id=team_member.user_id,
                                             guest_id=team_member.guest_id,
                                             user_name=user_name,
                                             user_nickname=user_nickname,
                                             order=team_member.order,
                                             created_at=team_member.created_at)
        members.append(member_response)

    team_response = TeamResponse(id=team.id,
                                 name=team.name,
                                 meeting_id=team.meeting_id,
                                 meeting_name=team.meeting.name,
                                 formation_mode=team.formation_mode,
                                 status=team.status,
                                 total_handicap=float(team.total_handicap) if team.total_handicap else None,
                                 tee_off_order=team.tee_off_order,
                                 formation_notes=team.formation_notes,
                                 member_count=len(members),
                                 members=members,
                                 created_at=team.created_at,
                                 updated_at=team.updated_at)

    return team_response


@router.put("/{team_id}", response_model=TeamResponse)
async def update_team(team_id: int,
                      team_data: TeamUpdate,
                      db: Session = Depends(get_db),
                      current_user: dict = Depends(get_current_user)):
    """팀 정보 수정"""
    # 팀 조회
    team = db.query(Team).options(joinedload(Team.meeting)).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="팀을 찾을 수 없습니다")

    if sync_rounding_completion_if_due(team.meeting):
        db.commit()
        db.refresh(team)

    if is_meeting_time_started(team.meeting.meeting_time):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="티업시간 이후에는 팀 편성을 수정할 수 없습니다")

    # 권한 확인
    if not check_team_management_permission(current_user["user_id"], team.meeting, db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="팀 관리는 리더/매니저만 가능합니다")

    # 팀 정보 업데이트
    update_data = team_data.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(team, field, value)

    reset_team_formation_confirmation(team.meeting, db)
    db.commit()
    db.refresh(team)

    # 멤버 정보 조회
    members = []
    for team_member in team.members:
        # user_id를 통해 MeetingParticipant 조회
        participant = db.query(MeetingParticipant).filter(
            MeetingParticipant.meeting_id == team.meeting_id,
            or_((MeetingParticipant.user_id == team_member.user_id) if team_member.user_id else False,
                (MeetingParticipant.guest_id == team_member.guest_id) if team_member.guest_id else False)).first()

        if not participant:
            continue

        # 게스트인 경우와 멤버인 경우 구분
        if participant.guest_id:
            # guest_id를 통해 Guest 모델에서 정보 조회
            guest = db.query(Guest).filter(Guest.id == participant.guest_id).first()
            if guest:
                user_name = guest.name or "게스트"
                user_nickname = guest.name or "게스트"
            else:
                user_name = "게스트"
                user_nickname = "게스트"
        else:
            if participant.user_id:
                user = participant.user
                if not user:
                    continue
                user_name = user.realname or user.nickname or "이름 없음"
                user_nickname = user.nickname or "닉네임 없음"
            else:
                continue

        member_response = TeamMemberResponse(id=team_member.id,
                                             team_id=team_member.team_id,
                                             user_id=team_member.user_id,
                                             guest_id=team_member.guest_id,
                                             user_name=user_name,
                                             user_nickname=user_nickname,
                                             order=team_member.order,
                                             created_at=team_member.created_at)
        members.append(member_response)

    team_response = TeamResponse(id=team.id,
                                 name=team.name,
                                 meeting_id=team.meeting_id,
                                 meeting_name=team.meeting.name,
                                 formation_mode=team.formation_mode,
                                 status=team.status,
                                 total_handicap=float(team.total_handicap) if team.total_handicap else None,
                                 tee_off_order=team.tee_off_order,
                                 formation_notes=team.formation_notes,
                                 member_count=len(members),
                                 members=members,
                                 created_at=team.created_at,
                                 updated_at=team.updated_at)

    return team_response


@router.delete("/{team_id}", response_model=MessageResponse)
async def delete_team(team_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """팀 삭제"""
    try:
        logger.info(f"팀 삭제 시작 - team_id: {team_id}, user_id: {current_user.get('user_id')}")

        # 팀 조회
        team = db.query(Team).options(joinedload(Team.meeting)).filter(Team.id == team_id).first()
        if not team:
            logger.warning(f"팀을 찾을 수 없음 - team_id: {team_id}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="팀을 찾을 수 없습니다")

        if sync_rounding_completion_if_due(team.meeting):
            db.commit()
            db.refresh(team)

        if is_meeting_time_started(team.meeting.meeting_time):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="티업시간 이후에는 팀 편성을 수정할 수 없습니다")

        logger.info(f"팀 정보 - id: {team.id}, name: {team.name}, meeting_id: {team.meeting_id}")

        # 권한 확인
        if not check_team_management_permission(current_user["user_id"], team.meeting, db):
            logger.warning(f"팀 삭제 권한 없음 - team_id: {team_id}, user_id: {current_user.get('user_id')}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="팀 관리는 리더/매니저만 가능합니다")

        # 팀 멤버 수 확인
        member_count = db.query(TeamMember).filter(TeamMember.team_id == team_id).count()
        logger.info(f"팀 멤버 수: {member_count}")

        # 팀 삭제 (CASCADE로 멤버들도 자동 삭제됨)
        db.delete(team)
        reset_team_formation_confirmation(team.meeting, db)
        db.commit()

        logger.info(f"팀 삭제 완료 - team_id: {team_id}")

        return MessageResponse(message="팀이 성공적으로 삭제되었습니다", success=True)

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"팀 삭제 중 오류 발생 - team_id: {team_id}, error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"팀 삭제 중 오류가 발생했습니다: {str(e)}")


@router.post("/auto-formation", response_model=TeamFormationResponse)
async def auto_form_teams(meeting_id: int,
                          formation_request: TeamFormationRequest,
                          db: Session = Depends(get_db),
                          current_user: dict = Depends(get_current_user)):
    """자동 팀 편성"""
    # 모임 존재 확인
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="모임을 찾을 수 없습니다")

    # 권한 확인 (임시 비활성화)
    # if not check_team_management_permission(current_user["user_id"], meeting, db):
    #     raise HTTPException(
    #         status_code=status.HTTP_403_FORBIDDEN,
    #         detail="팀 관리는 리더/매니저만 가능합니다"
    #     )

    # 참가자들 조회
    participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).options(
        joinedload(MeetingParticipant.user)).all()

    if len(participants) < 2:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="팀 편성을 위해서는 최소 2명의 참가자가 필요합니다")

    # 팀 편성 실행
    formation_engine = TeamFormationEngine(db)
    teams = formation_engine.create_teams_from_formation(meeting_id, formation_request, participants)

    # 편성 결과 조회 및 응답 생성
    team_responses = []
    for team in teams:
        members = []
        for team_member in team.members:
            # user_id를 통해 MeetingParticipant 조회
            participant = db.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == meeting_id,
                or_((MeetingParticipant.user_id == team_member.user_id) if team_member.user_id else False,
                    (MeetingParticipant.guest_id == team_member.guest_id) if team_member.guest_id else False)).first()

            if not participant:
                continue

            # 게스트인 경우와 멤버인 경우 구분
            gender = None
            handicap = None
            average_score = None

            if participant.guest_id:
                # guest_id를 통해 Guest 모델에서 정보 조회
                if participant.guest_id:
                    guest = db.query(Guest).filter(Guest.id == participant.guest_id).first()
                    if guest:
                        user_name = guest.name or "게스트"
                        user_nickname = guest.name or "게스트"
                        gender = guest.gender.value if guest.gender else None
                        handicap = int(guest.handicap) if guest.handicap else None
                    else:
                        # 하위 호환성: guest 필드 사용
                        user_name = participant.guest_name or "게스트"
                        user_nickname = participant.guest_name or "게스트"
                        gender = participant.guest_gender.value if participant.guest_gender else None
                        handicap = int(participant.guest_handicap) if participant.guest_handicap else None
                else:
                    # 하위 호환성: guest 필드 사용
                    user_name = participant.guest_name or "게스트"
                    user_nickname = participant.guest_name or "게스트"
                    gender = participant.guest_gender.value if participant.guest_gender else None
                    handicap = int(participant.guest_handicap) if participant.guest_handicap else None
            else:
                # 멤버인 경우
                if participant.user_id:
                    user = participant.user
                    if not user:
                        continue
                    user_name = user.realname or user.nickname or "이름 없음"
                    user_nickname = user.nickname or "닉네임 없음"
                    gender = user.gender.value if user.gender else None
                    handicap = participant.handicap

                    # MeetingResult의 직전 경기 id를 찾은 뒤 UserScoreHistory.gross_score 조회
                    last_result = db.query(MeetingResult).filter(
                        MeetingResult.user_id == participant.user_id,
                        MeetingResult.meeting_id != meeting_id  # 현재 모임 제외
                    ).order_by(MeetingResult.completed_at.desc()).first()

                    if last_result:
                        last_score = db.query(UserScoreHistory).filter(
                            UserScoreHistory.user_id == participant.user_id,
                            UserScoreHistory.meeting_id == last_result.meeting_id
                        ).order_by(UserScoreHistory.played_at.desc()).first()
                        if last_score:
                            average_score = last_score.gross_score
                else:
                    continue

            member_response = TeamMemberResponse(id=team_member.id,
                                                 team_id=team.id,
                                                 user_id=team_member.user_id,
                                                 guest_id=team_member.guest_id,
                                                 user_name=user_name,
                                                 user_nickname=user_nickname,
                                                 order=team_member.order,
                                                 gender=gender,
                                                 handicap=handicap,
                                                 average_score=average_score,
                                                 created_at=team_member.created_at)
            members.append(member_response)

        team_response = TeamResponse(
            id=team.id,
            name=team.name,
            meeting_id=team.meeting_id,
            formation_mode=TeamFormationMode(team.formation_mode) if team.formation_mode else None,
            status=TeamStatus(team.status) if team.status else TeamStatus.DRAFT,
            formation_notes=team.formation_notes,
            member_count=len(members),
            members=members,
            created_at=team.created_at,
            updated_at=team.updated_at)
        team_responses.append(team_response)

    # 편성 요약 생성
    formation_summary = formation_engine.get_formation_summary(teams)

    return TeamFormationResponse(teams=team_responses,
                                 total_teams=len(teams),
                                 unassigned_participants=[],
                                 total_participants=len(participants),
                                 formation_summary=formation_summary)
