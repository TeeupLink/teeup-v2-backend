"""
클럽 멤버 관리 테스트
"""
import pytest
import requests
import time

# API 기본 URL
BASE_URL = "http://localhost:8002/api/v1"


class TestClubMembers:
    """클럽 멤버 관리 테스트 클래스"""
    
    def get_auth_token(self, email="leader1@teeup.run", password="test1234"):
        """인증 토큰 획득"""
        response = requests.post(f"{BASE_URL}/auth/login", json={
            "email": email,
            "password": password
        })
        assert response.status_code == 200
        return response.json()["access_token"]
    
    def get_auth_headers(self, token):
        """인증 헤더 생성"""
        return {"Authorization": f"Bearer {token}"}
    
    def create_test_club(self):
        """테스트용 클럽 생성 헬퍼 메서드"""
        token = self.get_auth_token()
        
        club_data = {
            "name": f"테스트 골프클럽 {int(time.time())}",
            "description": "테스트용 골프클럽입니다",
            "member_count": 10,
            "location": "서울시 강남구",
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
    
    def get_user_id_from_token(self, token):
        """토큰에서 사용자 ID 추출"""
        # JWT 토큰을 디코딩하여 사용자 ID 추출 (간단한 방법)
        import base64
        import json
        
        try:
            # JWT 토큰의 payload 부분 디코딩
            parts = token.split('.')
            if len(parts) >= 2:
                # Base64 디코딩 (패딩 추가)
                payload = parts[1]
                # 패딩 추가
                missing_padding = len(payload) % 4
                if missing_padding:
                    payload += '=' * (4 - missing_padding)
                
                decoded = base64.b64decode(payload)
                payload_data = json.loads(decoded)
                return payload_data.get('id')  # JWT에서 사용자 ID는 보통 'sub' 필드에 있음
        except Exception:
            pass
        
        # JWT 디코딩 실패 시 다른 방법 시도
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(f"{BASE_URL}/users/me", headers=headers)
        if response.status_code == 200:
            return response.json()["id"]
        else:
            pytest.skip("사용자 정보 조회 실패")
    
    def get_user_id_by_email(self, email):
        """이메일로 사용자 ID 조회"""
        # 관리자 토큰으로 사용자 목록 조회
        admin_token = self.get_auth_token("admin@teeup.run", "admin123")
        response = requests.get(f"{BASE_URL}/users", headers={"Authorization": f"Bearer {admin_token}"})
        if response.status_code == 200:
            users = response.json()
            for user in users:
                if user.get("email") == email:
                    return user["id"]
        return None
    
    def create_test_user(self, email_suffix="test"):
        """테스트용 사용자 생성 (실제 사용자 ID 조회)"""
        # 기존 테스트 사용자들 사용
        test_users = {
            "user2": ("user2@teeup.run", "test1234"),
            "user3": ("user3@teeup.run", "test1234"),
            "manager1": ("manager1@teeup.run", "test1234"),
            "member1": ("member1@teeup.run", "test1234")
        }
        
        if email_suffix in test_users:
            email, password = test_users[email_suffix]
            login_response = requests.post(f"{BASE_URL}/auth/login", json={
                "email": email,
                "password": password
            })
            if login_response.status_code == 200:
                login_data = login_response.json()
                # JWT 토큰에서 사용자 ID 추출
                user_id = self.extract_user_id_from_token(login_data["access_token"])
                if user_id:
                    return {
                        "user_id": user_id,
                        "access_token": login_data["access_token"]
                    }
                else:
                    pytest.skip(f"사용자 ID 추출 실패: {email_suffix}")
            else:
                pytest.skip(f"사용자 로그인 실패: {email_suffix}")
        else:
            pytest.skip(f"알 수 없는 사용자: {email_suffix}")
    
    def extract_user_id_from_token(self, token):
        """JWT 토큰에서 사용자 ID 추출"""
        try:
            import base64
            import json
            
            # JWT 토큰의 payload 부분 디코딩
            parts = token.split('.')
            if len(parts) >= 2:
                # Base64 디코딩 (패딩 추가)
                payload = parts[1]
                # 패딩 추가
                missing_padding = len(payload) % 4
                if missing_padding:
                    payload += '=' * (4 - missing_padding)
                
                decoded = base64.b64decode(payload)
                payload_data = json.loads(decoded)
                return payload_data.get('id')  # JWT에서 사용자 ID는 보통 'sub' 필드에 있음
        except Exception as e:
            print(f"JWT 디코딩 실패: {e}")
            return None
    
    def test_get_club_members_success(self):
        """클럽 멤버 목록 조회 성공 테스트"""
        token = self.get_auth_token()
        club_id = self.create_test_club()
        headers = self.get_auth_headers(token)
        response = requests.get(f"{BASE_URL}/clubs/{club_id}/members", headers=headers)
        
        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # 최소 1명의 멤버(클럽 생성자)가 있어야 함
        assert len(data) >= 1
        
        # 멤버 데이터 구조 확인
        if data:
            member = data[0]
            assert "id" in member
            assert "user_id" in member
            assert "user_name" in member
            assert "user_nickname" in member
            assert "role" in member
            assert "status" in member
            assert "joined_at" in member
    
    def test_get_club_members_unauthorized(self):
        """인증 없이 클럽 멤버 목록 조회 시도"""
        club_id = self.create_test_club()
        response = requests.get(f"{BASE_URL}/clubs/{club_id}/members")
        
        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        
        assert response.status_code == 403
    
    def test_get_club_members_not_member(self):
        """클럽 멤버가 아닌 사용자가 멤버 목록 조회 시도"""
        club_id = self.create_test_club()
        token2 = self.get_auth_token("user2@teeup.run", "test1234")
        headers = self.get_auth_headers(token2)
        response = requests.get(f"{BASE_URL}/clubs/{club_id}/members", headers=headers)
        
        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        
        assert response.status_code == 403

    def test_get_member_record_summary_not_member(self):
        """클럽 멤버가 아닌 사용자가 멤버 기록 요약 조회 시도"""
        club_id = self.create_test_club()
        leader_token = self.get_auth_token()
        leader_headers = self.get_auth_headers(leader_token)

        # 클럽 멤버 1명(리더)을 조회해서 user_id 확보
        members_res = requests.get(f"{BASE_URL}/clubs/{club_id}/members", headers=leader_headers)
        assert members_res.status_code == 200
        members_data = members_res.json()
        # API가 list 또는 {data: []} 형태 모두 있을 수 있어 방어적으로 처리
        member_list = members_data if isinstance(members_data, list) else members_data.get("data", [])
        assert len(member_list) >= 1
        target_user_id = member_list[0]["user_id"]

        # 비멤버 계정으로 기록 요약 요청 -> 403
        token2 = self.get_auth_token("user2@teeup.run", "test1234")
        headers2 = self.get_auth_headers(token2)
        res = requests.get(
            f"{BASE_URL}/clubs/{club_id}/members/{target_user_id}/record-summary",
            headers=headers2,
        )
        assert res.status_code == 403
    
    def test_add_club_member_success(self):
        """클럽 멤버 추가 성공 테스트 (리더 권한)"""
        token = self.get_auth_token()
        club_id = self.create_test_club()
        user2 = self.create_test_user("user2")
        headers = self.get_auth_headers(token)
        member_data = {
            "user_id": user2["user_id"],
            "role": "MEMBER"
        }
        
        response = requests.post(
            f"{BASE_URL}/clubs/{club_id}/members",
            json=member_data,
            headers=headers
        )
        
        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] == True
        assert "message" in data
    
    def test_add_club_member_duplicate(self):
        """이미 멤버인 사용자를 다시 추가 시도"""
        token = self.get_auth_token()
        club_id = self.create_test_club()
        user2 = self.create_test_user("user2")
        headers = self.get_auth_headers(token)
        member_data = {
            "user_id": user2["user_id"],
            "role": "MEMBER"
        }
        
        # 첫 번째 추가
        response1 = requests.post(
            f"{BASE_URL}/clubs/{club_id}/members",
            json=member_data,
            headers=headers
        )
        assert response1.status_code == 200
        
        # 두 번째 추가 시도 (중복)
        response2 = requests.post(
            f"{BASE_URL}/clubs/{club_id}/members",
            json=member_data,
            headers=headers
        )
        
        print(f"Response status: {response2.status_code}")
        print(f"Response content: {response2.text}")
        
        assert response2.status_code == 400
    
    def test_add_club_member_unauthorized(self):
        """권한 없는 사용자가 멤버 추가 시도"""
        club_id = self.create_test_club()
        user2 = self.create_test_user("user2")
        user3 = self.create_test_user("user3")
        token2 = self.get_auth_token("user2@teeup.run", "test1234")
        headers = self.get_auth_headers(token2)
        member_data = {
            "user_id": user3["user_id"],
            "role": "MEMBER"
        }
        
        response = requests.post(
            f"{BASE_URL}/clubs/{club_id}/members",
            json=member_data,
            headers=headers
        )
        
        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        
        assert response.status_code == 403
    
    def test_add_club_member_nonexistent_user(self):
        """존재하지 않는 사용자를 멤버로 추가 시도"""
        token = self.get_auth_token()
        club_id = self.create_test_club()
        headers = self.get_auth_headers(token)
        member_data = {
            "user_id": "nonexistent_user_id",
            "role": "MEMBER"
        }
        
        response = requests.post(
            f"{BASE_URL}/clubs/{club_id}/members",
            json=member_data,
            headers=headers
        )
        
        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        
        assert response.status_code == 404
    
    def test_update_member_role_success(self):
        """멤버 역할 변경 성공 테스트"""
        token = self.get_auth_token()
        club_id = self.create_test_club()
        user2 = self.create_test_user("user2")
        headers = self.get_auth_headers(token)
        
        # 먼저 사용자를 클럽에 추가
        member_data = {
            "user_id": user2["user_id"],
            "role": "MEMBER"
        }
        add_response = requests.post(
            f"{BASE_URL}/clubs/{club_id}/members",
            json=member_data,
            headers=headers
        )
        assert add_response.status_code == 200
        
        # 그 다음 역할 변경
        role_data = {
            "role": "MANAGER"
        }
        
        response = requests.put(
            f"{BASE_URL}/clubs/{club_id}/members/{user2['user_id']}/role",
            json=role_data,
            headers=headers
        )
        
        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] == True
    
    def test_update_member_role_unauthorized(self):
        """권한 없는 사용자가 멤버 역할 변경 시도"""
        club_id = self.create_test_club()
        user2 = self.create_test_user("user2")
        user3 = self.create_test_user("user3")
        token2 = self.get_auth_token("user2@teeup.run", "test1234")
        headers = self.get_auth_headers(token2)
        role_data = {
            "role": "MANAGER"
        }
        
        response = requests.put(
            f"{BASE_URL}/clubs/{club_id}/members/{user3['user_id']}/role",
            json=role_data,
            headers=headers
        )
        
        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        
        assert response.status_code == 403
    
    def test_remove_club_member_success(self):
        """클럽 멤버 제거 성공 테스트"""
        token = self.get_auth_token()
        club_id = self.create_test_club()
        user2 = self.create_test_user("user2")
        headers = self.get_auth_headers(token)
        
        # 먼저 사용자를 클럽에 추가
        member_data = {
            "user_id": user2["user_id"],
            "role": "MEMBER"
        }
        add_response = requests.post(
            f"{BASE_URL}/clubs/{club_id}/members",
            json=member_data,
            headers=headers
        )
        assert add_response.status_code == 200
        
        # 그 다음 멤버 제거
        response = requests.delete(
            f"{BASE_URL}/clubs/{club_id}/members/{user2['user_id']}",
            headers=headers
        )
        
        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] == True
    
    def test_remove_club_member_self(self):
        """자기 자신을 클럽에서 제거 시도"""
        token = self.get_auth_token()
        club_id = self.create_test_club()
        headers = self.get_auth_headers(token)
        
        # 현재 사용자 ID를 얻기 위해 사용자 정보 조회
        user_info_response = requests.get(f"{BASE_URL}/users/me", headers=headers)
        if user_info_response.status_code == 200:
            user_id = user_info_response.json()["id"]
        else:
            pytest.skip("사용자 정보 조회 실패")
        
        response = requests.delete(
            f"{BASE_URL}/clubs/{club_id}/members/{user_id}",
            headers=headers
        )
        
        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        
        # 자기 자신을 제거하는 것은 보통 금지되거나 특별한 처리가 필요
        assert response.status_code in [400, 403]
    
    def test_remove_club_member_unauthorized(self):
        """권한 없는 사용자가 멤버 제거 시도"""
        club_id = self.create_test_club()
        user2 = self.create_test_user("user2")
        user3 = self.create_test_user("user3")
        token2 = self.get_auth_token("user2@teeup.run", "test1234")
        headers = self.get_auth_headers(token2)
        
        response = requests.delete(
            f"{BASE_URL}/clubs/{club_id}/members/{user3['user_id']}",
            headers=headers
        )
        
        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        
        assert response.status_code == 403
    
    def test_remove_club_member_nonexistent(self):
        """존재하지 않는 멤버 제거 시도"""
        token = self.get_auth_token()
        club_id = self.create_test_club()
        headers = self.get_auth_headers(token)
        
        response = requests.delete(
            f"{BASE_URL}/clubs/{club_id}/members/nonexistent_user_id",
            headers=headers
        )
        
        print(f"Response status: {response.status_code}")
        print(f"Response content: {response.text}")
        
        assert response.status_code == 404
    
    def test_member_management_workflow(self):
        """멤버 관리 전체 워크플로우 테스트"""
        token = self.get_auth_token()
        club_id = self.create_test_club()
        user2 = self.create_test_user("user2")
        user3 = self.create_test_user("user3")
        headers = self.get_auth_headers(token)
        
        # 1. 초기 멤버 목록 확인
        response = requests.get(f"{BASE_URL}/clubs/{club_id}/members", headers=headers)
        assert response.status_code == 200
        initial_members = response.json()
        initial_count = len(initial_members)
        
        # 2. 새 멤버 추가
        member_data = {
            "user_id": user2["user_id"],
            "role": "MEMBER"
        }
        response = requests.post(
            f"{BASE_URL}/clubs/{club_id}/members",
            json=member_data,
            headers=headers
        )
        assert response.status_code == 200
        
        # 3. 멤버 목록 재확인
        response = requests.get(f"{BASE_URL}/clubs/{club_id}/members", headers=headers)
        assert response.status_code == 200
        updated_members = response.json()
        assert len(updated_members) == initial_count + 1
        
        # 4. 멤버 역할 변경
        role_data = {"role": "MANAGER"}
        response = requests.put(
            f"{BASE_URL}/clubs/{club_id}/members/{user2['user_id']}/role",
            json=role_data,
            headers=headers
        )
        assert response.status_code == 200
        
        # 5. 멤버 제거 (MANAGER는 제거할 수 없을 수 있으므로 MEMBER로 다시 변경 후 제거)
        role_data = {"role": "MEMBER"}
        response = requests.put(
            f"{BASE_URL}/clubs/{club_id}/members/{user2['user_id']}/role",
            json=role_data,
            headers=headers
        )
        assert response.status_code == 200
        
        # 6. 멤버 제거
        response = requests.delete(
            f"{BASE_URL}/clubs/{club_id}/members/{user2['user_id']}",
            headers=headers
        )
        assert response.status_code == 200
        
        # 6. 최종 멤버 목록 확인
        response = requests.get(f"{BASE_URL}/clubs/{club_id}/members", headers=headers)
        assert response.status_code == 200
        final_members = response.json()
        assert len(final_members) == initial_count