"""
골프 라운딩 팀 편성 알고리즘

실무 기준 10가지 편성 원칙을 반영한 팀 편성 시스템
지그재그(Zigzag/Snake Draft) 분배 방식으로 공정한 팀 편성 구현
"""
import math
import random
from typing import Any
from decimal import Decimal
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import desc
from models import MeetingParticipant, Team, TeamMember, User, Gender, MeetingResult, UserStatus, Guest
from schemas import TeamFormationMode, TeamStatus
from schemas import TeamFormationRequest, TeamFormationResponse, TeamResponse, TeamMemberResponse
from utils.handicap_calculator import calculate_handicap_from_average_score, get_user_handicap_for_formation
from utils.datetime_utils import get_kst_date
from utils.cuid import generate_cuid


def _is_guest_participant(participant: MeetingParticipant) -> bool:
    return bool(getattr(participant, "guest_id", None)) or bool(getattr(participant, "is_guest", False))


class TeamFormationEngine:
    """팀 편성 엔진"""

    def __init__(self, db: Session):
        self.db = db

    def classify_by_handicap(self, participants: list[MeetingParticipant]) -> dict[str, list[MeetingParticipant]]:
        """핸디캡 기준으로 참가자를 버킷화"""
        buckets = {
            'A': [],  # ≤9 (상급)
            'B': [],  # 10-17 (중급)
            'C': [],  # 18-25 (중하급)
            'D': []  # ≥26 (하급)
        }

        for participant in participants:
            handicap = float(participant.handicap or 0)
            if handicap <= 9:
                buckets['A'].append(participant)
            elif handicap <= 17:
                buckets['B'].append(participant)
            elif handicap <= 25:
                buckets['C'].append(participant)
            else:
                buckets['D'].append(participant)

        return buckets

    def calculate_team_score(self,
                             team_members: list[MeetingParticipant],
                             formation_mode: TeamFormationMode,
                             preferences_weight: float = 0.3) -> float:
        """팀 편성 점수 계산"""
        if not team_members:
            return 0.0

        # 1. 실력 균형 점수 (0-100)
        handicaps = [float(m.handicap or 0) for m in team_members]
        handicap_std = math.sqrt(sum((h - sum(handicaps) / len(handicaps))**2 for h in handicaps) / len(handicaps))
        balance_score = max(0, 100 - handicap_std * 2)  # 표준편차가 낮을수록 높은 점수

        # 2. 페이스 적합도 점수 (0-100)
        pace_scores = {'빠름': 3, '보통': 2, '느림': 1}
        pace_variance = len(set(m.pace_preference or '보통' for m in team_members))
        pace_score = max(0, 100 - pace_variance * 20)  # 페이스가 비슷할수록 높은 점수

        # 3. 신입/베테랑 균형 점수 (BEGINNER_FRIENDLY 모드)
        beginner_score = 100
        if formation_mode == TeamFormationMode.BEGINNER_FRIENDLY:
            newbies = [m for m in team_members if m.is_newbie]
            veterans = [m for m in team_members if not m.is_newbie]
            if len(newbies) > 0 and len(veterans) == 0:
                beginner_score = 0  # 신입만 있는 팀은 점수 없음
            elif len(newbies) > 0 and len(veterans) > 0:
                beginner_score = 100  # 신입과 베테랑이 함께 있으면 만점

        # 4. 선호도 충족 점수
        preference_score = 0
        for member in team_members:
            if member.prefer_with:
                for preferred_user_id in member.prefer_with:
                    if any(m.user_id == preferred_user_id for m in team_members):
                        preference_score += 20  # 선호도 충족 시 가점

        # 5. 기피 조합 회피 점수
        avoid_penalty = 0
        for member in team_members:
            if member.avoid_with:
                for avoided_user_id in member.avoid_with:
                    if any(m.user_id == avoided_user_id for m in team_members):
                        avoid_penalty += 50  # 기피 조합 시 큰 감점

        # 가중치 적용
        total_score = (balance_score * 0.4 + pace_score * 0.2 + beginner_score * 0.2 + preference_score * 0.1 +
                       (100 - avoid_penalty) * 0.1)

        return max(0, min(100, total_score))

    # =============================================================================
    # [DEPRECATED] 기존 버킷 방식 편성 함수들 (더 이상 사용되지 않음)
    # 새로운 지그재그 분배 방식을 사용하려면 `form_teams_by_mode()` 함수를 사용하세요.
    # =============================================================================

    # def form_balanced_teams(self, participants: List[MeetingParticipant],
    #                       max_team_size: int = 4) -> List[List[MeetingParticipant]]:
    #     """
    #     밸런스 모드 팀 편성
    #
    #     [DEPRECATED] 이 함수는 더 이상 사용되지 않습니다.
    #     새로운 지그재그 분배 방식을 사용하려면 `form_teams_by_mode()` 함수를 사용하세요.
    #     """
    #     if len(participants) < 2:
    #         return []
    #
    #     buckets = self.classify_by_handicap(participants)
    #     teams = []
    #
    #     # A-B-C-D 조합 우선 생성
    #     while (len(buckets['A']) > 0 or len(buckets['B']) > 0 or
    #            len(buckets['C']) > 0 or len(buckets['D']) > 0):
    #
    #         team = []
    #         bucket_order = ['A', 'B', 'C', 'D']
    #
    #         # 각 버킷에서 1명씩 선택
    #         for bucket in bucket_order:
    #             if buckets[bucket] and len(team) < max_team_size:
    #                 # 가장 적합한 멤버 선택 (랜덤 + 점수 고려)
    #                 best_member = random.choice(buckets[bucket][:3])  # 상위 3명 중 랜덤
    #                 team.append(best_member)
    #                 buckets[bucket].remove(best_member)
    #
    #         # 팀이 완성되지 않았으면 남은 버킷에서 채우기
    #         if len(team) < max_team_size:
    #             remaining = []
    #             for bucket in ['A', 'B', 'C', 'D']:
    #                 remaining.extend(buckets[bucket])
    #
    #             while len(team) < max_team_size and remaining:
    #                 member = remaining.pop(0)
    #                 team.append(member)
    #
    #         if len(team) >= 2:  # 최소 2명 이상
    #             teams.append(team)
    #
    #     return teams

    # def form_competitive_teams(self, participants: List[MeetingParticipant],
    #                          max_team_size: int = 4) -> List[List[MeetingParticipant]]:
    #     """
    #     경쟁 모드 팀 편성 (비슷한 실력끼리)
    #
    #     [DEPRECATED] 이 함수는 더 이상 사용되지 않습니다.
    #     새로운 지그재그 분배 방식을 사용하려면 `form_teams_by_mode()` 함수를 사용하세요.
    #     """
    #     if len(participants) < 2:
    #         return []
    #
    #     # 핸디캡 순으로 정렬
    #     sorted_participants = sorted(participants,
    #                                key=lambda p: float(p.handicap or 0))
    #
    #     teams = []
    #     i = 0
    #
    #     while i < len(sorted_participants):
    #         team = []
    #         # 비슷한 핸디캡 범위 내에서 팀 구성 (±2~3타)
    #         base_handicap = float(sorted_participants[i].handicap or 0)
    #
    #         for j in range(i, len(sorted_participants)):
    #             if len(team) >= max_team_size:
    #                 break
    #             participant = sorted_participants[j]
    #             handicap = float(participant.handicap or 0)
    #
    #             if abs(handicap - base_handicap) <= 3:  # ±3타 이내
    #                 team.append(participant)
    #
    #         if len(team) >= 2:
    #             teams.append(team)
    #             # 사용된 참가자들을 리스트에서 제거
    #             for member in team:
    #                 sorted_participants.remove(member)
    #         else:
    #             i += 1
    #
    #     return teams

    # def form_beginner_friendly_teams(self, participants: List[MeetingParticipant],
    #                                max_team_size: int = 4) -> List[List[MeetingParticipant]]:
    #     """
    #     신입 친화 모드 팀 편성
    #
    #     [DEPRECATED] 이 함수는 더 이상 사용되지 않습니다.
    #     새로운 지그재그 분배 방식을 사용하려면 `form_teams_by_mode()` 함수를 사용하세요.
    #     """
    #     if len(participants) < 2:
    #         return []
    #
    #     newbies = [p for p in participants if p.is_newbie]
    #     veterans = [p for p in participants if not p.is_newbie]
    #
    #     teams = []
    #
    #     # 신입과 베테랑을 매칭
    #     for newbie in newbies:
    #         if not veterans:
    #             break
    #
    #         # 가장 적합한 베테랑 선택 (핸디캡이 비슷한)
    #         best_veteran = min(veterans,
    #                          key=lambda v: abs(float(v.handicap or 0) - float(newbie.handicap or 0)))
    #
    #         team = [newbie, best_veteran]
    #         veterans.remove(best_veteran)
    #
    #         # 팀을 4명까지 채우기
    #         remaining = [p for p in participants if p not in team and p not in newbies]
    #         while len(team) < max_team_size and remaining:
    #             team.append(remaining.pop(0))
    #
    #         teams.append(team)
    #
    #     # 남은 베테랑들로 팀 구성
    #     while len(veterans) >= 2:
    #         team = []
    #         while len(team) < max_team_size and veterans:
    #             team.append(veterans.pop(0))
    #         teams.append(team)
    #
    #     return teams

    # def form_speed_priority_teams(self, participants: List[MeetingParticipant],
    #                             max_team_size: int = 4) -> List[List[MeetingParticipant]]:
    #     """
    #     페이스 우선 모드 팀 편성
    #
    #     [DEPRECATED] 이 함수는 더 이상 사용되지 않습니다.
    #     새로운 지그재그 분배 방식을 사용하려면 `form_teams_by_mode()` 함수를 사용하세요.
    #     """
    #     if len(participants) < 2:
    #         return []
    #
    #     # 페이스별로 그룹핑
    #     pace_groups = {'빠름': [], '보통': [], '느림': []}
    #     for p in participants:
    #         pace = p.pace_preference or '보통'
    #         pace_groups[pace].append(p)
    #
    #     teams = []
    #
    #     # 빠른 그룹부터 팀 구성
    #     for pace in ['빠름', '보통', '느림']:
    #         group = pace_groups[pace]
    #         while len(group) >= 2:
    #             team = []
    #             while len(team) < max_team_size and group:
    #                 team.append(group.pop(0))
    #             teams.append(team)
    #
    #     return teams

    # def form_random_teams(self, participants: List[MeetingParticipant],
    #                      max_team_size: int = 4) -> List[List[MeetingParticipant]]:
    #     """랜덤 모드 팀 편성"""
    #     if len(participants) < 2:
    #         return []
    #
    #     shuffled = participants.copy()
    #     random.shuffle(shuffled)
    #
    #     teams = []
    #     i = 0
    #
    #     while i < len(shuffled):
    #         team = shuffled[i:i + max_team_size]
    #         if len(team) >= 2:
    #             teams.append(team)
    #         i += max_team_size
    #
    #     return teams

    def create_teams_from_formation(self, meeting_id: int, formation_request: TeamFormationRequest,
                                    participants: list[MeetingParticipant]) -> list[Team]:
        """
        편성 요청에 따라 팀 생성 (새로운 2단계 선택 방식)
        
        새로운 지그재그 분배 방식을 사용하여 팀을 편성합니다.
        """
        # 기존 팀들 삭제 (team_members 먼저 삭제)
        existing_teams = self.db.query(Team).filter(Team.meeting_id == meeting_id).all()
        team_ids = [team.id for team in existing_teams]
        if team_ids:
            # 관련 team_members 먼저 삭제
            self.db.query(TeamMember).filter(TeamMember.team_id.in_(team_ids)).delete(synchronize_session=False)
            # 팀 삭제
            self.db.query(Team).filter(Team.id.in_(team_ids)).delete(synchronize_session=False)
        self.db.commit()

        # 게스트 처리 (formation_request에 게스트가 있는 경우)
        if formation_request.guests:
            # 같은 파일에 있는 함수이므로 직접 호출
            for guest_data in formation_request.guests:
                guest_handicap = guest_data.handicap
                if guest_handicap is None and guest_data.average_score:
                    guest_handicap = calculate_guest_handicap(guest_data.average_score)
                elif guest_handicap is None:
                    # 기본값 사용
                    guest_handicap = Decimal("36.0")

                # 성별 변환 (문자열 -> Gender Enum)
                guest_gender_enum = None
                if guest_data.gender:
                    try:
                        guest_gender_enum = Gender(guest_data.gender)
                    except ValueError:
                        guest_gender_enum = None

                guest_participant = add_guest_to_meeting(meeting_id=meeting_id,
                                                         guest_name=guest_data.name,
                                                         guest_handicap=guest_handicap,
                                                         guest_birthdate=guest_data.birthdate,
                                                         guest_gender=guest_gender_enum,
                                                         db=self.db)
                participants.append(guest_participant)

        # 새로운 2단계 선택 방식으로 팀 편성
        # 같은 파일에 있는 함수이므로 직접 호출
        team_groups = form_teams_by_mode(participants=participants,
                                         formation_mode=formation_request.formation_mode,
                                         db=self.db,
                                         max_team_size=formation_request.team_size)

        teams = []

        for i, team_members in enumerate(team_groups):
            # 팀 생성
            team = Team(
                name=f"팀 {i+1}",
                meeting_id=meeting_id,
                formation_mode=formation_request.formation_mode.value,  # Enum 값을 문자열로 변환
                status=TeamStatus.DRAFT,
                formation_notes=formation_request.preferences.get("notes") if formation_request.preferences else None)

            # 팀 합계 핸디캡 계산 (게스트 포함)
            total_handicap = 0.0
            for member in team_members:
                if _is_guest_participant(member):
                    # 게스트는 guest_id를 통해 Guest 모델에서 핸디캡 조회
                    if member.guest_id:
                        guest = self.db.query(Guest).filter(Guest.id == member.guest_id).first()
                        if guest and guest.handicap:
                            total_handicap += float(guest.handicap)
                    # 하위 호환성: guest_handicap 필드도 확인
                    else:
                        guest_handicap = getattr(member, "guest_handicap", None)
                        if guest_handicap:
                            total_handicap += float(guest_handicap)
                else:
                    # 멤버는 get_user_handicap_for_formation 사용
                    if member.user_id:
                        handicap = get_user_handicap_for_formation(self.db, member.user_id)
                        if handicap:
                            total_handicap += float(handicap)
                        elif member.handicap:
                            total_handicap += float(member.handicap)

            team.total_handicap = Decimal(str(round(total_handicap, 1)))

            self.db.add(team)
            self.db.flush()  # ID 생성

            # 팀 멤버 추가
            for j, member in enumerate(team_members):
                # 게스트인 경우 guest_id, 일반 사용자인 경우 user_id 사용
                team_member = TeamMember(team_id=team.id,
                                         user_id=member.user_id if not _is_guest_participant(member) else None,
                                         guest_id=member.guest_id if _is_guest_participant(member) else None,
                                         order=j + 1)
                self.db.add(team_member)

            teams.append(team)

        self.db.commit()
        return teams

    def get_formation_summary(self, teams: list[Team]) -> dict[str, Any]:
        """편성 요약 정보 생성"""
        if not teams:
            return {}

        total_participants = sum(len(team.members) for team in teams)
        handicaps = []
        paces = {'빠름': 0, '보통': 0, '느림': 0}

        for team in teams:
            for member in team.members:
                # TeamMember에서 user_id를 통해 MeetingParticipant 조회
                participant = self.db.query(MeetingParticipant).filter(
                    MeetingParticipant.meeting_id == team.meeting_id,
                    MeetingParticipant.user_id == member.user_id).first()

                if participant and participant.handicap:
                    handicaps.append(float(participant.handicap))
                pace = (participant.pace_preference if participant and participant.pace_preference else '보통')
                paces[pace] = paces.get(pace, 0) + 1

        avg_handicap = sum(handicaps) / len(handicaps) if handicaps else 0

        return {
            "total_teams": len(teams),
            "total_participants": total_participants,
            "average_handicap": round(avg_handicap, 1),
            "handicap_range": f"{min(handicaps):.1f} - {max(handicaps):.1f}" if handicaps else "N/A",
            "pace_distribution": paces,
            "formation_quality": "우수" if len(teams) > 0 else "보통"
        }


# =============================================================================
# Phase 3: 핵심 알고리즘 구현 (지그재그 분배 방식)
# =============================================================================


def calculate_team_distribution(total_participants: int, max_team_size: int = 4) -> dict[str, Any]:
    """
    4인조/3인조 처리 로직
    
    총 팀 개수와 나머지 인원을 계산하여 팀 분배 정보를 반환합니다.
    실제 지그재그 분배 결과와 일치하도록 계산합니다.
    
    Args:
        total_participants: 총 참가자 수
        max_team_size: 최대 팀 크기 (기본값: 4)
    
    Returns:
        {
            "team_count": 총 팀 개수,
            "remainder": 나머지 인원 (N % 4),
            "full_teams": 4인조 팀 개수,
            "partial_teams": 3인조 팀 개수
        }
    """
    team_count = math.ceil(total_participants / max_team_size)
    remainder = total_participants % max_team_size

    if remainder == 0:
        # 정확히 max_team_size의 배수인 경우: 모두 max_team_size인조
        full_teams = team_count
        partial_teams = 0
    elif remainder == 1:
        # 나머지가 1명인 경우: 모두 3인조로 재분배
        # 예: 9명 → 3인조 3팀, 13명 → 3인조 3팀 + 4인조 1팀
        if max_team_size == 4:
            if team_count >= 3:
                # 3팀 이상인 경우: 3인조 3팀 + 나머지 4인조
                full_teams = team_count - 3
                partial_teams = 3
            else:
                # 3팀 미만인 경우: 모두 3인조
                full_teams = 0
                partial_teams = team_count
        else:
            # 3인조 모드: 마지막 팀에 1명 추가 (4인조가 됨)
            full_teams = 1
            partial_teams = team_count - 1
    elif remainder == 2:
        # 나머지가 2명인 경우
        if max_team_size == 4:
            if team_count == 2:
                # 6명: 4인조 1팀 + 2인조 1팀
                full_teams = 1
                partial_teams = 1
            else:
                # 10명 이상: 4인조 (team_count-2)팀 + 3인조 2팀
                full_teams = team_count - 2
                partial_teams = 2
        else:
            # 3인조 모드: 마지막 팀에 2명 추가 (5인조가 됨) 또는 2인조 1팀
            if team_count == 2:
                full_teams = 0
                partial_teams = 2
            else:
                full_teams = 1
                partial_teams = team_count - 1
    else:  # remainder == 3
        # 나머지가 3명인 경우
        if max_team_size == 4:
            # 4인조 모드: 마지막 팀이 3인조
            full_teams = team_count - 1
            partial_teams = 1
        else:
            # 3인조 모드: 정확히 3의 배수이므로 모두 3인조
            full_teams = 0
            partial_teams = team_count

    return {
        "team_count": team_count,
        "remainder": remainder,
        "full_teams": max(0, full_teams),
        "partial_teams": max(0, partial_teams)
    }


def zigzag_distribute(participants: list[MeetingParticipant],
                      team_count: int,
                      max_team_size: int = 4) -> list[list[MeetingParticipant]]:
    """
    지그재그(Zigzag/Snake Draft) 분배 함수
    
    정렬된 참가자 리스트를 지그재그 패턴으로 팀에 배정합니다.
    - Round 1: T1→T2→T3→T4 (정방향)
    - Round 2: T4→T3→T2→T1 (역방향)
    - 반복 패턴 적용
    
    특별 케이스: 6명을 2팀으로 나눌 때는 4인조 1팀 + 2인조 1팀으로 분배
    
    Args:
        participants: 정렬된 참가자 리스트
        team_count: 팀 개수
        max_team_size: 최대 팀 크기 (기본값: 4)
    
    Returns:
        팀별 참가자 리스트 (List[List[MeetingParticipant]])
    """
    if not participants or team_count <= 0:
        return []

    # 특별 케이스: 6명을 2팀으로 나눌 때는 4인조 1팀 + 2인조 1팀
    if len(participants) == 6 and team_count == 2 and max_team_size == 4:
        return [
            participants[:4],  # 팀1: 4인조
            participants[4:]  # 팀2: 2인조
        ]

    # 팀 초기화
    teams = [[] for _ in range(team_count)]

    # 지그재그 패턴으로 배정
    participant_index = 0
    round_number = 0

    while participant_index < len(participants):
        # 정방향 (Round 1, 3, 5, ...)
        if round_number % 2 == 0:
            for team_idx in range(team_count):
                if participant_index >= len(participants):
                    break
                if len(teams[team_idx]) < max_team_size:
                    teams[team_idx].append(participants[participant_index])
                    participant_index += 1
        # 역방향 (Round 2, 4, 6, ...)
        else:
            for team_idx in range(team_count - 1, -1, -1):
                if participant_index >= len(participants):
                    break
                if len(teams[team_idx]) < max_team_size:
                    teams[team_idx].append(participants[participant_index])
                    participant_index += 1

        round_number += 1

        # 모든 팀이 가득 찬 경우 종료
        if all(len(team) >= max_team_size for team in teams):
            # 남은 참가자는 마지막 팀부터 역순으로 배정
            while participant_index < len(participants):
                for team_idx in range(team_count - 1, -1, -1):
                    if participant_index >= len(participants):
                        break
                    teams[team_idx].append(participants[participant_index])
                    participant_index += 1
            break

    # 빈 팀 제거
    teams = [team for team in teams if len(team) >= 2]

    return teams


def sort_by_handicap(participants: list[MeetingParticipant], db: Session) -> list[MeetingParticipant]:
    """
    핸디캡 기준 정렬 함수
    
    참가자를 핸디캡 낮은 순서대로 정렬합니다 (고수 → 초보).
    게스트는 guest_handicap을 사용하고, 멤버는 get_user_handicap_for_formation()을 사용합니다.
    
    Args:
        participants: 참가자 리스트
        db: 데이터베이스 세션
    
    Returns:
        핸디캡 순으로 정렬된 참가자 리스트
    """

    def get_handicap_value(participant: MeetingParticipant) -> float:
        """참가자의 핸디캡 값을 반환"""
        # 게스트인 경우
        if _is_guest_participant(participant):
            # guest_id를 통해 Guest 모델에서 핸디캡 조회
            if participant.guest_id:
                guest = db.query(Guest).filter(Guest.id == participant.guest_id).first()
                if guest and guest.handicap is not None:
                    return float(guest.handicap)
            # 하위 호환성: guest_handicap 필드도 확인
            guest_handicap = getattr(participant, "guest_handicap", None)
            if guest_handicap is not None:
                return float(guest_handicap)
            return 72.0  # 기본값 (게스트 핸디캡이 없으면 높은 값으로 처리)

        # 멤버인 경우
        if participant.user_id:
            handicap = get_user_handicap_for_formation(db, participant.user_id)
            if handicap is not None:
                return float(handicap)

        # 핸디캡이 없는 경우 높은 값으로 처리 (마지막에 배치)
        return 72.0

    # 핸디캡 기준으로 정렬 (낮은 순서 = 고수부터)
    sorted_participants = sorted(participants, key=get_handicap_value)

    return sorted_participants


def sort_by_previous_record(participants: list[MeetingParticipant], db: Session) -> list[MeetingParticipant]:
    """
    Net Score 기준 정렬 함수 (직전 대회 성적 기준)
    
    MeetingResult에서 직전 대회 Net Score를 조회하여 정렬합니다.
    Net Score가 낮을수록 좋은 성적이므로 낮은 순서대로 정렬합니다.
    기록이 없는 참가자는 마지막에 배치합니다.
    
    Args:
        participants: 참가자 리스트
        db: 데이터베이스 세션
    
    Returns:
        Net Score 순으로 정렬된 참가자 리스트 (성적 좋은 순)
    """

    def get_net_score_value(participant: MeetingParticipant) -> tuple[bool, float]:
        """참가자의 Net Score 값을 반환 (기록 여부, Net Score)"""
        # 게스트는 기록이 없으므로 마지막에 배치
        if _is_guest_participant(participant):
            return (False, 999.0)

        # 멤버인 경우 직전 대회 성적 조회
        if participant.user_id:
            # 가장 최근 MeetingResult 조회
            latest_result = db.query(MeetingResult).filter(MeetingResult.user_id == participant.user_id).order_by(
                desc(MeetingResult.completed_at)).first()

            if latest_result and latest_result.net_score is not None:
                return (True, float(latest_result.net_score))

        # 기록이 없는 경우 마지막에 배치
        return (False, 999.0)

    # Net Score 기준으로 정렬 (낮은 순서 = 성적 좋은 순)
    # 기록이 있는 참가자를 먼저 배치하고, 기록이 없는 참가자는 마지막에 배치
    sorted_participants = sorted(participants, key=lambda p: get_net_score_value(p))

    return sorted_participants


def separate_by_gender(participants: list[MeetingParticipant], db: Session) -> dict[str, list[MeetingParticipant]]:
    """
    성별 분리 함수
    
    참가자를 남성 그룹과 여성 그룹으로 분리합니다.
    게스트는 guest_name만 있고 user_id가 없을 수 있으므로, 
    user_id가 있는 경우에만 User 모델에서 gender를 조회합니다.
    
    Args:
        participants: 참가자 리스트
        db: 데이터베이스 세션
    
    Returns:
        {
            "male": 남성 참가자 리스트,
            "female": 여성 참가자 리스트,
            "other": 기타 성별 참가자 리스트
        }
    """
    male_participants = []
    female_participants = []
    other_participants = []

    for participant in participants:
        # 게스트인 경우 guest_id를 통해 Guest 모델에서 성별 조회
        if _is_guest_participant(participant):
            gender = None
            if participant.guest_id:
                guest = db.query(Guest).filter(Guest.id == participant.guest_id).first()
                if guest:
                    gender = guest.gender
            # 하위 호환성: guest_gender 필드도 확인
            if gender is None:
                gender = getattr(participant, "guest_gender", None)

            if gender == Gender.MALE:
                male_participants.append(participant)
            elif gender == Gender.FEMALE:
                female_participants.append(participant)
            else:
                # 게스트 성별 정보가 없는 경우 "other"로 분류
                other_participants.append(participant)
            continue

        # 멤버인 경우 User 모델에서 gender 조회
        if participant.user_id:
            user = db.query(User).filter(User.id == participant.user_id).first()
            if user and user.gender:
                if user.gender == Gender.MALE:
                    male_participants.append(participant)
                elif user.gender == Gender.FEMALE:
                    female_participants.append(participant)
                else:
                    other_participants.append(participant)
            else:
                # 성별 정보가 없는 경우 "other"로 분류
                other_participants.append(participant)
        else:
            # user_id가 없는 경우 "other"로 분류
            other_participants.append(participant)

    return {"male": male_participants, "female": female_participants, "other": other_participants}


# =============================================================================
# Phase 4: 2단계 선택 방식 구현
# =============================================================================


def form_teams_by_mode(participants: list[MeetingParticipant],
                       formation_mode: TeamFormationMode,
                       db: Session,
                       max_team_size: int = 4) -> list[list[MeetingParticipant]]:
    """
    주 편성 함수 - 6가지 모드별로 분기 처리
    
    2단계 선택 방식:
    1단계: 성별 분리 / 성별 혼합
    2단계: 핸디캡 기준 / 직전대회 성적 기준 / 완전 랜덤
    
    Args:
        participants: 참가자 리스트
        formation_mode: 팀 편성 모드 (6가지 중 하나)
        db: 데이터베이스 세션
        max_team_size: 최대 팀 크기 (기본값: 4)
    
    Returns:
        팀별 참가자 리스트 (List[List[MeetingParticipant]])
    """
    if not participants:
        return []

    all_teams = []

    # 성별 분리 모드
    if formation_mode in [
            TeamFormationMode.GENDER_SEPARATED_HANDICAP, TeamFormationMode.GENDER_SEPARATED_PREVIOUS_RECORD,
            TeamFormationMode.GENDER_SEPARATED_RANDOM
    ]:
        # 성별로 분리
        gender_groups = separate_by_gender(participants, db)

        # 1인 팀 방지: "other"가 1명뿐이면 남/여 그룹 중 하나에 합쳐서 4+4 등 균등 편성
        other_list = gender_groups["other"] or []
        if len(other_list) == 1:
            if gender_groups["male"]:
                gender_groups["male"] = gender_groups["male"] + other_list
            elif gender_groups["female"]:
                gender_groups["female"] = gender_groups["female"] + other_list
            gender_groups["other"] = []

        # 남성 그룹 편성
        if gender_groups["male"]:
            male_teams = _form_teams_by_criteria(gender_groups["male"], formation_mode, db, max_team_size)
            all_teams.extend(male_teams)

        # 여성 그룹 편성
        if gender_groups["female"]:
            female_teams = _form_teams_by_criteria(gender_groups["female"], formation_mode, db, max_team_size)
            all_teams.extend(female_teams)

        # 기타 그룹 편성 (2명 이상일 때만 별도 팀 생성)
        if gender_groups["other"]:
            other_teams = _form_teams_by_criteria(gender_groups["other"], formation_mode, db, max_team_size)
            all_teams.extend(other_teams)

    # 성별 혼합 모드
    else:
        # 전체 참가자에 대해 편성
        all_teams = _form_teams_by_criteria(participants, formation_mode, db, max_team_size)

    return all_teams


def _form_teams_by_criteria(participants: list[MeetingParticipant],
                            formation_mode: TeamFormationMode,
                            db: Session,
                            max_team_size: int = 4) -> list[list[MeetingParticipant]]:
    """
    편성 기준에 따라 팀 편성 (내부 헬퍼 함수)
    
    Args:
        participants: 참가자 리스트
        formation_mode: 팀 편성 모드
        db: 데이터베이스 세션
        max_team_size: 최대 팀 크기
    
    Returns:
        팀별 참가자 리스트
    """
    if not participants:
        return []

    # 랜덤 모드
    if formation_mode in [TeamFormationMode.GENDER_SEPARATED_RANDOM, TeamFormationMode.GENDER_MIXED_RANDOM]:
        return form_random_teams(participants, max_team_size)

    # 정렬 기준 선택
    if formation_mode in [TeamFormationMode.GENDER_SEPARATED_HANDICAP, TeamFormationMode.GENDER_MIXED_HANDICAP]:
        # 핸디캡 기준 정렬
        sorted_participants = sort_by_handicap(participants, db)
    elif formation_mode in [
            TeamFormationMode.GENDER_SEPARATED_PREVIOUS_RECORD, TeamFormationMode.GENDER_MIXED_PREVIOUS_RECORD
    ]:
        # 직전대회 성적 기준 정렬
        sorted_participants = sort_by_previous_record(participants, db)
    else:
        # 기본값: 핸디캡 기준
        sorted_participants = sort_by_handicap(participants, db)

    # 팀 개수 계산
    distribution = calculate_team_distribution(len(sorted_participants), max_team_size)
    team_count = distribution["team_count"]

    # 지그재그 분배
    teams = zigzag_distribute(sorted_participants, team_count, max_team_size)

    return teams


def form_random_teams(participants: list[MeetingParticipant], max_team_size: int = 4) -> list[list[MeetingParticipant]]:
    """
    랜덤 편성 함수
    
    성별 분리 모드에서는 각 그룹별로 랜덤 배정하고,
    성별 혼합 모드에서는 전체 참가자를 랜덤 배정합니다.
    
    Args:
        participants: 참가자 리스트
        max_team_size: 최대 팀 크기 (기본값: 4)
    
    Returns:
        팀별 참가자 리스트
    """
    if not participants or len(participants) < 2:
        return []

    # 참가자 리스트 복사 및 셔플
    shuffled = participants.copy()
    random.shuffle(shuffled)

    # 팀 개수 계산
    distribution = calculate_team_distribution(len(shuffled), max_team_size)
    team_count = distribution["team_count"]

    # 특별 케이스: 6명을 2팀으로 나눌 때는 4인조 1팀 + 2인조 1팀
    if len(shuffled) == 6 and team_count == 2 and max_team_size == 4:
        return [
            shuffled[:4],  # 팀1: 4인조
            shuffled[4:]  # 팀2: 2인조
        ]

    # 팀 초기화
    teams = [[] for _ in range(team_count)]

    # 순차적으로 배정
    participant_index = 0
    team_index = 0

    while participant_index < len(shuffled):
        if len(teams[team_index]) < max_team_size:
            teams[team_index].append(shuffled[participant_index])
            participant_index += 1

        team_index = (team_index + 1) % team_count

        # 모든 팀이 가득 찬 경우 남은 참가자는 마지막 팀부터 역순으로 배정
        if all(len(team) >= max_team_size for team in teams):
            while participant_index < len(shuffled):
                for idx in range(team_count - 1, -1, -1):
                    if participant_index >= len(shuffled):
                        break
                    teams[idx].append(shuffled[participant_index])
                    participant_index += 1
            break

    # 빈 팀 제거 (최소 2명 이상)
    teams = [team for team in teams if len(team) >= 2]

    return teams


# =============================================================================
# Phase 5: 게스트 처리 로직
# =============================================================================


def calculate_guest_handicap(average_score: int) -> Decimal:
    """
    게스트 핸디캡 계산
    
    게스트의 평균 타수를 받아서 핸디캡을 계산합니다.
    공식: handicap = average_score - 72
    범위: 0 ~ 72로 클램프
    
    Args:
        average_score: 게스트의 평균 타수
    
    Returns:
        계산된 핸디캡 (Decimal)
    """
    return calculate_handicap_from_average_score(average_score)


def add_guest_to_meeting(meeting_id: int,
                         guest_name: str,
                         guest_handicap: Decimal | None = None,
                         average_score: int | None = None,
                         guest_birthdate: str | None = None,
                         guest_gender: Gender | None = None,
                         db: Session = None,
                         *,
                         auto_commit: bool = True) -> MeetingParticipant:
    """
    게스트 데이터 입력 함수
    
    게스트용 Guest 레코드를 생성하고, MeetingParticipant를 생성합니다.
    더 이상 더미 User를 생성하지 않고, 별도의 Guest 테이블을 사용합니다.
    
    Args:
        meeting_id: 모임 ID
        guest_name: 게스트 이름
        guest_handicap: 게스트 핸디캡 (선택적, average_score가 있으면 자동 계산)
        average_score: 게스트 평균 타수 (선택적, guest_handicap이 없으면 필수)
        guest_birthdate: 게스트 생년월일 (YYYY-MM-DD 형식, 선택적)
        guest_gender: 게스트 성별 (MALE/FEMALE만 허용, 선택적)
        db: 데이터베이스 세션
    
    Returns:
        생성된 MeetingParticipant (게스트)
    
    Raises:
        ValueError: guest_handicap과 average_score가 모두 없는 경우, 또는 생년월일 검증 실패
    """
    if db is None:
        raise ValueError("데이터베이스 세션이 필요합니다.")

    # 생년월일 검증 (제공된 경우)
    guest_birthdate_dt = None
    if guest_birthdate:
        try:
            birth_date = datetime.strptime(guest_birthdate, "%Y-%m-%d").date()
            today = get_kst_date().date()

            # 미래 날짜 체크
            if birth_date > today:
                raise ValueError("생년월일은 미래 날짜일 수 없습니다.")

            # 만 나이 계산
            age = today.year - birth_date.year
            if (today.month, today.day) < (birth_date.month, birth_date.day):
                age -= 1

            # 만 14세 이상 확인
            if age < 14:
                raise ValueError("만 14세 이상만 가입할 수 있습니다.")

            # 유효한 범위 확인 (1900년 이후)
            if birth_date.year < 1900:
                raise ValueError("올바른 생년월일을 입력해주세요.")

            guest_birthdate_dt = datetime.combine(birth_date, datetime.min.time())

        except ValueError as e:
            if "생년월일" in str(e) or "만 14세" in str(e) or "올바른" in str(e):
                raise
            raise ValueError("올바른 날짜 형식(YYYY-MM-DD)을 입력해주세요.")

    # 성별 검증 (제공된 경우)
    if guest_gender and guest_gender not in [Gender.MALE, Gender.FEMALE]:
        raise ValueError("성별은 MALE(남성) 또는 FEMALE(여성)만 허용됩니다.")

    # 핸디캡 계산
    if guest_handicap is None:
        if average_score is None:
            raise ValueError("guest_handicap 또는 average_score 중 하나는 필수입니다.")
        guest_handicap = calculate_guest_handicap(average_score)

    # Guest 레코드 생성
    guest = Guest(name=guest_name, handicap=guest_handicap, birthdate=guest_birthdate_dt, gender=guest_gender)

    db.add(guest)
    db.flush()  # id를 얻기 위해 flush

    # 게스트용 MeetingParticipant 생성
    from models import ParticipantType
    guest_participant = MeetingParticipant(
        meeting_id=meeting_id,
        user_id=None,  # 게스트는 user_id가 없음
        guest_id=guest.id,  # Guest ID 사용
        participant_type=ParticipantType.GUEST)

    db.add(guest_participant)
    if auto_commit:
        db.commit()
    else:
        db.flush()
    db.refresh(guest_participant)

    return guest_participant


def integrate_guests_and_members(participants: list[MeetingParticipant]) -> list[MeetingParticipant]:
    """
    게스트와 멤버 통합 함수
    
    멤버와 게스트를 하나의 리스트로 통합합니다.
    이미 participants 리스트에 게스트와 멤버가 함께 있을 수 있으므로,
    이 함수는 단순히 리스트를 반환합니다.
    (실제로는 sort_by_handicap 등에서 게스트를 자동으로 처리하므로 별도 통합 불필요)
    
    Args:
        participants: 참가자 리스트 (멤버 + 게스트)
    
    Returns:
        통합된 참가자 리스트
    """
    # 이미 통합되어 있으므로 그대로 반환
    # 게스트와 멤버를 구분할 필요가 있는 경우를 위해 함수를 제공
    return participants
