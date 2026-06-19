import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime,
    ForeignKey, Boolean, Enum as SAEnum
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from enum import Enum as PyEnum


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tree_order.db")
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Role(str, PyEnum):
    INSPECTOR = "inspector"
    ADMIN = "admin"


class OrderStatus(str, PyEnum):
    PENDING_ASSIGN = "待派工"
    ASSIGNED = "已派工"
    IN_PROGRESS = "作业中"
    PENDING_REVIEW = "待复核"
    COMPLETED = "已完成"
    CANCELLED = "已撤销"


class RiskLevel(str, PyEnum):
    LOW = "低"
    MEDIUM = "中"
    HIGH = "高"
    CRITICAL = "紧急"


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    role = Column(SAEnum(Role), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Team(Base):
    __tablename__ = "teams"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    leader = Column(String(100), nullable=False)
    phone = Column(String(30))
    created_at = Column(DateTime, default=datetime.utcnow)


class Vehicle(Base):
    __tablename__ = "vehicles"
    id = Column(Integer, primary_key=True, index=True)
    plate = Column(String(30), unique=True, nullable=False)
    type = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)


class WorkOrder(Base):
    __tablename__ = "work_orders"
    id = Column(Integer, primary_key=True, index=True)
    order_no = Column(String(30), unique=True, nullable=False, index=True)

    road = Column(String(200), nullable=False)
    tree_no = Column(String(100), nullable=False)
    risk_level = Column(SAEnum(RiskLevel), nullable=False)
    suggested_time = Column(String(200))
    need_road_close = Column(Boolean, default=False, nullable=False)
    description = Column(Text)

    status = Column(SAEnum(OrderStatus), default=OrderStatus.PENDING_ASSIGN, nullable=False, index=True)

    reporter_id = Column(Integer, ForeignKey("users.id"))
    reporter = relationship("User", foreign_keys=[reporter_id])
    reported_at = Column(DateTime, default=datetime.utcnow)

    team_id = Column(Integer, ForeignKey("teams.id"))
    team = relationship("Team")
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"))
    vehicle = relationship("Vehicle")

    road_close_start = Column(DateTime)
    road_close_end = Column(DateTime)

    assignee_id = Column(Integer, ForeignKey("users.id"))
    assigned_at = Column(DateTime)

    started_at = Column(DateTime)
    start_operator_id = Column(Integer, ForeignKey("users.id"))

    submitted_at = Column(DateTime)
    submit_operator_id = Column(Integer, ForeignKey("users.id"))

    reviewed_at = Column(DateTime)
    reviewer_id = Column(Integer, ForeignKey("users.id"))
    review_note = Column(Text)

    cancelled_at = Column(DateTime)
    canceller_id = Column(Integer, ForeignKey("users.id"))
    cancel_reason = Column(Text)

    histories = relationship("StatusHistory", back_populates="order",
                             cascade="all, delete-orphan", lazy="selectin")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    action = Column(String(50), nullable=False, index=True)
    operator_id = Column(Integer, nullable=False)
    operator_name = Column(String(100))
    target_type = Column(String(50))
    target_id = Column(String(100))
    detail = Column(Text)
    snapshot_version = Column(String(30))
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class StatusHistory(Base):
    __tablename__ = "status_histories"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=False, index=True)
    from_status = Column(String(50))
    to_status = Column(String(50), nullable=False)
    operator_id = Column(Integer, ForeignKey("users.id"))
    operator_name = Column(String(100))
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    order = relationship("WorkOrder", back_populates="histories")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if not db.query(User).filter(User.username == "admin").first():
            db.add_all([
                User(username="admin", name="系统管理员", role=Role.ADMIN),
                User(username="inspector1", name="巡查员老张", role=Role.INSPECTOR),
                User(username="inspector2", name="巡查员小李", role=Role.INSPECTOR),
            ])
        if db.query(Team).count() == 0:
            db.add_all([
                Team(name="修剪一队", leader="王队长", phone="13800000001"),
                Team(name="修剪二队", leader="李队长", phone="13800000002"),
                Team(name="应急队", leader="赵队长", phone="13800000003"),
            ])
        if db.query(Vehicle).count() == 0:
            db.add_all([
                Vehicle(plate="京A·12345", type="高空作业车"),
                Vehicle(plate="京A·67890", type="运输卡车"),
                Vehicle(plate="京B·11111", type="高空作业车"),
            ])
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
