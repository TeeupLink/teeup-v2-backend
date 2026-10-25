"""
쿼리 최적화 유틸리티
데이터베이스 쿼리 성능 향상을 위한 도구들
"""

from typing import Any
from sqlalchemy.orm import Session, Query
from sqlalchemy import func, and_, or_
import logging

logger = logging.getLogger(__name__)

class QueryOptimizer:
    """쿼리 최적화 클래스"""
    
    @staticmethod
    def add_pagination(query: Query, page: int = 1, limit: int = 10) -> Query:
        """페이지네이션 추가"""
        offset = (page - 1) * limit
        return query.offset(offset).limit(limit)
    
    @staticmethod
    def add_search_filter(query: Query, search_term: str, search_columns: list[str]) -> Query:
        """검색 필터 추가 (LIKE 검색)"""
        if not search_term:
            return query
        
        search_conditions = []
        for column in search_columns:
            search_conditions.append(column.ilike(f"%{search_term}%"))
        
        return query.filter(or_(*search_conditions))
    
    @staticmethod
    def add_date_range_filter(query: Query, date_column, date_from: str | None = None, date_to: str | None = None) -> Query:
        """날짜 범위 필터 추가"""
        conditions = []
        
        if date_from:
            conditions.append(date_column >= date_from)
        
        if date_to:
            conditions.append(date_column <= date_to)
        
        if conditions:
            return query.filter(and_(*conditions))
        
        return query
    
    @staticmethod
    def add_numeric_range_filter(query: Query, column, min_value: float | None = None, max_value: float | None = None) -> Query:
        """숫자 범위 필터 추가"""
        conditions = []
        
        if min_value is not None:
            conditions.append(column >= min_value)
        
        if max_value is not None:
            conditions.append(column <= max_value)
        
        if conditions:
            return query.filter(and_(*conditions))
        
        return query
    
    @staticmethod
    def optimize_join_query(query: Query, join_tables: list[str]) -> Query:
        """JOIN 최적화"""
        # 필요한 테이블만 JOIN하도록 최적화
        # 실제 구현에서는 모델 관계를 기반으로 동적으로 JOIN
        return query
    
    @staticmethod
    def add_ordering(query: Query, order_by: str, desc: bool = False) -> Query:
        """정렬 추가"""
        if desc:
            return query.order_by(order_by.desc())
        return query.order_by(order_by)
    
    @staticmethod
    def get_count_optimized(query: Query) -> int:
        """최적화된 카운트 쿼리"""
        # COUNT(*) 대신 COUNT(1) 사용으로 성능 향상
        return query.with_entities(func.count(1)).scalar() or 0

def create_optimized_list_query(
    db: Session,
    base_model,
    page: int = 1,
    limit: int = 10,
    search_term: str | None = None,
    search_columns: list | None = None,
    filters: dict[str, Any] | None = None,
    order_by: str | None = None,
    desc: bool = True
) -> dict[str, Any]:
    """최적화된 목록 조회 쿼리 생성"""
    
    # 기본 쿼리 생성
    query = db.query(base_model)
    
    # 검색 필터 적용
    if search_term and search_columns:
        query = QueryOptimizer.add_search_filter(query, search_term, search_columns)
    
    # 추가 필터 적용
    if filters:
        for column, value in filters.items():
            if value is not None:
                if hasattr(base_model, column):
                    query = query.filter(getattr(base_model, column) == value)
    
    # 총 개수 조회 (최적화된 카운트)
    total = QueryOptimizer.get_count_optimized(query)
    
    # 정렬 적용
    if order_by and hasattr(base_model, order_by):
        query = QueryOptimizer.add_ordering(query, getattr(base_model, order_by), desc)
    
    # 페이지네이션 적용
    query = QueryOptimizer.add_pagination(query, page, limit)
    
    # 결과 조회
    results = query.all()
    
    # 페이지 정보 계산
    total_pages = (total + limit - 1) // limit
    
    return {
        "data": results,
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages
    }

def create_optimized_join_query(
    db: Session,
    base_model,
    join_models: list[tuple],  # [(model, join_condition), ...]
    select_columns: list,
    filters: dict[str, Any] | None = None,
    order_by: str | None = None,
    desc: bool = True,
    page: int = 1,
    limit: int = 10
) -> dict[str, Any]:
    """최적화된 JOIN 쿼리 생성"""
    
    # 기본 쿼리 생성
    query = db.query(*select_columns)
    
    # JOIN 추가
    for model, condition in join_models:
        query = query.join(model, condition)
    
    # 필터 적용
    if filters:
        for column, value in filters.items():
            if value is not None:
                # 첫 번째 모델에서 컬럼 찾기
                if hasattr(base_model, column):
                    query = query.filter(getattr(base_model, column) == value)
    
    # 총 개수 조회
    count_query = db.query(func.count(1))
    for model, condition in join_models:
        count_query = count_query.join(model, condition)
    
    if filters:
        for column, value in filters.items():
            if value is not None and hasattr(base_model, column):
                count_query = count_query.filter(getattr(base_model, column) == value)
    
    total = count_query.scalar() or 0
    
    # 정렬 적용
    if order_by and hasattr(base_model, order_by):
        query = QueryOptimizer.add_ordering(query, getattr(base_model, order_by), desc)
    
    # 페이지네이션 적용
    query = QueryOptimizer.add_pagination(query, page, limit)
    
    # 결과 조회
    results = query.all()
    
    # 페이지 정보 계산
    total_pages = (total + limit - 1) // limit
    
    return {
        "data": results,
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages
    }

# 성능 모니터링 데코레이터
def monitor_query_performance(func):
    """쿼리 성능 모니터링 데코레이터"""
    import time
    
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        
        execution_time = end_time - start_time
        if execution_time > 1.0:  # 1초 이상 걸리는 쿼리 경고
            logger.warning(f"Slow query detected in {func.__name__}: {execution_time:.2f}s")
        
        return result
    
    return wrapper
