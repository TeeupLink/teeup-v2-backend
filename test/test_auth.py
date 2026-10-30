#!/usr/bin/env python3
"""
인증 관련 테스트
- 로그인/로그아웃
- 토큰 검증
- 사용자 정보 조회
"""

import requests
import pytest

# API 기본 URL
BASE_URL = "http://localhost:8002/api/v1"

# 서버 없이 in-process 로 돈다. 실서버 대상은 TEEUP_LIVE_API_URL (conftest 의 legacy_api 참고)
pytestmark = pytest.mark.usefixtures("legacy_api")

class TestAuth:
    """인증 테스트 클래스"""
    
    def test_admin_login_success(self):
        """관리자 로그인 성공"""
        response = requests.post(f"{BASE_URL}/auth/login", json={
            "email": "admin@teeup.run",
            "password": "test1234"
        })
        
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["user"]["email"] == "admin@teeup.run"
        assert data["user"]["role"] == "ADMIN"
    
    def test_user_login_success(self):
        """일반 사용자 로그인 성공"""
        response = requests.post(f"{BASE_URL}/auth/login", json={
            "email": "leader1@teeup.run",
            "password": "test1234"
        })
        
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["user"]["email"] == "leader1@teeup.run"
        assert data["user"]["role"] == "USER"
    
    def test_login_with_invalid_credentials(self):
        """잘못된 인증 정보로 로그인 실패"""
        response = requests.post(f"{BASE_URL}/auth/login", json={
            "email": "admin@teeup.run",
            "password": "wrongpassword"
        })
        
        assert response.status_code == 401
        data = response.json()
        assert "detail" in data
    
    def test_login_with_nonexistent_user(self):
        """존재하지 않는 사용자로 로그인 실패"""
        response = requests.post(f"{BASE_URL}/auth/login", json={
            "email": "nonexistent@teeup.run",
            "password": "test1234"
        })
        
        assert response.status_code == 401
        data = response.json()
        assert "detail" in data
    
    def test_get_current_user_with_valid_token(self):
        """유효한 토큰으로 현재 사용자 정보 조회"""
        # 먼저 로그인
        login_response = requests.post(f"{BASE_URL}/auth/login", json={
            "email": "leader1@teeup.run",
            "password": "test1234"
        })
        
        assert login_response.status_code == 200
        token = login_response.json()["access_token"]
        
        # 현재 사용자 정보 조회
        response = requests.get(f"{BASE_URL}/auth/me", headers={
            "Authorization": f"Bearer {token}"
        })
        
        assert response.status_code == 200
        data = response.json()
        assert data["email"] == "leader1@teeup.run"
        # /auth/me 응답은 role 대신 type("user"/"admin") 을 준다
        assert data["type"] == "user"
        assert "id" in data
    
    def test_get_current_user_without_token(self):
        """토큰 없이 현재 사용자 정보 조회 실패"""
        response = requests.get(f"{BASE_URL}/auth/me")
        
        # HTTPBearer(auto_error=False) + 인증 의존성의 401 로 바뀌어 403 이 아니라 401
        assert response.status_code == 401
    
    def test_get_current_user_with_invalid_token(self):
        """잘못된 토큰으로 현재 사용자 정보 조회 실패"""
        response = requests.get(f"{BASE_URL}/auth/me", headers={
            "Authorization": "Bearer invalid_token"
        })
        
        assert response.status_code in [401, 500]  # 401 Unauthorized 또는 500 Internal Server Error
    
    def test_refresh_token(self):
        """리프레시 토큰으로 액세스 토큰 갱신"""
        # 먼저 로그인
        login_response = requests.post(f"{BASE_URL}/auth/login", json={
            "email": "user1@teeup.run",
            "password": "test1234"
        })
        
        assert login_response.status_code == 200
        refresh_token = login_response.json()["refresh_token"]
        
        # 토큰 갱신
        response = requests.post(f"{BASE_URL}/auth/refresh", json={
            "refresh_token": refresh_token
        })
        
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
    
    def test_logout(self):
        """로그아웃 테스트"""
        # 먼저 로그인
        login_response = requests.post(f"{BASE_URL}/auth/login", json={
            "email": "user2@teeup.run",
            "password": "test1234"
        })
        
        assert login_response.status_code == 200
        token = login_response.json()["access_token"]
        
        # 로그아웃
        response = requests.post(f"{BASE_URL}/auth/logout", headers={
            "Authorization": f"Bearer {token}"
        })
        
        # 로그아웃은 보통 200 또는 204 상태 코드
        assert response.status_code in [200, 204]
    
    def test_all_test_accounts_can_login(self):
        """모든 테스트 계정이 로그인 가능한지 확인"""
        test_accounts = [
            "admin@teeup.run",
            "leader1@teeup.run", 
            "manager1@teeup.run",
            "user1@teeup.run",
            "user2@teeup.run",
            "user3@teeup.run"
        ]
        
        for email in test_accounts:
            response = requests.post(f"{BASE_URL}/auth/login", json={
                "email": email,
                "password": "test1234"
            })
            
            assert response.status_code == 200, f"Failed to login with {email}"
            data = response.json()
            assert "access_token" in data
            assert data["user"]["email"] == email
