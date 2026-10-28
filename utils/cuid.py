"""
CUID (Collision-resistant Unique Identifier) 생성 유틸리티
"""
import time
import random
import string
import hashlib
import socket
import os

def generate_cuid() -> str:
    """CUID 생성"""
    # 타임스탬프 (36진수)
    timestamp = int(time.time() * 1000)
    timestamp_str = base36_encode(timestamp)
    
    # 카운터 (36진수, 4자리)
    counter = get_counter()
    counter_str = base36_encode(counter).zfill(4)
    
    # 랜덤 문자열 (36진수, 4자리)
    random_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    
    # 호스트 ID (36진수, 2자리)
    host_id = get_host_id()
    
    return f"c{timestamp_str}{counter_str}{random_str}{host_id}"

def base36_encode(num: int) -> str:
    """숫자를 36진수 문자열로 변환"""
    if num == 0:
        return "0"
    
    chars = string.digits + string.ascii_lowercase
    result = ""
    
    while num > 0:
        result = chars[num % 36] + result
        num //= 36
    
    return result

_counter = 0

def get_counter() -> int:
    """카운터 값 반환 (프로세스 내에서 증가)"""
    global _counter
    _counter = (_counter + 1) % (36 ** 4)  # 4자리 36진수 범위
    return _counter

def get_host_id() -> str:
    """호스트 ID 생성 (2자리 36진수)"""
    try:
        # 호스트명의 해시값 사용
        hostname = socket.gethostname()
        hash_value = hashlib.md5(hostname.encode()).hexdigest()
        # 해시값의 첫 2자리를 36진수로 변환
        return base36_encode(int(hash_value[:2], 16))[:2].zfill(2)
    except Exception:
        # 호스트명을 가져올 수 없는 경우 랜덤 값 사용
        return base36_encode(random.randint(0, 36**2 - 1))[:2].zfill(2)
