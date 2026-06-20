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


class RestoreBatchStatus(str, PyEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    REVOKED = "revoked"
    PARTIALLY_REVOKED = "partially_revoked"


class RestoreBatchItemAction(str, PyEnum):
    CREATE = "create"
    OVERWRITE = "overwrite"
    SKIP = "skip"
    REJECT = "reject"
    NOT_SELECTED = "not_selected"
    ERROR = "error"


class TraceEventType(str, PyEnum):
    REPORT = "report"
    ASSIGN = "assign"
    REASSIGN = "reassign"
    CANCEL = "cancel"
    START = "start"
    SUBMIT = "submit"
    COMPLETE = "complete"
    SNAPSHOT_CREATE = "snapshot_create"
    SNAPSHOT_OVERWRITE = "snapshot_overwrite"
    SNAPSHOT_SKIP = "snapshot_skip"
    SNAPSHOT_REJECT = "snapshot_reject"
    BATCH_REVOKE_DELETE = "batch_revoke_delete"
    BATCH_REVOKE_RESTORE = "batch_revoke_restore"
    BATCH_REVOKE_SKIP = "batch_revoke_skip"


class OrderTrace(Base):
    __tablename__ = "order_traces"
    id = Column(Integer, primary_key=True, index=True)
    trace_uid = Column(String(64), unique=True, nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=False, index=True)
    order_no = Column(String(30), nullable=False, index=True)
    event_type = Column(SAEnum(TraceEventType), nullable=False, index=True)
    operator_id = Column(Integer, nullable=False)
    operator_name = Column(String(100))
    operator_role = Column(String(20))
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    from_status = Column(String(50))
    to_status = Column(String(50))

    changed_fields_json = Column(Text)
    before_snapshot_json = Column(Text)
    after_snapshot_json = Column(Text)

    batch_id = Column(Integer, ForeignKey("restore_batches.id"), index=True)
    batch_no = Column(String(50))
    is_batch_operation = Column(Boolean, default=False, nullable=False)

    success = Column(Boolean, default=True, nullable=False)
    fail_reason = Column(Text)

    note = Column(Text)

    order = relationship("WorkOrder")
    batch = relationship("RestoreBatch")


class RestoreBatch(Base):
    __tablename__ = "restore_batches"
    id = Column(Integer, primary_key=True, index=True)
    batch_no = Column(String(50), unique=True, nullable=False, index=True)
    snapshot_version = Column(String(30))
    snapshot_exported_at = Column(String(100))
    snapshot_exported_by = Column(String(200))
    operator_id = Column(Integer, nullable=False)
    operator_name = Column(String(100))
    status = Column(SAEnum(RestoreBatchStatus), default=RestoreBatchStatus.COMPLETED, nullable=False)
    total_count = Column(Integer, default=0)
    imported_count = Column(Integer, default=0)
    skipped_count = Column(Integer, default=0)
    rejected_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    not_selected_count = Column(Integer, default=0)
    revoked_count = Column(Integer, default=0)
    detail_summary = Column(Text)
    raw_snapshot = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    revoked_at = Column(DateTime)
    revoked_by_id = Column(Integer)
    revoked_by_name = Column(String(100))
    revoke_reason = Column(Text)

    items = relationship("RestoreBatchItem", back_populates="batch",
                         cascade="all, delete-orphan", lazy="selectin")


class RestoreBatchItem(Base):
    __tablename__ = "restore_batch_items"
    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("restore_batches.id"), nullable=False, index=True)
    order_no = Column(String(30), nullable=False, index=True)
    action = Column(SAEnum(RestoreBatchItemAction), nullable=False)
    success = Column(Boolean, default=False)
    reason = Column(Text)
    order_id = Column(Integer)
    before_status = Column(String(50))
    after_status = Column(String(50))
    before_snapshot_json = Column(Text)
    after_snapshot_json = Column(Text)
    is_revoked = Column(Boolean, default=False)
    revoked_at = Column(DateTime)
    revoke_failed_reason = Column(Text)
    revoke_action = Column(String(50))
    revoke_result_reason = Column(Text)
    revoke_changed_fields = Column(Text)

    batch = relationship("RestoreBatch", back_populates="items")


class VerificationStatus(str, PyEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class VerificationTask(Base):
    __tablename__ = "verification_tasks"
    id = Column(Integer, primary_key=True, index=True)
    task_no = Column(String(50), unique=True, nullable=False, index=True)
    task_type = Column(String(30), nullable=False, default="order_trace")
    target_order_no = Column(String(30), index=True)
    target_order_id = Column(Integer, index=True)
    target_batch_id = Column(Integer, index=True)
    target_batch_no = Column(String(50), index=True)
    status = Column(SAEnum(VerificationStatus), default=VerificationStatus.PENDING, nullable=False, index=True)
    operator_id = Column(Integer, nullable=False)
    operator_name = Column(String(100))
    result_summary = Column(Text)
    result_json = Column(Text)
    conflict_count = Column(Integer, default=0)
    event_count = Column(Integer, default=0)
    failed_event_count = Column(Integer, default=0)
    batch_count = Column(Integer, default=0)
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime)
    rerun_count = Column(Integer, default=0)
    last_rerun_at = Column(DateTime)
    last_rerun_by_id = Column(Integer)
    last_rerun_by_name = Column(String(100))
    export_count = Column(Integer, default=0)
    last_export_at = Column(DateTime)
    last_export_by_id = Column(Integer)
    last_export_by_name = Column(String(100))


class SystemConfig(Base):
    __tablename__ = "system_configs"
    id = Column(Integer, primary_key=True, index=True)
    config_key = Column(String(100), unique=True, nullable=False, index=True)
    config_value = Column(Text)
    config_type = Column(String(20), default="string")
    description = Column(String(200))
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(100))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migrate_restore_batch_items():
    from sqlalchemy import text
    db = SessionLocal()
    try:
        result = db.execute(text("PRAGMA table_info(restore_batch_items)")).fetchall()
        existing_cols = {row[1] for row in result}

        needed_cols = {
            "revoke_action": "VARCHAR(50)",
            "revoke_result_reason": "TEXT",
            "revoke_changed_fields": "TEXT",
        }

        for col_name, col_type in needed_cols.items():
            if col_name not in existing_cols:
                db.execute(text(f"ALTER TABLE restore_batch_items ADD COLUMN {col_name} {col_type}"))

        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _ensure_order_traces_table():
    from sqlalchemy import text
    db = SessionLocal()
    try:
        result = db.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='order_traces'")).fetchone()
        if not result:
            db.execute(text("""
                CREATE TABLE order_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_uid VARCHAR(64) NOT NULL UNIQUE,
                    order_id INTEGER NOT NULL,
                    order_no VARCHAR(30) NOT NULL,
                    event_type VARCHAR(50) NOT NULL,
                    operator_id INTEGER NOT NULL,
                    operator_name VARCHAR(100),
                    operator_role VARCHAR(20),
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    from_status VARCHAR(50),
                    to_status VARCHAR(50),
                    changed_fields_json TEXT,
                    before_snapshot_json TEXT,
                    after_snapshot_json TEXT,
                    batch_id INTEGER,
                    batch_no VARCHAR(50),
                    is_batch_operation BOOLEAN DEFAULT 0 NOT NULL,
                    success BOOLEAN DEFAULT 1 NOT NULL,
                    fail_reason TEXT,
                    note TEXT
                )
            """))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_order_traces_id ON order_traces(id)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_order_traces_trace_uid ON order_traces(trace_uid)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_order_traces_order_id ON order_traces(order_id)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_order_traces_order_no ON order_traces(order_no)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_order_traces_event_type ON order_traces(event_type)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_order_traces_created_at ON order_traces(created_at)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_order_traces_batch_id ON order_traces(batch_id)"))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _ensure_verification_tables():
    from sqlalchemy import text
    db = SessionLocal()
    try:
        result = db.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='verification_tasks'")).fetchone()
        if not result:
            db.execute(text("""
                CREATE TABLE verification_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_no VARCHAR(50) NOT NULL UNIQUE,
                    task_type VARCHAR(30) NOT NULL DEFAULT 'order_trace',
                    target_order_no VARCHAR(30),
                    target_order_id INTEGER,
                    target_batch_id INTEGER,
                    target_batch_no VARCHAR(50),
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    operator_id INTEGER NOT NULL,
                    operator_name VARCHAR(100),
                    result_summary TEXT,
                    result_json TEXT,
                    conflict_count INTEGER DEFAULT 0,
                    event_count INTEGER DEFAULT 0,
                    failed_event_count INTEGER DEFAULT 0,
                    batch_count INTEGER DEFAULT 0,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    completed_at DATETIME,
                    rerun_count INTEGER DEFAULT 0,
                    last_rerun_at DATETIME,
                    last_rerun_by_id INTEGER,
                    last_rerun_by_name VARCHAR(100),
                    export_count INTEGER DEFAULT 0,
                    last_export_at DATETIME,
                    last_export_by_id INTEGER,
                    last_export_by_name VARCHAR(100)
                )
            """))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_verification_tasks_id ON verification_tasks(id)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_verification_tasks_task_no ON verification_tasks(task_no)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_verification_tasks_target_order_no ON verification_tasks(target_order_no)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_verification_tasks_target_order_id ON verification_tasks(target_order_id)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_verification_tasks_target_batch_id ON verification_tasks(target_batch_id)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_verification_tasks_status ON verification_tasks(status)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_verification_tasks_created_at ON verification_tasks(created_at)"))

        result = db.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='system_configs'")).fetchone()
        if not result:
            db.execute(text("""
                CREATE TABLE system_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_key VARCHAR(100) NOT NULL UNIQUE,
                    config_value TEXT,
                    config_type VARCHAR(20) DEFAULT 'string',
                    description VARCHAR(200),
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_by VARCHAR(100)
                )
            """))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_system_configs_id ON system_configs(id)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_system_configs_config_key ON system_configs(config_key)"))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _init_default_configs():
    db = SessionLocal()
    try:
        defaults = [
            ("verification_retention_days", "30", "int", "校验任务保留天数，超过此天数的任务将被清理"),
            ("verification_export_enabled", "true", "bool", "校验结果导出功能开关"),
            ("verification_auto_clean_enabled", "true", "bool", "校验任务自动清理开关"),
            ("trace_retention_days", "90", "int", "工单追溯记录保留天数"),
        ]
        for key, value, type_, desc in defaults:
            existing = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
            if not existing:
                db.add(SystemConfig(
                    config_key=key,
                    config_value=value,
                    config_type=type_,
                    description=desc,
                    updated_by="system_init",
                ))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate_restore_batch_items()
    _ensure_order_traces_table()
    _ensure_verification_tables()
    _init_default_configs()
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
