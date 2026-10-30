"""
pytest fixtures for API testing
"""
import os
import tempfile

# ── 테스트 DB 격리 ─────────────────────────────────────────────
# database.py 는 임포트되는 순간 settings.DATABASE_URL 로 전역 엔진을 만든다.
# 테스트 파일마다 "임포트 전에 DATABASE_URL 을 바꾸는" 패턴을 쓰지만, 한 프로세스에서
# 전체를 돌리면 가장 먼저 database 를 임포트한 파일의 값이 끝까지 쓰인다.
# test_admin_* 가 setdefault("sqlite://") 로 먼저 임포트하면 QueuePool + 메모리 sqlite
# 조합이 되어 연결마다 빈 DB 가 열리고, 전역 엔진에 테이블을 만드는 test_expenses /
# test_scores_null_timestamp 가 "no such table" 로 깨졌다.
# 어떤 테스트 파일보다 먼저 읽히는 conftest 에서 파일 기반 임시 sqlite 하나로 고정한다.
# (셸에 운영 DATABASE_URL 이 있어도 테스트가 그 DB 를 건드리지 않도록 덮어쓴다.)
_TEST_DB_DIR = tempfile.mkdtemp(prefix="teeup-pytest-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TEST_DB_DIR, "test.db")

import pytest  # noqa: E402
import requests  # noqa: E402

# API 기본 URL
BASE_URL = "http://localhost:8002/api/v1"

@pytest.fixture
def token():
    """로그인하여 토큰을 반환하는 fixture"""
    response = requests.post(f"{BASE_URL}/auth/login", json={
        "email": "leader@teeup.run",
        "password": "test123"
    })
    
    if response.status_code == 200:
        data = response.json()
        return data['access_token']
    else:
        pytest.skip("로그인 실패 - 테스트 데이터가 없습니다")

@pytest.fixture
def admin_token():
    """관리자 토큰을 반환하는 fixture"""
    response = requests.post(f"{BASE_URL}/auth/login", json={
        "email": "admin@teeup.run",
        "password": "admin123"
    })
    
    if response.status_code == 200:
        data = response.json()
        return data['access_token']
    else:
        pytest.skip("관리자 로그인 실패 - 테스트 데이터가 없습니다")

@pytest.fixture
def club_id(token):
    """테스트용 클럽 ID를 반환하는 fixture"""
    # 기존 클럽이 있는지 확인
    response = requests.get(f"{BASE_URL}/clubs", headers={"Authorization": f"Bearer {token}"})
    
    if response.status_code == 200:
        clubs = response.json()
        if clubs and len(clubs) > 0:
            return clubs[0]['id']
    
    # 클럽이 없으면 생성
    club_data = {
        "name": "테스트 골프클럽",
        "type": "REGULAR",
        "description": "테스트용 골프클럽입니다",
        "member_count": 10,
        "location": "서울시 강남구",
        "contact_info": "010-1234-5678",
        "additional_info": "매주 토요일 정기 라운딩"
    }
    
    response = requests.post(f"{BASE_URL}/clubs", json=club_data, headers={"Authorization": f"Bearer {token}"})
    
    if response.status_code in [200, 201]:
        data = response.json()
        return data['id']
    else:
        pytest.skip("클럽 생성 실패")

