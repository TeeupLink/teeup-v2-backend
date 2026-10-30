# Payment 관련 모델
from sqlalchemy import Column, String, Text, DateTime, Boolean, Integer, ForeignKey, DECIMAL, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base

class Plan(Base):
    __tablename__ = "plans"
    
    id = Column(Integer, primary_key=True, autoincrement=True, comment="순번 ID (AUTO_INCREMENT)")
    name = Column(String(255), nullable=False)
    description = Column(Text)
    type = Column(String(50), nullable=False)
    price = Column(DECIMAL(10, 2), nullable=False)
    billing_cycle = Column(String(50), nullable=False)
    features = Column(JSON)
    max_clubs = Column(Integer)
    max_members_per_club = Column(Integer)
    max_meetings_per_month = Column(Integer)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
    # 관계 설정
    subscriptions = relationship("Subscription", back_populates="plan")

class Payment(Base):
    __tablename__ = "payments"
    
    id = Column(Integer, primary_key=True, autoincrement=True, comment="순번 ID (AUTO_INCREMENT)")
    payment_key = Column(String(255), unique=True)
    order_id = Column(String(255), unique=True)
    order_name = Column(String(255))
    amount = Column(Integer, nullable=False)
    status = Column(String(50), default="PENDING")
    method = Column(String(50))
    customer_key = Column(String(255))
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True, comment="회원별 결제 조회 (backend #51)")
    customer_name = Column(String(255))
    customer_email = Column(String(255))
    requested_at = Column(DateTime)
    approved_at = Column(DateTime)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

class UserPaymentMethod(Base):
    __tablename__ = "user_payment_methods"
    
    id = Column(Integer, primary_key=True, autoincrement=True, comment="순번 ID (AUTO_INCREMENT)")
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    method_type = Column(String(50), nullable=False)
    method_name = Column(String(255), nullable=False)
    masked_info = Column(String(255))
    is_default = Column(Boolean, default=False)
    status = Column(String(50), default="ACTIVE")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
    # 관계 설정
    user = relationship("User", backref="payment_methods")

class Subscription(Base):
    __tablename__ = "subscriptions"
    
    id = Column(Integer, primary_key=True, autoincrement=True, comment="순번 ID (AUTO_INCREMENT)")
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(50), default="ACTIVE")
    start_date = Column(DateTime, nullable=False)
    next_billing_date = Column(DateTime)
    end_date = Column(DateTime)
    billing_key_id = Column(Integer, ForeignKey("billing_keys.id"), comment="결제 수단 (backend #51)")
    canceled_at = Column(DateTime)
    cancel_reason = Column(String(200))
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
    # 관계 설정
    user = relationship("User", backref="subscriptions")
    plan = relationship("Plan", back_populates="subscriptions")

