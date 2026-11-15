#!/usr/bin/env python3
"""
미팅 관리 테스트
- 미팅 생성/수정/삭제
- 미팅 참가/나가기
- 참가자 관리
- 미팅 통계
"""

import requests
import pytest
import time
from datetime import datetime, timedelta

# API 기본 URL
BASE_URL = "http://localhost:8002/api/v1"

# 서버 없이 in-process 로 돈다. 실서버 대상은 TEEUP_LIVE_API_URL (conftest 의 legacy_api 참고)
pytestmark = pytest.mark.usefixtures("legacy_api")

class TestMeetings:
    """미팅 관리 테스트 클래스"""
    
    def get_auth_token(self, email="leader1@teeup.run", password="test1234"):
        """인증 토큰 획득"""
        response = requests.post(f"{BASE_URL}/auth/login", json={
            "email": email,
            "password": password
        })
        assert response.status_code == 200
        return response.json()["access_token"]
    
    def create_test_club(self):
        """테스트용 클럽 생성 헬퍼 메서드"""
        token = self.get_auth_token()
        
        club_data = {
            "name": f"테스트 골프클럽 {int(time.time())}",
            "description": "테스트용 골프클럽입니다",
            "member_count": 10,
            # 클럽 지역은 자유 입력 location 에서 시도/군구 코드로 바뀌었다 (서울 강남구)
            "sido_code": "11",
            "gungu_codes": ["11680"],
            "contact_info": "010-1234-5678",
            "representative_name": "김테스트",
            "additional_info": "매주 토요일 정기 라운딩"
        }
        
        response = requests.post(f"{BASE_URL}/clubs", json=club_data, headers={
            "Authorization": f"Bearer {token}"
        })
        
        assert response.status_code == 200
        data = response.json()
        return data["id"]
    
    def create_test_meeting(self, club_id=None):
        """테스트용 미팅 생성 헬퍼 메서드"""
        if club_id is None:
            club_id = self.create_test_club()
        
        token = self.get_auth_token()
        
        # 미래 시간으로 설정 (1시간 후)
        meeting_time = datetime.now() + timedelta(hours=1)
        
        meeting_data = {
            "name": f"테스트 라운딩 {int(time.time())}",
            "description": "테스트용 라운딩입니다",
            "location": "서울 골프클럽",
            "meeting_time": meeting_time.isoformat(),
            "tee_times": ["08:00", "08:10", "08:20"],
            "max_participants": 4,
            "meeting_type": "ROUND",
            "meeting_subtype": "REGULAR",
            "total_cost": 100000,
            "green_fee": 80000,
            "caddy_fee": 20000,
            "cart_fee": 0,
            "settlement_method": "EQUAL_SPLIT",
            "reservation_name": "김테스트",
            # RoundingMeetingCreate 가 본문에도 club_id(정수, 필수)를 받도록 바뀌었다 (쿼리 club_id 와 별개)
            "club_id": club_id,
        }
        
        response = requests.post(f"{BASE_URL}/meetings/", json=meeting_data, headers={
            "Authorization": f"Bearer {token}"
        }, params={"club_id": club_id})
        
        assert response.status_code == 200, response.text
        data = response.json()
        return data["id"]

    def get_user_id(self, token):
        """토큰으로 현재 사용자 ID 조회"""
        # GET /users/me 는 없어졌다 (users 라우터에는 PUT /users/me 만 있다). 조회는 /auth/me
        response = requests.get(f"{BASE_URL}/auth/me", headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 200
        return response.json()["id"]

    def add_member_to_club(self, club_id, email="user2@teeup.run", password="test1234"):
        """테스트용 클럽에 기존 테스트 사용자를 활성 멤버로 추가"""
        member_token = self.get_auth_token(email, password)
        member_user_id = self.get_user_id(member_token)
        leader_token = self.get_auth_token()

        response = requests.post(
            f"{BASE_URL}/clubs/{club_id}/members",
            json={"user_id": member_user_id, "role": "MEMBER"},
            headers={"Authorization": f"Bearer {leader_token}"}
        )
        assert response.status_code == 200
        return member_token, member_user_id

    def create_test_round(self, club_id, application_deadline):
        """실제 /rounds 경로로 테스트용 라운딩 생성"""
        token = self.get_auth_token()
        meeting_time = datetime.now() + timedelta(hours=1)
        round_data = {
            "name": f"테스트 라운딩 {int(time.time())}",
            "description": "테스트용 라운딩입니다",
            "location": "서울 골프클럽",
            "meeting_time": meeting_time.isoformat(),
            "tee_times": ["08:00", "08:10", "08:20"],
            "max_participants": 4,
            "meeting_type": "ROUND",
            "meeting_subtype": "REGULAR",
            "total_cost": 100000,
            "green_fee": 80000,
            "caddy_fee": 20000,
            "cart_fee": 0,
            "settlement_method": "EQUAL_SPLIT",
            "reservation_name": "김테스트",
            "application_deadline": application_deadline.isoformat(),
            "club_id": club_id,
            "team_formation_mode": "GENDER_MIXED_HANDICAP",
            "team_size": 4,
        }

        response = requests.post(f"{BASE_URL}/rounds/", json=round_data, headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 200
        return response.json()["id"]

    def create_test_social(self, club_id, application_deadline):
        """실제 /socials 경로로 테스트용 소셜 생성"""
        token = self.get_auth_token()
        meeting_time = datetime.now() + timedelta(hours=1)
        social_data = {
            "name": f"테스트 소셜 {int(time.time())}",
            "description": "테스트용 소셜입니다",
            "type": "CASUAL",
            "meeting_time": meeting_time.isoformat(),
            "max_participants": 4,
            "venue_name": "테스트 라운지",
            "social_cost": 30000,
            "settlement_method": "EQUAL_SPLIT",
            "club_id": club_id,
            "application_deadline": application_deadline.isoformat(),
            "social_notes": "테스트 메모",
        }

        response = requests.post(f"{BASE_URL}/socials/", json=social_data, headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 200
        return response.json()["id"]

    def test_bulk_update_round_teams(self):
        """라운딩 팀 상태 전체 저장"""
        club_id = self.create_test_club()
        leader_token = self.get_auth_token()
        leader_user_id = self.get_user_id(leader_token)
        round_id = self.create_test_round(club_id, datetime.now() + timedelta(minutes=30))

        response = requests.put(
            f"{BASE_URL}/rounds/{round_id}/teams",
            json={
                "teams": [
                    {
                        "name": "팀 1",
                        "members": [
                            {"user_id": leader_user_id},
                        ],
                    }
                ]
            },
            headers={"Authorization": f"Bearer {leader_token}"}
        )

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "팀 1"
        assert len(data[0]["members"]) == 1
        assert data[0]["members"][0]["user_id"] == leader_user_id
    
    def test_create_meeting_success(self):
        """미팅 생성 성공"""
        club_id = self.create_test_club()
        token = self.get_auth_token()
        
        # 미래 시간으로 설정 (1시간 후)
        meeting_time = datetime.now() + timedelta(hours=1)
        
        meeting_data = {
            "name": f"테스트 라운딩 {int(time.time())}",
            "description": "테스트용 라운딩입니다",
            "location": "서울 골프클럽",
            "meeting_time": meeting_time.isoformat(),
            "tee_times": ["08:00", "08:10", "08:20"],
            "max_participants": 4,
            "meeting_type": "ROUND",
            "meeting_subtype": "REGULAR",
            "total_cost": 100000,
            "green_fee": 80000,
            "caddy_fee": 20000,
            "cart_fee": 0,
            "settlement_method": "EQUAL_SPLIT",
            "reservation_name": "김테스트",
            # RoundingMeetingCreate 가 본문에도 club_id(정수, 필수)를 받도록 바뀌었다 (쿼리 club_id 와 별개)
            "club_id": club_id,
        }
        
        response = requests.post(f"{BASE_URL}/meetings/", json=meeting_data, headers={
            "Authorization": f"Bearer {token}"
        }, params={"club_id": club_id})
        
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == meeting_data["name"]
        assert data["description"] == meeting_data["description"]
        assert "id" in data
    
    def test_create_meeting_without_authentication(self):
        """인증 없이 미팅 생성 실패"""
        meeting_data = {
            "name": "무인증 미팅",
            "meeting_time": (datetime.now() + timedelta(hours=1)).isoformat(),
            "tee_times": ["08:00"],
            "max_participants": 4,
            "meeting_subtype": "REGULAR",
            "settlement_method": "EQUAL_SPLIT"
        }
        
        response = requests.post(f"{BASE_URL}/meetings", json=meeting_data, params={"club_id": "test_club"})
        
        # HTTPBearer(auto_error=False) + 인증 의존성의 401 로 바뀌어 403 이 아니라 401
        assert response.status_code == 401
    
    def test_create_meeting_with_invalid_data(self):
        """잘못된 데이터로 미팅 생성 실패"""
        club_id = self.create_test_club()
        token = self.get_auth_token()
        
        # 필수 필드 누락
        meeting_data = {
            "name": "",  # 빈 이름
            "max_participants": -1  # 음수 참가자 수
        }
        
        response = requests.post(f"{BASE_URL}/meetings/", json=meeting_data, headers={
            "Authorization": f"Bearer {token}"
        }, params={"club_id": club_id})
        
        assert response.status_code == 422  # Validation Error
    
    def test_get_meetings_list(self):
        """미팅 목록 조회"""
        token = self.get_auth_token()
        
        response = requests.get(f"{BASE_URL}/meetings", headers={
            "Authorization": f"Bearer {token}"
        })
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert "data" in data
        assert isinstance(data["data"], list)
    
    def test_get_my_meetings(self):
        """내 미팅 목록 조회"""
        token = self.get_auth_token()
        
        response = requests.get(f"{BASE_URL}/meetings/my", headers={
            "Authorization": f"Bearer {token}"
        })
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert "data" in data
        assert isinstance(data["data"], list)
    
    def test_get_meeting_detail(self):
        """미팅 상세 정보 조회"""
        # 먼저 미팅 생성
        meeting_id = self.create_test_meeting()
        
        token = self.get_auth_token()
        
        response = requests.get(f"{BASE_URL}/meetings/{meeting_id}", headers={
            "Authorization": f"Bearer {token}"
        })
        
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == meeting_id
        assert "name" in data
        assert "description" in data
    
    def test_update_meeting(self):
        """미팅 정보 수정"""
        # 먼저 미팅 생성
        meeting_id = self.create_test_meeting()
        
        token = self.get_auth_token()
        
        update_data = {
            "name": "수정된 미팅 이름",
            "description": "수정된 미팅 설명"
        }
        
        response = requests.put(f"{BASE_URL}/meetings/{meeting_id}", json=update_data, headers={
            "Authorization": f"Bearer {token}"
        })
        
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == update_data["name"]
        assert data["description"] == update_data["description"]
    
    def test_delete_meeting(self):
        """미팅 삭제"""
        # 먼저 미팅 생성
        meeting_id = self.create_test_meeting()
        
        token = self.get_auth_token()
        
        # 미팅 삭제
        response = requests.delete(f"{BASE_URL}/meetings/{meeting_id}", headers={
            "Authorization": f"Bearer {token}"
        })
        
        # 삭제 성공 확인
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert data["success"] == True
        
        # 삭제된 미팅 조회 시 404 확인
        get_response = requests.get(f"{BASE_URL}/meetings/{meeting_id}", headers={
            "Authorization": f"Bearer {token}"
        })
        assert get_response.status_code == 404
    
    def test_get_club_meetings(self):
        """클럽별 미팅 목록 조회"""
        # 먼저 클럽과 미팅 생성
        club_id = self.create_test_club()
        self.create_test_meeting(club_id)

        token = self.get_auth_token()

        response = requests.get(f"{BASE_URL}/meetings/clubs/{club_id}", headers={
            "Authorization": f"Bearer {token}"
        })

        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert "data" in data
        assert isinstance(data["data"], list)
    
    def test_join_meeting(self):
        """미팅 참가"""
        # 먼저 미팅 생성
        meeting_id = self.create_test_meeting()
        
        token = self.get_auth_token()
        
        # 참가/탈퇴/참가자 조회는 /meetings/{id}/... 에서 유형별 경로 /rounds/{id}/... 로 옮겨졌다
        response = requests.post(f"{BASE_URL}/rounds/{meeting_id}/join", headers={
            "Authorization": f"Bearer {token}"
        })
        
        # 이미 참가한 경우(400) 또는 성공한 경우(200) 모두 허용
        assert response.status_code in [200, 400]
        if response.status_code == 200:
            data = response.json()
            assert "message" in data
    
    def test_leave_meeting(self):
        """미팅 나가기"""
        # 먼저 미팅 생성 (생성자는 자동 참가됨)
        meeting_id = self.create_test_meeting()

        token = self.get_auth_token()

        # 생성자는 탈퇴할 수 없으므로 400 에러가 정상
        # 참가/탈퇴/참가자 조회는 /meetings/{id}/... 에서 유형별 경로 /rounds/{id}/... 로 옮겨졌다
        response = requests.delete(f"{BASE_URL}/rounds/{meeting_id}/leave", headers={
            "Authorization": f"Bearer {token}"
        })

        # 생성자는 탈퇴할 수 없으므로 400 에러가 예상됨
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "생성자" in data["detail"]
    
    def test_get_meeting_participants(self):
        """미팅 참가자 목록 조회"""
        # 먼저 미팅 생성
        meeting_id = self.create_test_meeting()
        
        token = self.get_auth_token()
        
        # 참가/탈퇴/참가자 조회는 /meetings/{id}/... 에서 유형별 경로 /rounds/{id}/... 로 옮겨졌다
        response = requests.get(f"{BASE_URL}/rounds/{meeting_id}/participants", headers={
            "Authorization": f"Bearer {token}"
        })
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
    
    def test_remove_participant(self):
        """참가자가 라운딩에서 빠진다

        예전 DELETE /meetings/{id}/participants/{participant_id} (리더가 참가자 제거) 는 없어졌다.
        지금 참가자를 빼는 사용자 API 는 본인 탈퇴 DELETE /rounds/{id}/leave 뿐이라 그것으로 검증한다.
        """
        club_id = self.create_test_club()
        meeting_id = self.create_test_meeting(club_id)
        member_token, member_user_id = self.add_member_to_club(club_id)
        leader_token = self.get_auth_token()

        def participant_user_ids():
            res = requests.get(f"{BASE_URL}/rounds/{meeting_id}/participants", headers={
                "Authorization": f"Bearer {leader_token}"
            })
            assert res.status_code == 200
            return [p.get("user_id") for p in res.json()]

        response = requests.post(f"{BASE_URL}/rounds/{meeting_id}/join", headers={
            "Authorization": f"Bearer {member_token}"
        })
        assert response.status_code == 200
        assert member_user_id in participant_user_ids()

        response = requests.delete(f"{BASE_URL}/rounds/{meeting_id}/leave", headers={
            "Authorization": f"Bearer {member_token}"
        })
        assert response.status_code == 200
        assert member_user_id not in participant_user_ids()
    
    def test_meeting_workflow_complete(self):
        """전체 미팅 워크플로우 테스트"""
        # 1. 클럽 생성
        club_id = self.create_test_club()
        
        # 2. 미팅 생성
        meeting_id = self.create_test_meeting(club_id)
        
        # 3. 미팅 정보 조회
        # 앱이 쓰는 라운딩 상세 /rounds/{id} 로 흐름을 검증한다 (공통 상세 /meetings/{id} 는 test_get_meeting_detail).
        token = self.get_auth_token()
        response = requests.get(f"{BASE_URL}/rounds/{meeting_id}", headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 200
        
        # 4. 미팅 참가 (생성자는 이미 참가되어 있음)
        # 참가/탈퇴/참가자 조회는 /meetings/{id}/... 에서 유형별 경로 /rounds/{id}/... 로 옮겨졌다
        response = requests.post(f"{BASE_URL}/rounds/{meeting_id}/join", headers={
            "Authorization": f"Bearer {token}"
        })
        # 이미 참가한 경우(400) 또는 성공한 경우(200) 모두 허용
        assert response.status_code in [200, 400]
        
        # 5. 참가자 목록 조회
        response = requests.get(f"{BASE_URL}/rounds/{meeting_id}/participants", headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 200
        
        # 6. 클럽별 미팅 목록 조회
        response = requests.get(f"{BASE_URL}/meetings/clubs/{club_id}", headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 200
        
        # 7. 내 미팅 목록 조회
        response = requests.get(f"{BASE_URL}/meetings/my", headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 200

    def test_join_round_rejects_after_application_deadline(self):
        """라운딩 신청 기한 경과 후 참가 거부"""
        club_id = self.create_test_club()
        member_token, _ = self.add_member_to_club(club_id)
        round_id = self.create_test_round(club_id, datetime.now() - timedelta(minutes=1))

        response = requests.post(f"{BASE_URL}/rounds/{round_id}/join", headers={
            "Authorization": f"Bearer {member_token}"
        })

        assert response.status_code == 400
        assert response.json()["detail"] == "참가 신청 마감되었습니다."

    def test_join_social_rejects_after_application_deadline(self):
        """소셜 신청 기한 경과 후 참가 거부"""
        club_id = self.create_test_club()
        member_token, _ = self.add_member_to_club(club_id)
        social_id = self.create_test_social(club_id, datetime.now() - timedelta(minutes=1))

        response = requests.post(f"{BASE_URL}/socials/{social_id}/join", headers={
            "Authorization": f"Bearer {member_token}"
        })

        assert response.status_code == 400
        assert response.json()["detail"] == "참가 신청 마감되었습니다."
