import csv
import io
import json
import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from database import (
    get_db, init_db, Role, OrderStatus, RiskLevel,
    User, Team, Vehicle, WorkOrder, StatusHistory, AuditLog,
    RestoreBatch, RestoreBatchItem, RestoreBatchStatus, RestoreBatchItemAction,
    OrderTrace, TraceEventType,
    VerificationTask, VerificationStatus, SystemConfig,
)


app = FastAPI(title="城市树木修剪工单系统", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


# ---------- auth helpers ----------

def _current_user(
    x_user_id: Optional[int] = Header(None),
    db: Session = Depends(get_db),
) -> User:
    if x_user_id is None:
        raise HTTPException(status_code=401, detail="未登录 (请在请求头 X-User-Id 传入用户ID)")
    user = db.query(User).filter(User.id == x_user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user


def _require_role(user: User, role: Role):
    if user.role != role:
        raise HTTPException(status_code=403, detail=f"权限不足：需要{role.value}角色")


# ---------- Pydantic schemas ----------

class UserOut(BaseModel):
    id: int
    username: str
    name: str
    role: str

    class Config:
        from_attributes = True


class TeamOut(BaseModel):
    id: int
    name: str
    leader: str
    phone: Optional[str] = None

    class Config:
        from_attributes = True


class VehicleOut(BaseModel):
    id: int
    plate: str
    type: Optional[str] = None

    class Config:
        from_attributes = True


class HistoryOut(BaseModel):
    id: int
    from_status: Optional[str]
    to_status: str
    operator_name: Optional[str]
    note: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class OrderReportIn(BaseModel):
    road: str = Field(..., min_length=1, description="道路名称")
    tree_no: str = Field(..., min_length=1, description="树木编号")
    risk_level: RiskLevel
    suggested_time: Optional[str] = None
    need_road_close: bool = False
    description: Optional[str] = None


class OrderAssignIn(BaseModel):
    team_id: int
    vehicle_id: Optional[int] = None
    road_close_start: Optional[datetime] = None
    road_close_end: Optional[datetime] = None


class OrderCancelIn(BaseModel):
    reason: str = Field(..., min_length=1, description="撤销原因")


class OrderReviewIn(BaseModel):
    review_note: Optional[str] = None


class OrderOut(BaseModel):
    id: int
    order_no: str
    road: str
    tree_no: str
    risk_level: str
    suggested_time: Optional[str]
    need_road_close: bool
    description: Optional[str]
    status: str
    reporter_id: Optional[int]
    reporter_name: Optional[str]
    reported_at: Optional[datetime]
    team_id: Optional[int]
    team_name: Optional[str]
    vehicle_id: Optional[int]
    vehicle_plate: Optional[str]
    road_close_start: Optional[datetime]
    road_close_end: Optional[datetime]
    assigned_at: Optional[datetime]
    started_at: Optional[datetime]
    submitted_at: Optional[datetime]
    reviewed_at: Optional[datetime]
    cancelled_at: Optional[datetime]
    cancel_reason: Optional[str]
    histories: List[HistoryOut] = []

    class Config:
        from_attributes = True


def _to_order_out(o: WorkOrder) -> OrderOut:
    return OrderOut(
        id=o.id,
        order_no=o.order_no,
        road=o.road,
        tree_no=o.tree_no,
        risk_level=o.risk_level.value if hasattr(o.risk_level, "value") else str(o.risk_level),
        suggested_time=o.suggested_time,
        need_road_close=o.need_road_close,
        description=o.description,
        status=o.status.value if hasattr(o.status, "value") else str(o.status),
        reporter_id=o.reporter_id,
        reporter_name=o.reporter.name if o.reporter else None,
        reported_at=o.reported_at,
        team_id=o.team_id,
        team_name=o.team.name if o.team else None,
        vehicle_id=o.vehicle_id,
        vehicle_plate=o.vehicle.plate if o.vehicle else None,
        road_close_start=o.road_close_start,
        road_close_end=o.road_close_end,
        assigned_at=o.assigned_at,
        started_at=o.started_at,
        submitted_at=o.submitted_at,
        reviewed_at=o.reviewed_at,
        cancelled_at=o.cancelled_at,
        cancel_reason=o.cancel_reason,
        histories=[HistoryOut.model_validate(h) for h in (o.histories or [])],
    )


# ---------- state helpers ----------

STATUS_FLOW = {
    OrderStatus.PENDING_ASSIGN: [OrderStatus.ASSIGNED, OrderStatus.CANCELLED],
    OrderStatus.ASSIGNED: [OrderStatus.IN_PROGRESS, OrderStatus.CANCELLED],
    OrderStatus.IN_PROGRESS: [OrderStatus.PENDING_REVIEW, OrderStatus.CANCELLED],
    OrderStatus.PENDING_REVIEW: [OrderStatus.COMPLETED, OrderStatus.IN_PROGRESS, OrderStatus.CANCELLED],
    OrderStatus.COMPLETED: [],
    OrderStatus.CANCELLED: [],
}


def _transition_check(from_status: OrderStatus, to_status: OrderStatus):
    allowed = STATUS_FLOW.get(from_status, [])
    if to_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"状态流转非法：{from_status.value} 不能转为 {to_status.value}（允许目标: {[s.value for s in allowed]}）",
        )


def _record_history(db: Session, order: WorkOrder, from_status, to_status: OrderStatus,
                    operator: User, note: Optional[str] = None):
    h = StatusHistory(
        order_id=order.id,
        from_status=from_status.value if hasattr(from_status, "value") else (str(from_status) if from_status else None),
        to_status=to_status.value,
        operator_id=operator.id,
        operator_name=operator.name,
        note=note,
        created_at=datetime.utcnow(),
    )
    db.add(h)


def _diff_snapshots(before: dict, after: dict) -> List[str]:
    changed = []
    ignore = {"id", "order_no"}
    for k in after:
        if k in ignore:
            continue
        if before.get(k) != after.get(k):
            changed.append(k)
    for k in before:
        if k in ignore:
            continue
        if k not in after and before.get(k) is not None:
            changed.append(k)
    return changed


def _record_trace(
    db: Session,
    order: WorkOrder,
    event_type: TraceEventType,
    operator: User,
    *,
    from_status=None,
    to_status=None,
    before_snap: Optional[dict] = None,
    after_snap: Optional[dict] = None,
    batch_id: Optional[int] = None,
    batch_no: Optional[str] = None,
    is_batch: bool = False,
    success: bool = True,
    fail_reason: Optional[str] = None,
    note: Optional[str] = None,
    changed_fields: Optional[List[str]] = None,
    idempotency_key: Optional[str] = None,
):
    if idempotency_key:
        trace_uid = f"{order.order_no}-{event_type.value}-{idempotency_key}"
    else:
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        rand = uuid.uuid4().hex[:8]
        trace_uid = f"{order.order_no}-{event_type.value}-{ts}-{rand}"

    existing = db.query(OrderTrace).filter(OrderTrace.trace_uid == trace_uid).first()
    if existing:
        return existing

    if changed_fields is None and before_snap and after_snap:
        changed_fields = _diff_snapshots(before_snap, after_snap)

    from_status_val = from_status
    if hasattr(from_status, "value"):
        from_status_val = from_status.value
    elif from_status is not None:
        from_status_val = str(from_status)

    to_status_val = to_status
    if hasattr(to_status, "value"):
        to_status_val = to_status.value
    elif to_status is not None:
        to_status_val = str(to_status)

    trace = OrderTrace(
        trace_uid=trace_uid,
        order_id=order.id,
        order_no=order.order_no,
        event_type=event_type,
        operator_id=operator.id,
        operator_name=operator.name,
        operator_role=operator.role.value if hasattr(operator.role, "value") else str(operator.role),
        created_at=datetime.utcnow(),
        from_status=from_status_val,
        to_status=to_status_val,
        changed_fields_json=json.dumps(changed_fields, ensure_ascii=False) if changed_fields else None,
        before_snapshot_json=json.dumps(before_snap, ensure_ascii=False) if before_snap else None,
        after_snapshot_json=json.dumps(after_snap, ensure_ascii=False) if after_snap else None,
        batch_id=batch_id,
        batch_no=batch_no,
        is_batch_operation=is_batch,
        success=success,
        fail_reason=fail_reason,
        note=note,
    )
    db.add(trace)
    db.flush()
    return trace


# ---------- conflict checks ----------

def _check_team_time_conflict(db: Session, team_id: int,
                              start: datetime, end: datetime,
                              exclude_order_id: Optional[int] = None):
    if not start or not end:
        return
    if end <= start:
        raise HTTPException(status_code=400, detail="封路结束时间必须晚于开始时间")

    q = db.query(WorkOrder).filter(
        WorkOrder.team_id == team_id,
        WorkOrder.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW]),
        WorkOrder.road_close_start.isnot(None),
        WorkOrder.road_close_end.isnot(None),
        or_(
            and_(WorkOrder.road_close_start <= start, start < WorkOrder.road_close_end),
            and_(WorkOrder.road_close_start < end, end <= WorkOrder.road_close_end),
            and_(start <= WorkOrder.road_close_start, WorkOrder.road_close_end <= end),
        ),
    )
    if exclude_order_id is not None:
        q = q.filter(WorkOrder.id != exclude_order_id)
    conflict = q.first()
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=f"队伍时间冲突：队伍ID={team_id} 在 {conflict.road_close_start} ~ {conflict.road_close_end} "
                   f"已被工单 {conflict.order_no} 占用，与请求窗口 {start} ~ {end} 重叠",
        )


def _check_road_close_conflict(db: Session, road: str,
                               start: datetime, end: datetime,
                               exclude_order_id: Optional[int] = None):
    if not start or not end or not road:
        return
    if end <= start:
        raise HTTPException(status_code=400, detail="封路结束时间必须晚于开始时间")

    q = db.query(WorkOrder).filter(
        WorkOrder.road == road,
        WorkOrder.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW]),
        WorkOrder.road_close_start.isnot(None),
        WorkOrder.road_close_end.isnot(None),
        or_(
            and_(WorkOrder.road_close_start <= start, start < WorkOrder.road_close_end),
            and_(WorkOrder.road_close_start < end, end <= WorkOrder.road_close_end),
            and_(start <= WorkOrder.road_close_start, WorkOrder.road_close_end <= end),
        ),
    )
    if exclude_order_id is not None:
        q = q.filter(WorkOrder.id != exclude_order_id)
    conflict = q.first()
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=f"同路段封路窗口冲突：路段「{road}」在 {conflict.road_close_start} ~ {conflict.road_close_end} "
                   f"已被工单 {conflict.order_no} 占用，与请求窗口 {start} ~ {end} 重叠",
        )


def _check_vehicle_time_conflict(db: Session, vehicle_id: Optional[int],
                                 start: datetime, end: datetime,
                                 exclude_order_id: Optional[int] = None):
    if vehicle_id is None or not start or not end:
        return
    if end <= start:
        raise HTTPException(status_code=400, detail="封路结束时间必须晚于开始时间")

    q = db.query(WorkOrder).filter(
        WorkOrder.vehicle_id == vehicle_id,
        WorkOrder.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW]),
        WorkOrder.road_close_start.isnot(None),
        WorkOrder.road_close_end.isnot(None),
        or_(
            and_(WorkOrder.road_close_start <= start, start < WorkOrder.road_close_end),
            and_(WorkOrder.road_close_start < end, end <= WorkOrder.road_close_end),
            and_(start <= WorkOrder.road_close_start, WorkOrder.road_close_end <= end),
        ),
    )
    if exclude_order_id is not None:
        q = q.filter(WorkOrder.id != exclude_order_id)
    conflict = q.first()
    if conflict:
        v = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
        v_info = f"{v.plate}（ID={vehicle_id}）" if v else f"ID={vehicle_id}"
        raise HTTPException(
            status_code=409,
            detail=f"车辆排班冲突：车辆 {v_info} 在 {conflict.road_close_start} ~ {conflict.road_close_end} "
                   f"已被工单 {conflict.order_no} 占用，与请求窗口 {start} ~ {end} 重叠",
        )


# ---------- APIs ----------

@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/api/me")
def api_me(user: User = Depends(_current_user)):
    return {"id": user.id, "username": user.username, "name": user.name, "role": user.role.value}


@app.get("/api/users", response_model=List[UserOut])
def list_users(role: Optional[Role] = None, db: Session = Depends(get_db)):
    q = db.query(User)
    if role:
        q = q.filter(User.role == role)
    return q.order_by(User.id).all()


@app.get("/api/teams", response_model=List[TeamOut])
def list_teams(db: Session = Depends(get_db)):
    return db.query(Team).order_by(Team.id).all()


@app.get("/api/vehicles", response_model=List[VehicleOut])
def list_vehicles(db: Session = Depends(get_db)):
    return db.query(Vehicle).order_by(Vehicle.id).all()


@app.get("/api/orders", response_model=List[OrderOut])
def list_orders(
    status: Optional[OrderStatus] = None,
    road: Optional[str] = None,
    reporter_id: Optional[int] = None,
    team_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    q = db.query(WorkOrder)
    if status:
        q = q.filter(WorkOrder.status == status)
    if road:
        q = q.filter(WorkOrder.road.contains(road))
    if reporter_id:
        q = q.filter(WorkOrder.reporter_id == reporter_id)
    if team_id:
        q = q.filter(WorkOrder.team_id == team_id)
    orders = q.order_by(WorkOrder.id.desc()).all()
    return [_to_order_out(o) for o in orders]


@app.get("/api/orders/{order_id}", response_model=OrderOut)
def get_order(order_id: int, db: Session = Depends(get_db)):
    o = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not o:
        raise HTTPException(status_code=404, detail="工单不存在")
    return _to_order_out(o)


@app.post("/api/orders/report", response_model=OrderOut, status_code=201)
def report_order(data: OrderReportIn, user: User = Depends(_current_user),
                 db: Session = Depends(get_db)):
    _require_role(user, Role.INSPECTOR)

    order_no = f"WO{datetime.now().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:4].upper()}"
    o = WorkOrder(
        order_no=order_no,
        road=data.road.strip(),
        tree_no=data.tree_no.strip(),
        risk_level=data.risk_level,
        suggested_time=data.suggested_time,
        need_road_close=data.need_road_close,
        description=data.description,
        status=OrderStatus.PENDING_ASSIGN,
        reporter_id=user.id,
        reported_at=datetime.utcnow(),
    )
    db.add(o)
    db.flush()
    _record_history(db, o, None, OrderStatus.PENDING_ASSIGN, user, note="巡查员上报工单")
    after_snap = _serialize_order_snapshot(o)
    _record_trace(db, o, TraceEventType.REPORT, user,
                  from_status=None, to_status=OrderStatus.PENDING_ASSIGN,
                  before_snap=None, after_snap=after_snap,
                  note="巡查员上报工单")
    db.commit()
    db.refresh(o)
    return _to_order_out(o)


@app.post("/api/orders/{order_id}/assign", response_model=OrderOut)
def assign_order(order_id: int, data: OrderAssignIn,
                 user: User = Depends(_current_user),
                 db: Session = Depends(get_db)):
    _require_role(user, Role.ADMIN)
    o = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not o:
        raise HTTPException(status_code=404, detail="工单不存在")

    is_reassign = (o.status == OrderStatus.ASSIGNED)
    if not is_reassign:
        _transition_check(o.status, OrderStatus.ASSIGNED)

    team = db.query(Team).filter(Team.id == data.team_id).first()
    if not team:
        raise HTTPException(status_code=400, detail=f"队伍ID={data.team_id}不存在")

    vehicle = None
    if data.vehicle_id is not None:
        vehicle = db.query(Vehicle).filter(Vehicle.id == data.vehicle_id).first()
        if not vehicle:
            raise HTTPException(status_code=400, detail=f"车辆ID={data.vehicle_id}不存在")

    _check_team_time_conflict(db, data.team_id, data.road_close_start, data.road_close_end,
                              exclude_order_id=o.id)
    _check_road_close_conflict(db, o.road, data.road_close_start, data.road_close_end,
                                exclude_order_id=o.id)
    _check_vehicle_time_conflict(db, data.vehicle_id, data.road_close_start, data.road_close_end,
                                 exclude_order_id=o.id)

    old_status = o.status
    note_suffix = "改派" if is_reassign else "派工"
    before_snap = _serialize_order_snapshot(o)
    if not is_reassign:
        o.status = OrderStatus.ASSIGNED
        o.assigned_at = datetime.utcnow()
    o.team_id = data.team_id
    o.vehicle_id = data.vehicle_id
    o.road_close_start = data.road_close_start
    o.road_close_end = data.road_close_end
    o.assignee_id = user.id
    _record_history(db, o, old_status, OrderStatus.ASSIGNED, user,
                    note=f"{note_suffix}：队伍={team.name}，车辆={vehicle.plate if vehicle else '无'}")
    db.flush()
    after_snap = _serialize_order_snapshot(o)
    event_type = TraceEventType.REASSIGN if is_reassign else TraceEventType.ASSIGN
    _record_trace(db, o, event_type, user,
                  from_status=old_status, to_status=OrderStatus.ASSIGNED,
                  before_snap=before_snap, after_snap=after_snap,
                  note=f"{note_suffix}：队伍={team.name}，车辆={vehicle.plate if vehicle else '无'}")
    db.commit()
    db.refresh(o)
    return _to_order_out(o)


@app.post("/api/orders/{order_id}/start", response_model=OrderOut)
def start_order(order_id: int, user: User = Depends(_current_user),
                db: Session = Depends(get_db)):
    o = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not o:
        raise HTTPException(status_code=404, detail="工单不存在")
    _transition_check(o.status, OrderStatus.IN_PROGRESS)
    old_status = o.status
    before_snap = _serialize_order_snapshot(o)
    o.status = OrderStatus.IN_PROGRESS
    o.started_at = datetime.utcnow()
    o.start_operator_id = user.id
    _record_history(db, o, old_status, OrderStatus.IN_PROGRESS, user, note="开始作业")
    db.flush()
    after_snap = _serialize_order_snapshot(o)
    _record_trace(db, o, TraceEventType.START, user,
                  from_status=old_status, to_status=OrderStatus.IN_PROGRESS,
                  before_snap=before_snap, after_snap=after_snap,
                  note="开始作业")
    db.commit()
    db.refresh(o)
    return _to_order_out(o)


@app.post("/api/orders/{order_id}/submit", response_model=OrderOut)
def submit_order(order_id: int, user: User = Depends(_current_user),
                 db: Session = Depends(get_db)):
    o = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not o:
        raise HTTPException(status_code=404, detail="工单不存在")
    _transition_check(o.status, OrderStatus.PENDING_REVIEW)
    old_status = o.status
    before_snap = _serialize_order_snapshot(o)
    o.status = OrderStatus.PENDING_REVIEW
    o.submitted_at = datetime.utcnow()
    o.submit_operator_id = user.id
    _record_history(db, o, old_status, OrderStatus.PENDING_REVIEW, user, note="作业完成，提交复核")
    db.flush()
    after_snap = _serialize_order_snapshot(o)
    _record_trace(db, o, TraceEventType.SUBMIT, user,
                  from_status=old_status, to_status=OrderStatus.PENDING_REVIEW,
                  before_snap=before_snap, after_snap=after_snap,
                  note="作业完成，提交复核")
    db.commit()
    db.refresh(o)
    return _to_order_out(o)


@app.post("/api/orders/{order_id}/complete", response_model=OrderOut)
def complete_order(order_id: int, data: OrderReviewIn = OrderReviewIn(),
                   user: User = Depends(_current_user),
                   db: Session = Depends(get_db)):
    # 关键规则：巡查员不能审批完成（越权）
    if user.role == Role.INSPECTOR:
        raise HTTPException(
            status_code=403,
            detail=f"越权命中规则：巡查员({user.name}/{user.username}) 不得审批完成工单，须由管理员完成复核"
        )
    _require_role(user, Role.ADMIN)

    o = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not o:
        raise HTTPException(status_code=404, detail="工单不存在")
    _transition_check(o.status, OrderStatus.COMPLETED)
    old_status = o.status
    before_snap = _serialize_order_snapshot(o)
    o.status = OrderStatus.COMPLETED
    o.reviewed_at = datetime.utcnow()
    o.reviewer_id = user.id
    o.review_note = data.review_note
    _record_history(db, o, old_status, OrderStatus.COMPLETED, user,
                    note=f"复核通过，工单完成。备注：{data.review_note or '无'}")
    db.flush()
    after_snap = _serialize_order_snapshot(o)
    _record_trace(db, o, TraceEventType.COMPLETE, user,
                  from_status=old_status, to_status=OrderStatus.COMPLETED,
                  before_snap=before_snap, after_snap=after_snap,
                  note=f"复核通过，工单完成。备注：{data.review_note or '无'}")
    db.commit()
    db.refresh(o)
    return _to_order_out(o)


@app.post("/api/orders/{order_id}/cancel", response_model=OrderOut)
def cancel_order(order_id: int, data: OrderCancelIn,
                 user: User = Depends(_current_user),
                 db: Session = Depends(get_db)):
    _require_role(user, Role.ADMIN)
    o = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not o:
        raise HTTPException(status_code=404, detail="工单不存在")
    if o.status == OrderStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"状态非法：已完成工单({o.order_no}) 不能被撤销或悄悄改回待派工，当前状态={o.status.value}"
        )
    _transition_check(o.status, OrderStatus.CANCELLED)
    old_status = o.status
    before_snap = _serialize_order_snapshot(o)
    o.status = OrderStatus.CANCELLED
    o.cancelled_at = datetime.utcnow()
    o.canceller_id = user.id
    o.cancel_reason = data.reason.strip()
    _record_history(db, o, old_status, OrderStatus.CANCELLED, user,
                    note=f"撤销工单，原因：{data.reason.strip()}")
    db.flush()
    after_snap = _serialize_order_snapshot(o)
    _record_trace(db, o, TraceEventType.CANCEL, user,
                  from_status=old_status, to_status=OrderStatus.CANCELLED,
                  before_snap=before_snap, after_snap=after_snap,
                  note=f"撤销工单，原因：{data.reason.strip()}")
    db.commit()
    db.refresh(o)
    return _to_order_out(o)


# ---------- Export ----------

def _serialize_orders_for_export(db: Session) -> List[dict]:
    orders = db.query(WorkOrder).order_by(WorkOrder.id).all()
    rows = []
    for o in orders:
        rows.append({
            "id": o.id,
            "order_no": o.order_no,
            "road": o.road,
            "tree_no": o.tree_no,
            "risk_level": o.risk_level.value if hasattr(o.risk_level, "value") else str(o.risk_level),
            "suggested_time": o.suggested_time or "",
            "need_road_close": "是" if o.need_road_close else "否",
            "description": o.description or "",
            "status": o.status.value if hasattr(o.status, "value") else str(o.status),
            "reporter": o.reporter.name if o.reporter else "",
            "reported_at": o.reported_at.isoformat() if o.reported_at else "",
            "team": o.team.name if o.team else "",
            "vehicle": o.vehicle.plate if o.vehicle else "",
            "road_close_start": o.road_close_start.isoformat() if o.road_close_start else "",
            "road_close_end": o.road_close_end.isoformat() if o.road_close_end else "",
            "assigned_at": o.assigned_at.isoformat() if o.assigned_at else "",
            "started_at": o.started_at.isoformat() if o.started_at else "",
            "submitted_at": o.submitted_at.isoformat() if o.submitted_at else "",
            "reviewed_at": o.reviewed_at.isoformat() if o.reviewed_at else "",
            "review_note": o.review_note or "",
            "cancelled_at": o.cancelled_at.isoformat() if o.cancelled_at else "",
            "cancel_reason": o.cancel_reason or "",
            "history_count": len(o.histories or []),
            "histories": [
                {
                    "from": h.from_status or "",
                    "to": h.to_status,
                    "operator": h.operator_name or "",
                    "note": h.note or "",
                    "at": h.created_at.isoformat() if h.created_at else "",
                }
                for h in (o.histories or [])
            ],
        })
    return rows


@app.get("/api/export/csv")
def export_csv(db: Session = Depends(get_db), user: User = Depends(_current_user)):
    rows = _serialize_orders_for_export(db)
    if not rows:
        headers = []
    else:
        headers = [k for k in rows[0].keys() if k != "histories"]
        headers.append("histories_json")

    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        row = {k: r.get(k, "") for k in headers if k != "histories_json"}
        row["histories_json"] = json.dumps(r.get("histories", []), ensure_ascii=False)
        writer.writerow(row)

    content = buf.getvalue().encode("utf-8-sig")
    resp = StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
    )
    fname = f"work_orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


SNAPSHOT_VERSION = "1.0.0"


@app.get("/api/export/json")
def export_json(db: Session = Depends(get_db), user: User = Depends(_current_user)):
    latest_batch = db.query(RestoreBatch).order_by(RestoreBatch.id.desc()).first()
    batch_info = None
    if latest_batch:
        batch_info = {
            "batch_id": latest_batch.id,
            "batch_no": latest_batch.batch_no,
            "snapshot_version": latest_batch.snapshot_version,
            "operator_name": latest_batch.operator_name,
            "created_at": latest_batch.created_at.isoformat() if latest_batch.created_at else None,
            "status": latest_batch.status.value if hasattr(latest_batch.status, "value") else str(latest_batch.status),
            "total_count": latest_batch.total_count,
            "imported_count": latest_batch.imported_count,
        }

    payload = {
        "snapshot_version": SNAPSHOT_VERSION,
        "exported_at": datetime.utcnow().isoformat(),
        "exported_by": {"id": user.id, "name": user.name, "role": user.role.value},
        "total": None,
        "orders": None,
        "traceability": {
            "last_restore_batch": batch_info,
            "export_purpose": "snapshot_backup",
        },
    }
    payload["orders"] = _serialize_orders_for_export(db)
    payload["total"] = len(payload["orders"])
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    log = AuditLog(
        action="export_json",
        operator_id=user.id,
        operator_name=user.name,
        target_type="snapshot",
        target_id="all",
        detail=f"导出 {payload['total']} 条工单",
        snapshot_version=SNAPSHOT_VERSION,
    )
    db.add(log)
    db.commit()

    resp = StreamingResponse(
        iter([content]),
        media_type="application/json; charset=utf-8",
    )
    fname = f"work_orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


@app.get("/api/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}


# ---------- Snapshot Restore ----------

STATUS_RANK = {
    OrderStatus.PENDING_ASSIGN: 0,
    OrderStatus.ASSIGNED: 1,
    OrderStatus.IN_PROGRESS: 2,
    OrderStatus.PENDING_REVIEW: 3,
    OrderStatus.COMPLETED: 4,
    OrderStatus.CANCELLED: 5,
}


def _parse_iso(val):
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None


def _serialize_order_snapshot(o: WorkOrder) -> dict:
    def _dt_iso(val):
        if not val:
            return ""
        if hasattr(val, "tzinfo") and val.tzinfo is not None:
            val = val.replace(tzinfo=None)
        return val.isoformat()

    return {
        "id": o.id,
        "order_no": o.order_no,
        "road": o.road,
        "tree_no": o.tree_no,
        "risk_level": o.risk_level.value if hasattr(o.risk_level, "value") else str(o.risk_level),
        "suggested_time": o.suggested_time or "",
        "need_road_close": o.need_road_close,
        "description": o.description or "",
        "status": o.status.value if hasattr(o.status, "value") else str(o.status),
        "team_id": o.team_id,
        "vehicle_id": o.vehicle_id,
        "road_close_start": _dt_iso(o.road_close_start),
        "road_close_end": _dt_iso(o.road_close_end),
        "reported_at": _dt_iso(o.reported_at),
        "assigned_at": _dt_iso(o.assigned_at),
        "started_at": _dt_iso(o.started_at),
        "submitted_at": _dt_iso(o.submitted_at),
        "reviewed_at": _dt_iso(o.reviewed_at),
        "review_note": o.review_note or "",
        "cancelled_at": _dt_iso(o.cancelled_at),
        "cancel_reason": o.cancel_reason or "",
    }


def _generate_batch_no() -> str:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    rand = uuid.uuid4().hex[:6].upper()
    return f"RST-{ts}-{rand}"


def _detect_conflicts(item: dict, db: Session) -> List[dict]:
    conflicts = []
    order_no = item.get("order_no", "")
    snap_status_str = item.get("status", "")
    snap_team = item.get("team", "")
    snap_vehicle = item.get("vehicle", "")
    snap_rcs = _parse_iso(item.get("road_close_start"))
    snap_rce = _parse_iso(item.get("road_close_end"))

    existing = db.query(WorkOrder).filter(WorkOrder.order_no == order_no).first()

    if existing:
        conflicts.append({
            "type": "duplicate_order_no",
            "message": f"工单号 {order_no} 已存在（当前状态={existing.status.value}）",
            "existing_status": existing.status.value,
            "existing_id": existing.id,
        })

        try:
            snap_rank = STATUS_RANK[OrderStatus(snap_status_str)]
        except ValueError:
            snap_rank = -1
        existing_rank = STATUS_RANK.get(existing.status, 0)

        if existing.status == OrderStatus.COMPLETED:
            conflicts.append({
                "type": "completed_record",
                "message": f"工单号 {order_no} 在系统中已完成，禁止覆盖",
                "action": "reject",
            })
        elif existing.status == OrderStatus.CANCELLED:
            conflicts.append({
                "type": "cancelled_record",
                "message": f"工单号 {order_no} 在系统中已撤销，禁止覆盖",
                "action": "reject",
            })
        elif snap_rank < existing_rank:
            conflicts.append({
                "type": "status_regression",
                "message": f"快照状态「{snap_status_str}」落后于当前状态「{existing.status.value}」，属于状态倒退",
                "action": "reject",
            })
        else:
            conflicts.append({
                "type": "duplicate_overwritable",
                "message": f"工单号 {order_no} 已存在，快照状态「{snap_status_str}」可覆盖当前「{existing.status.value}」",
                "action": "overwrite_or_skip",
            })
    else:
        if snap_status_str in (OrderStatus.COMPLETED.value, OrderStatus.CANCELLED.value):
            conflicts.append({
                "type": "terminal_status_new",
                "message": f"快照中工单 {order_no} 状态为「{snap_status_str}」，属于终态记录，导入时将跳过",
                "action": "skip",
            })

    if snap_rcs and snap_rce and snap_team:
        team_obj = db.query(Team).filter(Team.name == snap_team).first()
        if team_obj:
            conflict_order = db.query(WorkOrder).filter(
                WorkOrder.team_id == team_obj.id,
                WorkOrder.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW]),
                WorkOrder.road_close_start.isnot(None),
                WorkOrder.road_close_end.isnot(None),
                or_(
                    and_(WorkOrder.road_close_start <= snap_rcs, snap_rcs < WorkOrder.road_close_end),
                    and_(WorkOrder.road_close_start < snap_rce, snap_rce <= WorkOrder.road_close_end),
                    and_(snap_rcs <= WorkOrder.road_close_start, WorkOrder.road_close_end <= snap_rce),
                ),
            )
            if existing:
                conflict_order = conflict_order.filter(WorkOrder.id != existing.id)
            c = conflict_order.first()
            if c:
                conflicts.append({
                    "type": "team_time_conflict",
                    "message": f"队伍「{snap_team}」在 {c.road_close_start} ~ {c.road_close_end} "
                               f"已被工单 {c.order_no} 占用，与快照窗口 {snap_rcs} ~ {snap_rce} 重叠",
                    "action": "reject",
                })

    if snap_rcs and snap_rce and snap_vehicle:
        vehicle_obj = db.query(Vehicle).filter(Vehicle.plate == snap_vehicle).first()
        if vehicle_obj:
            conflict_order = db.query(WorkOrder).filter(
                WorkOrder.vehicle_id == vehicle_obj.id,
                WorkOrder.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW]),
                WorkOrder.road_close_start.isnot(None),
                WorkOrder.road_close_end.isnot(None),
                or_(
                    and_(WorkOrder.road_close_start <= snap_rcs, snap_rcs < WorkOrder.road_close_end),
                    and_(WorkOrder.road_close_start < snap_rce, snap_rce <= WorkOrder.road_close_end),
                    and_(snap_rcs <= WorkOrder.road_close_start, WorkOrder.road_close_end <= snap_rce),
                ),
            )
            if existing:
                conflict_order = conflict_order.filter(WorkOrder.id != existing.id)
            c = conflict_order.first()
            if c:
                conflicts.append({
                    "type": "vehicle_time_conflict",
                    "message": f"车辆「{snap_vehicle}」在 {c.road_close_start} ~ {c.road_close_end} "
                               f"已被工单 {c.order_no} 占用，与快照窗口 {snap_rcs} ~ {snap_rce} 重叠",
                    "action": "reject",
                })

    road = item.get("road", "")
    if snap_rcs and snap_rce and road:
        road_conflict = db.query(WorkOrder).filter(
            WorkOrder.road == road,
            WorkOrder.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW]),
            WorkOrder.road_close_start.isnot(None),
            WorkOrder.road_close_end.isnot(None),
            or_(
                and_(WorkOrder.road_close_start <= snap_rcs, snap_rcs < WorkOrder.road_close_end),
                and_(WorkOrder.road_close_start < snap_rce, snap_rce <= WorkOrder.road_close_end),
                and_(snap_rcs <= WorkOrder.road_close_start, WorkOrder.road_close_end <= snap_rce),
            ),
        )
        if existing:
            road_conflict = road_conflict.filter(WorkOrder.id != existing.id)
        c = road_conflict.first()
        if c:
            conflicts.append({
                "type": "road_close_conflict",
                "message": f"路段「{road}」在 {c.road_close_start} ~ {c.road_close_end} "
                           f"已被工单 {c.order_no} 占用，与快照窗口 {snap_rcs} ~ {snap_rce} 重叠",
                "action": "reject",
            })

    return conflicts


def _resolve_decision(conflicts: List[dict]) -> str:
    for c in conflicts:
        act = c.get("action")
        if act == "reject":
            return "reject"
    has_dup_overwrite = any(c.get("type") == "duplicate_overwritable" for c in conflicts)
    has_terminal = any(c.get("type") == "terminal_status_new" for c in conflicts)
    if has_terminal:
        return "skip"
    if has_dup_overwrite:
        return "overwrite_or_skip"
    return "ok"


class SnapshotPrecheckIn(BaseModel):
    orders: List[dict]


class SnapshotPrecheckItemOut(BaseModel):
    order_no: str
    road: str
    tree_no: str
    status: str
    risk_level: str
    team: str
    vehicle: str
    decision: str
    conflicts: List[dict]


class SnapshotPrecheckOut(BaseModel):
    snapshot_version: Optional[str] = None
    exported_at: Optional[str] = None
    exported_by: Optional[dict] = None
    total: int
    items: List[SnapshotPrecheckItemOut]


@app.post("/api/snapshot/precheck", response_model=SnapshotPrecheckOut)
def snapshot_precheck(data: SnapshotPrecheckIn,
                      user: User = Depends(_current_user),
                      db: Session = Depends(get_db)):
    _require_role(user, Role.ADMIN)

    items = []
    for item in data.orders:
        conflicts = _detect_conflicts(item, db)
        decision = _resolve_decision(conflicts)
        items.append(SnapshotPrecheckItemOut(
            order_no=item.get("order_no", ""),
            road=item.get("road", ""),
            tree_no=item.get("tree_no", ""),
            status=item.get("status", ""),
            risk_level=item.get("risk_level", ""),
            team=item.get("team", ""),
            vehicle=item.get("vehicle", ""),
            decision=decision,
            conflicts=conflicts,
        ))

    return SnapshotPrecheckOut(
        total=len(items),
        items=items,
    )


class SnapshotImportItemIn(BaseModel):
    order_no: str
    decision: str


class SnapshotImportIn(BaseModel):
    snapshot_version: Optional[str] = None
    exported_at: Optional[str] = None
    exported_by: Optional[dict] = None
    orders: List[dict]
    selected: Optional[List[SnapshotImportItemIn]] = None


class SnapshotImportItemOut(BaseModel):
    order_no: str
    action: str
    success: bool
    reason: str


class SnapshotImportOut(BaseModel):
    total: int
    imported: int
    skipped: int
    rejected: int
    failed: int
    not_selected: int = 0
    batch_id: Optional[int] = None
    batch_no: Optional[str] = None
    items: List[SnapshotImportItemOut]


@app.post("/api/snapshot/import", response_model=SnapshotImportOut)
def snapshot_import(data: SnapshotImportIn,
                    user: User = Depends(_current_user),
                    db: Session = Depends(get_db)):
    _require_role(user, Role.ADMIN)

    selected_map = {}
    has_user_selection = data.selected is not None
    if data.selected:
        for s in data.selected:
            selected_map[s.order_no] = s.decision

    results = []
    batch_items_data = []
    imported_count = 0
    skipped_count = 0
    rejected_count = 0
    failed_count = 0
    not_selected_count = 0

    for item in data.orders:
        order_no = item.get("order_no", "")

        existing = db.query(WorkOrder).filter(WorkOrder.order_no == order_no).first()
        before_snap = None
        before_status = None
        if existing:
            before_snap = _serialize_order_snapshot(existing)
            before_status = existing.status.value if hasattr(existing.status, "value") else str(existing.status)

        batch_item = {
            "order_no": order_no,
            "action": None,
            "success": False,
            "reason": "",
            "order_id": existing.id if existing else None,
            "before_status": before_status,
            "after_status": None,
            "before_snapshot_json": json.dumps(before_snap, ensure_ascii=False) if before_snap else None,
            "after_snapshot_json": None,
        }

        if has_user_selection and order_no not in selected_map:
            not_selected_count += 1
            batch_item["action"] = "not_selected"
            batch_item["reason"] = "未勾选，不执行恢复"
            batch_items_data.append(batch_item)
            results.append(SnapshotImportItemOut(
                order_no=order_no, action="not_selected", success=False,
                reason="未勾选，不执行恢复",
            ))
            continue

        conflicts = _detect_conflicts(item, db)
        auto_decision = _resolve_decision(conflicts)

        user_decision = selected_map.get(order_no) if has_user_selection else None
        if user_decision == "skip":
            skipped_count += 1
            batch_item["action"] = "skip"
            batch_item["success"] = True
            batch_item["reason"] = "用户选择跳过"
            batch_item["after_status"] = before_status
            batch_item["after_snapshot_json"] = batch_item["before_snapshot_json"]
            batch_items_data.append(batch_item)
            results.append(SnapshotImportItemOut(
                order_no=order_no, action="skip", success=True,
                reason="用户选择跳过",
            ))
            continue

        decision = user_decision or auto_decision
        if decision == "overwrite":
            decision = "overwrite_or_skip"
        elif decision == "create":
            decision = "create_or_skip"

        if decision == "reject":
            reject_reasons = "; ".join(c["message"] for c in conflicts if c.get("action") == "reject")
            rejected_count += 1
            batch_item["action"] = "reject"
            batch_item["reason"] = reject_reasons or "存在不可恢复冲突"
            batch_item["after_status"] = before_status
            batch_item["after_snapshot_json"] = batch_item["before_snapshot_json"]
            batch_items_data.append(batch_item)
            results.append(SnapshotImportItemOut(
                order_no=order_no, action="reject", success=False,
                reason=reject_reasons or "存在不可恢复冲突",
            ))
            continue

        if decision == "skip":
            skip_reasons = "; ".join(c["message"] for c in conflicts if c.get("action") == "skip")
            skipped_count += 1
            batch_item["action"] = "skip"
            batch_item["success"] = True
            batch_item["reason"] = skip_reasons or "终态记录自动跳过"
            batch_item["after_status"] = before_status
            batch_item["after_snapshot_json"] = batch_item["before_snapshot_json"]
            batch_items_data.append(batch_item)
            results.append(SnapshotImportItemOut(
                order_no=order_no, action="skip", success=True,
                reason=skip_reasons or "终态记录自动跳过",
            ))
            continue

        try:
            if existing and decision == "overwrite_or_skip":
                snap_status_str = item.get("status", "")
                try:
                    snap_status = OrderStatus(snap_status_str)
                except ValueError:
                    skipped_count += 1
                    batch_item["action"] = "skip"
                    batch_item["reason"] = f"快照状态「{snap_status_str}」无效"
                    batch_item["after_status"] = before_status
                    batch_item["after_snapshot_json"] = batch_item["before_snapshot_json"]
                    batch_items_data.append(batch_item)
                    results.append(SnapshotImportItemOut(
                        order_no=order_no, action="skip", success=False,
                        reason=f"快照状态「{snap_status_str}」无效",
                    ))
                    continue

                snap_rank = STATUS_RANK.get(snap_status, -1)
                existing_rank = STATUS_RANK.get(existing.status, 0)

                if existing.status in (OrderStatus.COMPLETED, OrderStatus.CANCELLED):
                    rejected_count += 1
                    batch_item["action"] = "reject"
                    batch_item["reason"] = f"当前工单状态为「{existing.status.value}」，禁止覆盖"
                    batch_item["after_status"] = before_status
                    batch_item["after_snapshot_json"] = batch_item["before_snapshot_json"]
                    batch_items_data.append(batch_item)
                    results.append(SnapshotImportItemOut(
                        order_no=order_no, action="reject", success=False,
                        reason=f"当前工单状态为「{existing.status.value}」，禁止覆盖",
                    ))
                    continue

                if snap_rank < existing_rank:
                    skipped_count += 1
                    batch_item["action"] = "skip"
                    batch_item["reason"] = f"快照状态「{snap_status_str}」落后于当前「{existing.status.value}」，拒绝倒退覆盖"
                    batch_item["after_status"] = before_status
                    batch_item["after_snapshot_json"] = batch_item["before_snapshot_json"]
                    batch_items_data.append(batch_item)
                    results.append(SnapshotImportItemOut(
                        order_no=order_no, action="skip", success=False,
                        reason=f"快照状态「{snap_status_str}」落后于当前「{existing.status.value}」，拒绝倒退覆盖",
                    ))
                    continue

                snap_team_name = item.get("team", "")
                snap_vehicle_plate = item.get("vehicle", "")
                snap_rcs = _parse_iso(item.get("road_close_start"))
                snap_rce = _parse_iso(item.get("road_close_end"))

                team_obj = db.query(Team).filter(Team.name == snap_team_name).first() if snap_team_name else None
                vehicle_obj = db.query(Vehicle).filter(Vehicle.plate == snap_vehicle_plate).first() if snap_vehicle_plate else None

                if snap_rcs and snap_rce and team_obj:
                    c = db.query(WorkOrder).filter(
                        WorkOrder.team_id == team_obj.id,
                        WorkOrder.id != existing.id,
                        WorkOrder.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW]),
                        WorkOrder.road_close_start.isnot(None),
                        WorkOrder.road_close_end.isnot(None),
                        or_(
                            and_(WorkOrder.road_close_start <= snap_rcs, snap_rcs < WorkOrder.road_close_end),
                            and_(WorkOrder.road_close_start < snap_rce, snap_rce <= WorkOrder.road_close_end),
                            and_(snap_rcs <= WorkOrder.road_close_start, WorkOrder.road_close_end <= snap_rce),
                        ),
                    ).first()
                    if c:
                        rejected_count += 1
                        batch_item["action"] = "reject"
                        batch_item["reason"] = f"队伍「{snap_team_name}」时间冲突，已被工单 {c.order_no} 占用"
                        batch_item["after_status"] = before_status
                        batch_item["after_snapshot_json"] = batch_item["before_snapshot_json"]
                        batch_items_data.append(batch_item)
                        results.append(SnapshotImportItemOut(
                            order_no=order_no, action="reject", success=False,
                            reason=f"队伍「{snap_team_name}」时间冲突，已被工单 {c.order_no} 占用",
                        ))
                        continue

                if snap_rcs and snap_rce and vehicle_obj:
                    c = db.query(WorkOrder).filter(
                        WorkOrder.vehicle_id == vehicle_obj.id,
                        WorkOrder.id != existing.id,
                        WorkOrder.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW]),
                        WorkOrder.road_close_start.isnot(None),
                        WorkOrder.road_close_end.isnot(None),
                        or_(
                            and_(WorkOrder.road_close_start <= snap_rcs, snap_rcs < WorkOrder.road_close_end),
                            and_(WorkOrder.road_close_start < snap_rce, snap_rce <= WorkOrder.road_close_end),
                            and_(snap_rcs <= WorkOrder.road_close_start, WorkOrder.road_close_end <= snap_rce),
                        ),
                    ).first()
                    if c:
                        rejected_count += 1
                        batch_item["action"] = "reject"
                        batch_item["reason"] = f"车辆「{snap_vehicle_plate}」排班冲突，已被工单 {c.order_no} 占用"
                        batch_item["after_status"] = before_status
                        batch_item["after_snapshot_json"] = batch_item["before_snapshot_json"]
                        batch_items_data.append(batch_item)
                        results.append(SnapshotImportItemOut(
                            order_no=order_no, action="reject", success=False,
                            reason=f"车辆「{snap_vehicle_plate}」排班冲突，已被工单 {c.order_no} 占用",
                        ))
                        continue

                road = item.get("road", "")
                if snap_rcs and snap_rce and road:
                    c = db.query(WorkOrder).filter(
                        WorkOrder.road == road,
                        WorkOrder.id != existing.id,
                        WorkOrder.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW]),
                        WorkOrder.road_close_start.isnot(None),
                        WorkOrder.road_close_end.isnot(None),
                        or_(
                            and_(WorkOrder.road_close_start <= snap_rcs, snap_rcs < WorkOrder.road_close_end),
                            and_(WorkOrder.road_close_start < snap_rce, snap_rce <= WorkOrder.road_close_end),
                            and_(snap_rcs <= WorkOrder.road_close_start, WorkOrder.road_close_end <= snap_rce),
                        ),
                    ).first()
                    if c:
                        rejected_count += 1
                        batch_item["action"] = "reject"
                        batch_item["reason"] = f"路段「{road}」封路窗口冲突，已被工单 {c.order_no} 占用"
                        batch_item["after_status"] = before_status
                        batch_item["after_snapshot_json"] = batch_item["before_snapshot_json"]
                        batch_items_data.append(batch_item)
                        results.append(SnapshotImportItemOut(
                            order_no=order_no, action="reject", success=False,
                            reason=f"路段「{road}」封路窗口冲突，已被工单 {c.order_no} 占用",
                        ))
                        continue

                old_status_val = existing.status.value
                existing.road = item.get("road", existing.road)
                existing.tree_no = item.get("tree_no", existing.tree_no)
                try:
                    existing.risk_level = RiskLevel(item.get("risk_level", existing.risk_level.value))
                except ValueError:
                    pass
                existing.suggested_time = item.get("suggested_time", existing.suggested_time) or existing.suggested_time
                existing.need_road_close = item.get("need_road_close", "否") == "是"
                existing.description = item.get("description", existing.description) or existing.description
                existing.status = snap_status
                existing.team_id = team_obj.id if team_obj else None
                existing.vehicle_id = vehicle_obj.id if vehicle_obj else None
                existing.road_close_start = snap_rcs
                existing.road_close_end = snap_rce

                existing.reported_at = _parse_iso(item.get("reported_at"))
                existing.assigned_at = _parse_iso(item.get("assigned_at"))
                existing.started_at = _parse_iso(item.get("started_at"))
                existing.submitted_at = _parse_iso(item.get("submitted_at"))
                existing.reviewed_at = _parse_iso(item.get("reviewed_at"))
                existing.review_note = item.get("review_note") if item.get("review_note") is not None else existing.review_note
                existing.cancelled_at = _parse_iso(item.get("cancelled_at"))
                existing.cancel_reason = item.get("cancel_reason") if item.get("cancel_reason") is not None else existing.cancel_reason

                db.query(StatusHistory).filter(StatusHistory.order_id == existing.id).delete()
                for h_item in item.get("histories", []):
                    h = StatusHistory(
                        order_id=existing.id,
                        from_status=h_item.get("from", ""),
                        to_status=h_item.get("to", ""),
                        operator_name=h_item.get("operator", ""),
                        note=h_item.get("note", ""),
                        created_at=_parse_iso(h_item.get("at")) or datetime.utcnow(),
                    )
                    db.add(h)

                _record_history(db, existing, old_status_val, snap_status, user,
                                note=f"快照恢复覆盖：从「{old_status_val}」恢复为「{snap_status.value}」")

                db.flush()
                after_snap = _serialize_order_snapshot(existing)
                batch_item["action"] = "overwrite"
                batch_item["success"] = True
                batch_item["reason"] = f"覆盖恢复：{old_status_val} → {snap_status.value}"
                batch_item["order_id"] = existing.id
                batch_item["after_status"] = snap_status.value
                batch_item["after_snapshot_json"] = json.dumps(after_snap, ensure_ascii=False)

                _record_trace(db, existing, TraceEventType.SNAPSHOT_OVERWRITE, user,
                              from_status=old_status_val, to_status=snap_status,
                              before_snap=before_snap, after_snap=after_snap,
                              is_batch=True,
                              note=f"快照恢复覆盖：{old_status_val} → {snap_status.value}")

                batch_items_data.append(batch_item)

                imported_count += 1
                results.append(SnapshotImportItemOut(
                    order_no=order_no, action="overwrite", success=True,
                    reason=f"覆盖恢复：{old_status_val} → {snap_status.value}",
                ))
                continue

            if not existing:
                snap_status_str = item.get("status", "待派工")
                try:
                    snap_status = OrderStatus(snap_status_str)
                except ValueError:
                    snap_status = OrderStatus.PENDING_ASSIGN

                if snap_status in (OrderStatus.COMPLETED, OrderStatus.CANCELLED):
                    skipped_count += 1
                    batch_item["action"] = "skip"
                    batch_item["success"] = True
                    batch_item["reason"] = f"终态记录「{snap_status_str}」自动跳过"
                    batch_items_data.append(batch_item)
                    results.append(SnapshotImportItemOut(
                        order_no=order_no, action="skip", success=True,
                        reason=f"终态记录「{snap_status_str}」自动跳过",
                    ))
                    continue

                snap_team_name = item.get("team", "")
                snap_vehicle_plate = item.get("vehicle", "")
                snap_rcs = _parse_iso(item.get("road_close_start"))
                snap_rce = _parse_iso(item.get("road_close_end"))

                team_obj = db.query(Team).filter(Team.name == snap_team_name).first() if snap_team_name else None
                vehicle_obj = db.query(Vehicle).filter(Vehicle.plate == snap_vehicle_plate).first() if snap_vehicle_plate else None

                if snap_rcs and snap_rce and team_obj:
                    c = db.query(WorkOrder).filter(
                        WorkOrder.team_id == team_obj.id,
                        WorkOrder.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW]),
                        WorkOrder.road_close_start.isnot(None),
                        WorkOrder.road_close_end.isnot(None),
                        or_(
                            and_(WorkOrder.road_close_start <= snap_rcs, snap_rcs < WorkOrder.road_close_end),
                            and_(WorkOrder.road_close_start < snap_rce, snap_rce <= WorkOrder.road_close_end),
                            and_(snap_rcs <= WorkOrder.road_close_start, WorkOrder.road_close_end <= snap_rce),
                        ),
                    ).first()
                    if c:
                        rejected_count += 1
                        batch_item["action"] = "reject"
                        batch_item["reason"] = f"队伍「{snap_team_name}」时间冲突，已被工单 {c.order_no} 占用"
                        batch_items_data.append(batch_item)
                        results.append(SnapshotImportItemOut(
                            order_no=order_no, action="reject", success=False,
                            reason=f"队伍「{snap_team_name}」时间冲突，已被工单 {c.order_no} 占用",
                        ))
                        continue

                if snap_rcs and snap_rce and vehicle_obj:
                    c = db.query(WorkOrder).filter(
                        WorkOrder.vehicle_id == vehicle_obj.id,
                        WorkOrder.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW]),
                        WorkOrder.road_close_start.isnot(None),
                        WorkOrder.road_close_end.isnot(None),
                        or_(
                            and_(WorkOrder.road_close_start <= snap_rcs, snap_rcs < WorkOrder.road_close_end),
                            and_(WorkOrder.road_close_start < snap_rce, snap_rce <= WorkOrder.road_close_end),
                            and_(snap_rcs <= WorkOrder.road_close_start, WorkOrder.road_close_end <= snap_rce),
                        ),
                    ).first()
                    if c:
                        rejected_count += 1
                        batch_item["action"] = "reject"
                        batch_item["reason"] = f"车辆「{snap_vehicle_plate}」排班冲突，已被工单 {c.order_no} 占用"
                        batch_items_data.append(batch_item)
                        results.append(SnapshotImportItemOut(
                            order_no=order_no, action="reject", success=False,
                            reason=f"车辆「{snap_vehicle_plate}」排班冲突，已被工单 {c.order_no} 占用",
                        ))
                        continue

                road = item.get("road", "")
                if snap_rcs and snap_rce and road:
                    c = db.query(WorkOrder).filter(
                        WorkOrder.road == road,
                        WorkOrder.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW]),
                        WorkOrder.road_close_start.isnot(None),
                        WorkOrder.road_close_end.isnot(None),
                        or_(
                            and_(WorkOrder.road_close_start <= snap_rcs, snap_rcs < WorkOrder.road_close_end),
                            and_(WorkOrder.road_close_start < snap_rce, snap_rce <= WorkOrder.road_close_end),
                            and_(snap_rcs <= WorkOrder.road_close_start, WorkOrder.road_close_end <= snap_rce),
                        ),
                    ).first()
                    if c:
                        rejected_count += 1
                        batch_item["action"] = "reject"
                        batch_item["reason"] = f"路段「{road}」封路窗口冲突，已被工单 {c.order_no} 占用"
                        batch_items_data.append(batch_item)
                        results.append(SnapshotImportItemOut(
                            order_no=order_no, action="reject", success=False,
                            reason=f"路段「{road}」封路窗口冲突，已被工单 {c.order_no} 占用",
                        ))
                        continue

                new_order = WorkOrder(
                    order_no=order_no,
                    road=item.get("road", ""),
                    tree_no=item.get("tree_no", ""),
                    risk_level=RiskLevel(item.get("risk_level", "低")),
                    suggested_time=item.get("suggested_time", ""),
                    need_road_close=item.get("need_road_close", "否") == "是",
                    description=item.get("description", ""),
                    status=snap_status,
                    team_id=team_obj.id if team_obj else None,
                    vehicle_id=vehicle_obj.id if vehicle_obj else None,
                    road_close_start=snap_rcs,
                    road_close_end=snap_rce,
                    reported_at=_parse_iso(item.get("reported_at")) or datetime.utcnow(),
                    assigned_at=_parse_iso(item.get("assigned_at")),
                    started_at=_parse_iso(item.get("started_at")),
                    submitted_at=_parse_iso(item.get("submitted_at")),
                    reviewed_at=_parse_iso(item.get("reviewed_at")),
                    review_note=item.get("review_note") or None,
                    cancelled_at=_parse_iso(item.get("cancelled_at")),
                    cancel_reason=item.get("cancel_reason") or None,
                )
                db.add(new_order)
                db.flush()

                for h_item in item.get("histories", []):
                    h = StatusHistory(
                        order_id=new_order.id,
                        from_status=h_item.get("from", ""),
                        to_status=h_item.get("to", ""),
                        operator_name=h_item.get("operator", ""),
                        note=h_item.get("note", ""),
                        created_at=_parse_iso(h_item.get("at")) or datetime.utcnow(),
                    )
                    db.add(h)

                _record_history(db, new_order, None, snap_status, user,
                                note=f"快照恢复新建工单")

                after_snap = _serialize_order_snapshot(new_order)

                _record_trace(db, new_order, TraceEventType.SNAPSHOT_CREATE, user,
                              from_status=None, to_status=snap_status,
                              before_snap=None, after_snap=after_snap,
                              is_batch=True,
                              note=f"快照恢复新建工单，状态={snap_status.value}")

                batch_item["action"] = "create"
                batch_item["success"] = True
                batch_item["reason"] = f"新建工单，状态={snap_status.value}"
                batch_item["order_id"] = new_order.id
                batch_item["after_status"] = snap_status.value
                batch_item["after_snapshot_json"] = json.dumps(after_snap, ensure_ascii=False)
                batch_items_data.append(batch_item)

                imported_count += 1
                results.append(SnapshotImportItemOut(
                    order_no=order_no, action="create", success=True,
                    reason=f"新建工单，状态={snap_status.value}",
                ))
                continue

            skipped_count += 1
            batch_item["action"] = "skip"
            batch_item["success"] = True
            batch_item["reason"] = "无匹配操作，自动跳过"
            batch_item["after_status"] = before_status
            batch_item["after_snapshot_json"] = batch_item["before_snapshot_json"]
            batch_items_data.append(batch_item)
            results.append(SnapshotImportItemOut(
                order_no=order_no, action="skip", success=True,
                reason="无匹配操作，自动跳过",
            ))

        except Exception as e:
            failed_count += 1
            batch_item["action"] = "error"
            batch_item["reason"] = str(e)
            batch_item["after_status"] = before_status
            batch_item["after_snapshot_json"] = batch_item["before_snapshot_json"]
            batch_items_data.append(batch_item)
            results.append(SnapshotImportItemOut(
                order_no=order_no, action="error", success=False,
                reason=str(e),
            ))

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"导入提交失败：{str(e)}")

    detail_parts = []
    for r in results:
        if not r.success:
            detail_parts.append(f"{r.order_no}: {r.action} - {r.reason}")
    audit_detail = (
        f"导入完成: 总={len(data.orders)}, 成功={imported_count}, "
        f"跳过={skipped_count}, 拒绝={rejected_count}, 失败={failed_count}"
        f", 未勾选={not_selected_count}"
    )
    if detail_parts:
        audit_detail += "; 失败详情: " + "; ".join(detail_parts[:10])

    batch_no = _generate_batch_no()
    batch = RestoreBatch(
        batch_no=batch_no,
        snapshot_version=data.snapshot_version,
        snapshot_exported_at=data.exported_at,
        snapshot_exported_by=json.dumps(data.exported_by, ensure_ascii=False) if data.exported_by else None,
        operator_id=user.id,
        operator_name=user.name,
        status=RestoreBatchStatus.COMPLETED,
        total_count=len(data.orders),
        imported_count=imported_count,
        skipped_count=skipped_count,
        rejected_count=rejected_count,
        failed_count=failed_count,
        not_selected_count=not_selected_count,
        detail_summary=audit_detail,
        raw_snapshot=json.dumps({"orders": data.orders[:10]}, ensure_ascii=False),
    )
    db.add(batch)
    db.flush()

    for bi in batch_items_data:
        item = RestoreBatchItem(
            batch_id=batch.id,
            order_no=bi["order_no"],
            action=RestoreBatchItemAction(bi["action"]) if bi["action"] else RestoreBatchItemAction.SKIP,
            success=bi["success"],
            reason=bi["reason"],
            order_id=bi["order_id"],
            before_status=bi["before_status"],
            after_status=bi["after_status"],
            before_snapshot_json=bi["before_snapshot_json"],
            after_snapshot_json=bi["after_snapshot_json"],
        )
        db.add(item)

    db.flush()

    db.query(OrderTrace).filter(
        OrderTrace.order_no.in_([bi["order_no"] for bi in batch_items_data]),
        OrderTrace.is_batch_operation == True,
        OrderTrace.batch_id.is_(None),
        OrderTrace.event_type.in_([TraceEventType.SNAPSHOT_CREATE, TraceEventType.SNAPSHOT_OVERWRITE]),
    ).update(
        {OrderTrace.batch_id: batch.id, OrderTrace.batch_no: batch_no},
        synchronize_session=False,
    )

    for bi in batch_items_data:
        action = bi["action"]
        if action in ("create", "overwrite"):
            continue
        order_id = bi.get("order_id")
        order_no = bi["order_no"]
        if not order_id:
            existing = db.query(WorkOrder).filter(WorkOrder.order_no == order_no).first()
            if existing:
                order_id = existing.id
            else:
                continue
        order_obj = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
        if not order_obj:
            continue
        try:
            before_snap = json.loads(bi["before_snapshot_json"]) if bi.get("before_snapshot_json") else None
        except Exception:
            before_snap = None
        try:
            after_snap = json.loads(bi["after_snapshot_json"]) if bi.get("after_snapshot_json") else None
        except Exception:
            after_snap = None

        if action == "skip":
            event_type = TraceEventType.SNAPSHOT_SKIP
        elif action == "reject":
            event_type = TraceEventType.SNAPSHOT_REJECT
        else:
            event_type = TraceEventType.SNAPSHOT_SKIP

        _record_trace(db, order_obj, event_type, user,
                      from_status=bi.get("before_status"),
                      to_status=bi.get("after_status"),
                      before_snap=before_snap,
                      after_snap=after_snap,
                      batch_id=batch.id,
                      batch_no=batch_no,
                      is_batch=True,
                      success=bi.get("success", True),
                      fail_reason=None if bi.get("success", True) else bi.get("reason"),
                      note=bi.get("reason"),
                      idempotency_key=f"batch-{batch.id}-{action}")

    log = AuditLog(
        action="snapshot_import",
        operator_id=user.id,
        operator_name=user.name,
        target_type="snapshot",
        target_id=batch_no,
        detail=audit_detail,
        snapshot_version=data.snapshot_version,
    )
    db.add(log)
    db.commit()
    db.refresh(batch)

    return SnapshotImportOut(
        total=len(data.orders),
        imported=imported_count,
        skipped=skipped_count,
        rejected=rejected_count,
        failed=failed_count,
        not_selected=not_selected_count,
        batch_id=batch.id,
        batch_no=batch_no,
        items=results,
    )


class AuditLogOut(BaseModel):
    id: int
    action: str
    operator_id: int
    operator_name: Optional[str]
    target_type: Optional[str]
    target_id: Optional[str]
    detail: Optional[str]
    snapshot_version: Optional[str]
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


@app.get("/api/audit-logs", response_model=List[AuditLogOut])
def list_audit_logs(
    action: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    _require_role(user, Role.ADMIN)
    q = db.query(AuditLog).order_by(AuditLog.id.desc())
    if action:
        q = q.filter(AuditLog.action == action)
    return q.limit(limit).all()


# ---------- Restore Batch ----------

class RestoreBatchItemOut(BaseModel):
    id: int
    batch_id: int
    order_no: str
    action: str
    success: bool
    reason: Optional[str]
    order_id: Optional[int]
    before_status: Optional[str]
    after_status: Optional[str]
    is_revoked: bool
    revoked_at: Optional[datetime]
    revoke_failed_reason: Optional[str]
    revoke_action: Optional[str]
    revoke_result_reason: Optional[str]
    revoke_changed_fields: Optional[List[str]]

    class Config:
        from_attributes = True


class RestoreBatchOut(BaseModel):
    id: int
    batch_no: str
    snapshot_version: Optional[str]
    snapshot_exported_at: Optional[str]
    snapshot_exported_by: Optional[str]
    operator_id: int
    operator_name: Optional[str]
    status: str
    total_count: int
    imported_count: int
    skipped_count: int
    rejected_count: int
    failed_count: int
    not_selected_count: int
    revoked_count: int
    detail_summary: Optional[str]
    created_at: datetime
    revoked_at: Optional[datetime]
    revoked_by_id: Optional[int]
    revoked_by_name: Optional[str]
    revoke_reason: Optional[str]

    class Config:
        from_attributes = True


class RestoreBatchDetailOut(RestoreBatchOut):
    items: List[RestoreBatchItemOut] = []


class RevokeBatchIn(BaseModel):
    reason: str = Field(..., min_length=1, description="撤销原因")


class RevokeBatchItemOut(BaseModel):
    order_no: str
    action: str
    success: bool
    reason: str
    changed_fields: Optional[List[str]] = None


class RevokeBatchOut(BaseModel):
    batch_id: int
    batch_no: str
    total_revocable: int
    revoked: int
    failed: int
    status: str
    items: List[RevokeBatchItemOut]


@app.get("/api/restore-batches", response_model=List[RestoreBatchOut])
def list_restore_batches(
    limit: int = 50,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    _require_role(user, Role.ADMIN)
    q = db.query(RestoreBatch).order_by(RestoreBatch.id.desc())
    if status:
        try:
            q = q.filter(RestoreBatch.status == RestoreBatchStatus(status))
        except ValueError:
            pass
    return q.limit(limit).all()


@app.get("/api/restore-batches/{batch_id}", response_model=RestoreBatchDetailOut)
def get_restore_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    _require_role(user, Role.ADMIN)
    batch = db.query(RestoreBatch).filter(RestoreBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    items = []
    for item in batch.items:
        changed_fields = None
        if item.revoke_changed_fields:
            try:
                changed_fields = json.loads(item.revoke_changed_fields)
            except Exception:
                changed_fields = None
        items.append(RestoreBatchItemOut(
            id=item.id,
            batch_id=item.batch_id,
            order_no=item.order_no,
            action=item.action.value if hasattr(item.action, "value") else str(item.action),
            success=item.success,
            reason=item.reason,
            order_id=item.order_id,
            before_status=item.before_status,
            after_status=item.after_status,
            is_revoked=item.is_revoked,
            revoked_at=item.revoked_at,
            revoke_failed_reason=item.revoke_failed_reason,
            revoke_action=item.revoke_action,
            revoke_result_reason=item.revoke_result_reason,
            revoke_changed_fields=changed_fields,
        ))

    return RestoreBatchDetailOut(
        id=batch.id,
        batch_no=batch.batch_no,
        snapshot_version=batch.snapshot_version,
        snapshot_exported_at=batch.snapshot_exported_at,
        snapshot_exported_by=batch.snapshot_exported_by,
        operator_id=batch.operator_id,
        operator_name=batch.operator_name,
        status=batch.status.value if hasattr(batch.status, "value") else str(batch.status),
        total_count=batch.total_count,
        imported_count=batch.imported_count,
        skipped_count=batch.skipped_count,
        rejected_count=batch.rejected_count,
        failed_count=batch.failed_count,
        not_selected_count=batch.not_selected_count,
        revoked_count=batch.revoked_count,
        detail_summary=batch.detail_summary,
        created_at=batch.created_at,
        revoked_at=batch.revoked_at,
        revoked_by_id=batch.revoked_by_id,
        revoked_by_name=batch.revoked_by_name,
        revoke_reason=batch.revoke_reason,
        items=items,
    )


def _check_order_modified_since(db: Session, order: WorkOrder, after_snap_json: str) -> tuple[bool, str, List[str]]:
    try:
        after_snap = json.loads(after_snap_json) if after_snap_json else {}
    except Exception:
        return True, "无法解析恢复后快照，无法确认是否被修改", []

    current_snap = _serialize_order_snapshot(order)

    ignore_fields = {"id", "order_no"}
    changed_fields = []
    for key in current_snap:
        if key in ignore_fields:
            continue
        cur_val = current_snap.get(key)
        snap_val = after_snap.get(key)
        if cur_val != snap_val:
            changed_fields.append(key)

    if changed_fields:
        field_display = ", ".join(changed_fields[:5])
        if len(changed_fields) > 5:
            field_display += f" 等{len(changed_fields)}个字段"
        return True, f"工单已被人工修改，变更字段: {field_display}", changed_fields
    return False, "", []


@app.post("/api/restore-batches/{batch_id}/revoke", response_model=RevokeBatchOut)
def revoke_restore_batch(
    batch_id: int,
    data: RevokeBatchIn,
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    _require_role(user, Role.ADMIN)

    batch = db.query(RestoreBatch).filter(RestoreBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    batch_status_val = batch.status.value if hasattr(batch.status, "value") else str(batch.status)
    if batch_status_val == RestoreBatchStatus.REVOKED.value:
        raise HTTPException(status_code=400, detail="该批次已全部撤销，无需重复操作")

    revocable_items = [
        item for item in batch.items
        if item.success and item.action in (RestoreBatchItemAction.CREATE, RestoreBatchItemAction.OVERWRITE)
        and not item.is_revoked
    ]

    if not revocable_items:
        raise HTTPException(status_code=400, detail="该批次没有可撤销的工单（已全部撤销或均为跳过/拒绝）")

    revoke_results = []
    revoked_count = 0
    failed_count = 0

    for item in revocable_items:
        order_no = item.order_no
        action_val = item.action.value if hasattr(item.action, "value") else str(item.action)

        order = db.query(WorkOrder).filter(WorkOrder.id == item.order_id).first() if item.order_id else None

        if action_val == "create":
            if not order:
                item.is_revoked = True
                item.revoked_at = datetime.utcnow()
                item.revoke_action = "revoke_delete"
                item.revoke_result_reason = "新建工单已不存在，视为已撤销"
                item.revoke_failed_reason = None
                item.revoke_changed_fields = None
                revoked_count += 1
                revoke_results.append(RevokeBatchItemOut(
                    order_no=order_no, action="revoke_delete", success=True,
                    reason="新建工单已不存在，视为已撤销",
                    changed_fields=None,
                ))
                continue

            try:
                before_snap_del = json.loads(item.after_snapshot_json) if item.after_snapshot_json else _serialize_order_snapshot(order)
            except Exception:
                before_snap_del = _serialize_order_snapshot(order)

            _record_trace(db, order, TraceEventType.BATCH_REVOKE_DELETE, user,
                          from_status=item.after_status,
                          to_status=None,
                          before_snap=before_snap_del,
                          after_snap=None,
                          batch_id=batch.id,
                          batch_no=batch.batch_no,
                          is_batch=True,
                          success=True,
                          note="删除新建的工单，成功撤销",
                          idempotency_key=f"revoke-{batch.id}-{item.id}")

            db.delete(order)
            item.is_revoked = True
            item.revoked_at = datetime.utcnow()
            item.revoke_action = "revoke_delete"
            item.revoke_result_reason = "删除新建的工单，成功撤销"
            item.revoke_failed_reason = None
            item.revoke_changed_fields = None
            revoked_count += 1
            revoke_results.append(RevokeBatchItemOut(
                order_no=order_no, action="revoke_delete", success=True,
                reason="删除新建的工单，成功撤销",
                changed_fields=None,
            ))

        elif action_val == "overwrite":
            if not order:
                failed_count += 1
                item.revoke_action = "revoke_skip"
                item.revoke_result_reason = None
                item.revoke_failed_reason = "被覆盖的工单已不存在，无法撤销"
                item.revoke_changed_fields = None
                revoke_results.append(RevokeBatchItemOut(
                    order_no=order_no, action="revoke_skip", success=False,
                    reason="被覆盖的工单已不存在，无法撤销",
                    changed_fields=None,
                ))
                continue

            modified, mod_reason, changed_fields_list = _check_order_modified_since(db, order, item.after_snapshot_json or "")
            if modified:
                failed_count += 1
                item.revoke_action = "revoke_skip"
                item.revoke_result_reason = None
                item.revoke_failed_reason = mod_reason
                item.revoke_changed_fields = json.dumps(changed_fields_list, ensure_ascii=False) if changed_fields_list else None

                try:
                    before_snap_skip = json.loads(item.after_snapshot_json) if item.after_snapshot_json else _serialize_order_snapshot(order)
                except Exception:
                    before_snap_skip = _serialize_order_snapshot(order)
                _record_trace(db, order, TraceEventType.BATCH_REVOKE_SKIP, user,
                              from_status=item.after_status,
                              to_status=item.after_status,
                              before_snap=before_snap_skip,
                              after_snap=before_snap_skip,
                              batch_id=batch.id,
                              batch_no=batch.batch_no,
                              is_batch=True,
                              success=False,
                              fail_reason=mod_reason,
                              changed_fields=changed_fields_list,
                              note=mod_reason,
                              idempotency_key=f"revoke-{batch.id}-{item.id}")

                revoke_results.append(RevokeBatchItemOut(
                    order_no=order_no, action="revoke_skip", success=False,
                    reason=mod_reason,
                    changed_fields=changed_fields_list if changed_fields_list else None,
                ))
                continue

            try:
                before_snap = json.loads(item.before_snapshot_json) if item.before_snapshot_json else {}
            except Exception:
                failed_count += 1
                item.revoke_action = "revoke_skip"
                item.revoke_result_reason = None
                item.revoke_failed_reason = "无法解析恢复前快照，无法回退"
                item.revoke_changed_fields = None

                try:
                    before_snap_fail = json.loads(item.after_snapshot_json) if item.after_snapshot_json else _serialize_order_snapshot(order)
                except Exception:
                    before_snap_fail = _serialize_order_snapshot(order)
                _record_trace(db, order, TraceEventType.BATCH_REVOKE_SKIP, user,
                              from_status=item.after_status,
                              to_status=item.after_status,
                              before_snap=before_snap_fail,
                              after_snap=before_snap_fail,
                              batch_id=batch.id,
                              batch_no=batch.batch_no,
                              is_batch=True,
                              success=False,
                              fail_reason="无法解析恢复前快照，无法回退",
                              note="无法解析恢复前快照，无法回退",
                              idempotency_key=f"revoke-{batch.id}-{item.id}")

                revoke_results.append(RevokeBatchItemOut(
                    order_no=order_no, action="revoke_skip", success=False,
                    reason="无法解析恢复前快照，无法回退",
                    changed_fields=None,
                ))
                continue

            restore_changed_fields = []
            if before_snap.get("risk_level"):
                try:
                    new_risk = RiskLevel(before_snap["risk_level"])
                    if order.risk_level != new_risk:
                        restore_changed_fields.append("risk_level")
                    order.risk_level = new_risk
                except ValueError:
                    pass

            if order.road != before_snap.get("road", order.road):
                restore_changed_fields.append("road")
            order.road = before_snap.get("road", order.road)

            if order.tree_no != before_snap.get("tree_no", order.tree_no):
                restore_changed_fields.append("tree_no")
            order.tree_no = before_snap.get("tree_no", order.tree_no)

            suggested_time_val = before_snap.get("suggested_time", "") or ""
            if order.suggested_time != suggested_time_val:
                restore_changed_fields.append("suggested_time")
            order.suggested_time = suggested_time_val

            need_close_val = before_snap.get("need_road_close", order.need_road_close)
            if order.need_road_close != need_close_val:
                restore_changed_fields.append("need_road_close")
            order.need_road_close = need_close_val

            desc_val = before_snap.get("description", "") or ""
            if order.description != desc_val:
                restore_changed_fields.append("description")
            order.description = desc_val

            try:
                status_val = before_snap.get("status")
                if status_val:
                    new_status = OrderStatus(status_val)
                    if order.status != new_status:
                        restore_changed_fields.append("status")
                    order.status = new_status
            except ValueError:
                pass

            team_id_val = before_snap.get("team_id")
            if order.team_id != team_id_val:
                restore_changed_fields.append("team_id")
            order.team_id = team_id_val

            vehicle_id_val = before_snap.get("vehicle_id")
            if order.vehicle_id != vehicle_id_val:
                restore_changed_fields.append("vehicle_id")
            order.vehicle_id = vehicle_id_val

            rcs_val = _parse_iso(before_snap.get("road_close_start"))
            if order.road_close_start != rcs_val:
                restore_changed_fields.append("road_close_start")
            order.road_close_start = rcs_val

            rce_val = _parse_iso(before_snap.get("road_close_end"))
            if order.road_close_end != rce_val:
                restore_changed_fields.append("road_close_end")
            order.road_close_end = rce_val

            reported_at_val = _parse_iso(before_snap.get("reported_at"))
            if order.reported_at != reported_at_val:
                restore_changed_fields.append("reported_at")
            order.reported_at = reported_at_val

            assigned_at_val = _parse_iso(before_snap.get("assigned_at"))
            if order.assigned_at != assigned_at_val:
                restore_changed_fields.append("assigned_at")
            order.assigned_at = assigned_at_val

            started_at_val = _parse_iso(before_snap.get("started_at"))
            if order.started_at != started_at_val:
                restore_changed_fields.append("started_at")
            order.started_at = started_at_val

            submitted_at_val = _parse_iso(before_snap.get("submitted_at"))
            if order.submitted_at != submitted_at_val:
                restore_changed_fields.append("submitted_at")
            order.submitted_at = submitted_at_val

            reviewed_at_val = _parse_iso(before_snap.get("reviewed_at"))
            if order.reviewed_at != reviewed_at_val:
                restore_changed_fields.append("reviewed_at")
            order.reviewed_at = reviewed_at_val

            review_note_val = before_snap.get("review_note") if before_snap.get("review_note") is not None else order.review_note
            if order.review_note != review_note_val:
                restore_changed_fields.append("review_note")
            order.review_note = review_note_val

            cancelled_at_val = _parse_iso(before_snap.get("cancelled_at"))
            if order.cancelled_at != cancelled_at_val:
                restore_changed_fields.append("cancelled_at")
            order.cancelled_at = cancelled_at_val

            cancel_reason_val = before_snap.get("cancel_reason") if before_snap.get("cancel_reason") is not None else order.cancel_reason
            if order.cancel_reason != cancel_reason_val:
                restore_changed_fields.append("cancel_reason")
            order.cancel_reason = cancel_reason_val

            db.query(StatusHistory).filter(StatusHistory.order_id == order.id).delete()

            _record_history(db, order, item.after_status, order.status, user,
                            note=f"批次撤销回退：从「{item.after_status}」回退为「{order.status.value}」")

            try:
                after_snap_restore = json.loads(item.after_snapshot_json) if item.after_snapshot_json else None
            except Exception:
                after_snap_restore = None

            _record_trace(db, order, TraceEventType.BATCH_REVOKE_RESTORE, user,
                          from_status=item.after_status,
                          to_status=order.status,
                          before_snap=after_snap_restore,
                          after_snap=before_snap,
                          batch_id=batch.id,
                          batch_no=batch.batch_no,
                          is_batch=True,
                          success=True,
                          changed_fields=restore_changed_fields,
                          note=f"回退到恢复前状态：{item.after_status} → {order.status.value}",
                          idempotency_key=f"revoke-{batch.id}-{item.id}")

            success_reason = f"回退到恢复前状态：{item.after_status} → {order.status.value}"
            if restore_changed_fields:
                field_display = ", ".join(restore_changed_fields[:5])
                if len(restore_changed_fields) > 5:
                    field_display += f" 等{len(restore_changed_fields)}个字段"
                success_reason += f"（变更字段: {field_display}）"

            item.is_revoked = True
            item.revoked_at = datetime.utcnow()
            item.revoke_action = "revoke_restore"
            item.revoke_result_reason = success_reason
            item.revoke_failed_reason = None
            item.revoke_changed_fields = json.dumps(restore_changed_fields, ensure_ascii=False) if restore_changed_fields else None
            revoked_count += 1
            revoke_results.append(RevokeBatchItemOut(
                order_no=order_no, action="revoke_restore", success=True,
                reason=success_reason,
                changed_fields=restore_changed_fields if restore_changed_fields else None,
            ))

    batch.revoked_count = (batch.revoked_count or 0) + revoked_count

    total_revocable = sum(1 for item in batch.items
                          if item.success and item.action in (RestoreBatchItemAction.CREATE, RestoreBatchItemAction.OVERWRITE))
    total_revoked = sum(1 for item in batch.items if item.is_revoked)

    if total_revoked >= total_revocable:
        batch.status = RestoreBatchStatus.REVOKED
    elif total_revoked > 0:
        batch.status = RestoreBatchStatus.PARTIALLY_REVOKED

    batch.revoked_at = datetime.utcnow()
    batch.revoked_by_id = user.id
    batch.revoked_by_name = user.name
    batch.revoke_reason = data.reason.strip()

    failed_orders = [r for r in revoke_results if not r.success]
    if failed_orders:
        failed_details = "; ".join([f"{r.order_no}: {r.reason}" for r in failed_orders[:3]])
        audit_detail = (f"撤销批次 {batch.batch_no}: 可撤销={len(revocable_items)}, "
                        f"成功={revoked_count}, 失败={failed_count}, 原因={data.reason}, "
                        f"失败详情: {failed_details}")
    else:
        audit_detail = (f"撤销批次 {batch.batch_no}: 可撤销={len(revocable_items)}, "
                        f"成功={revoked_count}, 失败={failed_count}, 原因={data.reason}")

    log = AuditLog(
        action="snapshot_revoke",
        operator_id=user.id,
        operator_name=user.name,
        target_type="restore_batch",
        target_id=batch.batch_no,
        detail=audit_detail,
        snapshot_version=batch.snapshot_version,
    )
    db.add(log)
    db.commit()

    return RevokeBatchOut(
        batch_id=batch.id,
        batch_no=batch.batch_no,
        total_revocable=len(revocable_items),
        revoked=revoked_count,
        failed=failed_count,
        status=batch.status.value if hasattr(batch.status, "value") else str(batch.status),
        items=revoke_results,
    )


# ---------- Order Trace (追溯中心) ----------

EVENT_LABEL_MAP = {
    "report": "巡查员上报",
    "assign": "管理员派工",
    "reassign": "管理员改派",
    "cancel": "撤销工单",
    "start": "开始作业",
    "submit": "提交复核",
    "complete": "复核完成",
    "snapshot_create": "快照恢复-新建",
    "snapshot_overwrite": "快照恢复-覆盖",
    "snapshot_skip": "快照恢复-跳过",
    "snapshot_reject": "快照恢复-拒绝",
    "batch_revoke_delete": "批次撤销-删除",
    "batch_revoke_restore": "批次撤销-回退",
    "batch_revoke_skip": "批次撤销-跳过",
}


class OrderTraceItemOut(BaseModel):
    id: int
    trace_uid: str
    order_id: int
    order_no: str
    event_type: str
    event_label: str
    operator_id: int
    operator_name: Optional[str]
    operator_role: Optional[str]
    created_at: datetime
    from_status: Optional[str]
    to_status: Optional[str]
    changed_fields: Optional[List[str]]
    before_snapshot: Optional[dict]
    after_snapshot: Optional[dict]
    diffs: Optional[List[dict]]
    batch_id: Optional[int]
    batch_no: Optional[str]
    is_batch_operation: bool
    success: bool
    fail_reason: Optional[str]
    note: Optional[str]

    class Config:
        from_attributes = True


class OrderTraceOut(BaseModel):
    order_id: int
    order_no: str
    can_see_batch_detail: bool
    current_status: str
    reporter_id: Optional[int]
    reporter_name: Optional[str]
    events: List[OrderTraceItemOut]
    summary: dict


def _build_field_diffs(before: Optional[dict], after: Optional[dict]) -> List[dict]:
    if not before or not after:
        return []
    diffs = []
    ignore = {"id", "order_no"}
    all_keys = set(before.keys()) | set(after.keys())
    for k in sorted(all_keys):
        if k in ignore:
            continue
        bv = before.get(k)
        av = after.get(k)
        if bv != av:
            diffs.append({"field": k, "before": bv, "after": av})
    return diffs


@app.get("/api/orders/{order_id}/trace", response_model=OrderTraceOut)
def get_order_trace(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="工单不存在")

    is_admin = user.role == Role.ADMIN
    is_reporter = order.reporter_id == user.id

    if not is_admin and not is_reporter:
        raise HTTPException(status_code=403, detail="权限不足：只能查看自己提交的工单追溯")

    can_see_batch_detail = is_admin

    traces = db.query(OrderTrace).filter(OrderTrace.order_id == order_id).order_by(
        OrderTrace.created_at.asc(), OrderTrace.id.asc()
    ).all()

    events = []
    batch_ids_in_trace = set()
    for t in traces:
        try:
            changed_fields = json.loads(t.changed_fields_json) if t.changed_fields_json else None
        except Exception:
            changed_fields = None
        try:
            before_snap = json.loads(t.before_snapshot_json) if t.before_snapshot_json else None
        except Exception:
            before_snap = None
        try:
            after_snap = json.loads(t.after_snapshot_json) if t.after_snapshot_json else None
        except Exception:
            after_snap = None

        event_type_val = t.event_type.value if hasattr(t.event_type, "value") else str(t.event_type)
        diffs = _build_field_diffs(before_snap, after_snap)

        out_before = before_snap if can_see_batch_detail or not t.is_batch_operation else None
        out_after = after_snap if can_see_batch_detail or not t.is_batch_operation else None
        out_diffs = diffs if can_see_batch_detail or not t.is_batch_operation else None
        out_batch_id = t.batch_id if can_see_batch_detail else None
        out_batch_no = t.batch_no if can_see_batch_detail else None
        out_fail = t.fail_reason if can_see_batch_detail or not t.is_batch_operation else None

        events.append(OrderTraceItemOut(
            id=t.id,
            trace_uid=t.trace_uid,
            order_id=t.order_id,
            order_no=t.order_no,
            event_type=event_type_val,
            event_label=EVENT_LABEL_MAP.get(event_type_val, event_type_val),
            operator_id=t.operator_id,
            operator_name=t.operator_name,
            operator_role=t.operator_role,
            created_at=t.created_at,
            from_status=t.from_status,
            to_status=t.to_status,
            changed_fields=changed_fields if (can_see_batch_detail or not t.is_batch_operation) else None,
            before_snapshot=out_before,
            after_snapshot=out_after,
            diffs=out_diffs,
            batch_id=out_batch_id,
            batch_no=out_batch_no,
            is_batch_operation=t.is_batch_operation,
            success=t.success,
            fail_reason=out_fail,
            note=t.note,
        ))
        if t.batch_id:
            batch_ids_in_trace.add(t.batch_id)

    event_counts = {}
    for e in events:
        event_counts[e.event_type] = event_counts.get(e.event_type, 0) + 1

    summary = {
        "total_events": len(events),
        "event_counts": event_counts,
        "batch_count": len(batch_ids_in_trace),
        "success_count": sum(1 for e in events if e.success),
        "failed_count": sum(1 for e in events if not e.success),
    }

    return OrderTraceOut(
        order_id=order.id,
        order_no=order.order_no,
        can_see_batch_detail=can_see_batch_detail,
        current_status=order.status.value if hasattr(order.status, "value") else str(order.status),
        reporter_id=order.reporter_id,
        reporter_name=order.reporter.name if order.reporter else None,
        events=events,
        summary=summary,
    )


def _serialize_trace_for_export(order_id: int, db: Session, user: User) -> dict:
    order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="工单不存在")

    is_admin = user.role == Role.ADMIN
    is_reporter = order.reporter_id == user.id
    if not is_admin and not is_reporter:
        raise HTTPException(status_code=403, detail="权限不足：只能导出自己提交的工单追溯")

    can_see_batch_detail = is_admin

    traces = db.query(OrderTrace).filter(OrderTrace.order_id == order_id).order_by(
        OrderTrace.created_at.asc(), OrderTrace.id.asc()
    ).all()

    rows = []
    for t in traces:
        try:
            changed_fields = json.loads(t.changed_fields_json) if t.changed_fields_json else []
        except Exception:
            changed_fields = []
        try:
            before_snap = json.loads(t.before_snapshot_json) if t.before_snapshot_json else None
        except Exception:
            before_snap = None
        try:
            after_snap = json.loads(t.after_snapshot_json) if t.after_snapshot_json else None
        except Exception:
            after_snap = None

        event_type_val = t.event_type.value if hasattr(t.event_type, "value") else str(t.event_type)
        diffs = _build_field_diffs(before_snap, after_snap)

        expose_batch = can_see_batch_detail or not t.is_batch_operation
        rows.append({
            "trace_id": t.id,
            "trace_uid": t.trace_uid,
            "event_time": t.created_at.isoformat() if t.created_at else "",
            "event_type": event_type_val,
            "event_label": EVENT_LABEL_MAP.get(event_type_val, event_type_val),
            "operator_id": t.operator_id,
            "operator_name": t.operator_name or "",
            "operator_role": t.operator_role or "",
            "from_status": t.from_status or "",
            "to_status": t.to_status or "",
            "changed_fields": ", ".join(changed_fields) if (expose_batch and changed_fields) else "",
            "is_batch_operation": "是" if t.is_batch_operation else "否",
            "batch_id": t.batch_id if expose_batch else "",
            "batch_no": t.batch_no if expose_batch else "",
            "success": "成功" if t.success else "失败",
            "fail_reason": t.fail_reason if expose_batch else "",
            "note": t.note or "",
            "diffs_json": json.dumps(diffs, ensure_ascii=False) if (expose_batch and diffs) else "",
            "before_snapshot_json": json.dumps(before_snap, ensure_ascii=False) if (expose_batch and before_snap) else "",
            "after_snapshot_json": json.dumps(after_snap, ensure_ascii=False) if (expose_batch and after_snap) else "",
        })

    return {
        "order": {
            "id": order.id,
            "order_no": order.order_no,
            "road": order.road,
            "tree_no": order.tree_no,
            "risk_level": order.risk_level.value if hasattr(order.risk_level, "value") else str(order.risk_level),
            "current_status": order.status.value if hasattr(order.status, "value") else str(order.status),
            "reporter_id": order.reporter_id,
            "reporter_name": order.reporter.name if order.reporter else "",
            "reported_at": order.reported_at.isoformat() if order.reported_at else "",
        },
        "exported_at": datetime.utcnow().isoformat(),
        "exported_by": {"id": user.id, "name": user.name, "role": user.role.value},
        "can_see_batch_detail": can_see_batch_detail,
        "total_events": len(rows),
        "events": rows,
    }


@app.get("/api/orders/{order_id}/trace/export/json")
def export_order_trace_json(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    payload = _serialize_trace_for_export(order_id, db, user)
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    resp = StreamingResponse(
        iter([content]),
        media_type="application/json; charset=utf-8",
    )
    fname = f"order_trace_{payload['order']['order_no']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


@app.get("/api/orders/{order_id}/trace/export/csv")
def export_order_trace_csv(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    payload = _serialize_trace_for_export(order_id, db, user)
    rows = payload["events"]
    if not rows:
        headers = []
    else:
        headers = list(rows[0].keys())

    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)

    header_lines = []
    for k, v in payload["order"].items():
        header_lines.append([f"order_{k}", str(v) if v is not None else ""])
    header_lines.append(["exported_at", payload["exported_at"]])
    header_lines.append(["exported_by_name", payload["exported_by"]["name"]])
    header_lines.append(["exported_by_role", payload["exported_by"]["role"]])
    header_lines.append(["total_events", str(payload["total_events"])])
    header_lines.append(["can_see_batch_detail", "是" if payload["can_see_batch_detail"] else "否"])
    header_lines.append([])

    header_buf = io.StringIO(newline="")
    hw = csv.writer(header_buf)
    for hl in header_lines:
        hw.writerow(hl)

    content = (header_buf.getvalue() + buf.getvalue()).encode("utf-8-sig")
    resp = StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
    )
    fname = f"order_trace_{payload['order']['order_no']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


# ========== Verification Console (校验台) ==========

VFY_ACTION_CREATE = "create"
VFY_ACTION_VIEW = "view"
VFY_ACTION_RERUN = "rerun"
VFY_ACTION_EXPORT = "export"


def _check_vfy_permission(user: User, task: Optional[VerificationTask], action: str):
    if user.role == Role.ADMIN:
        return
    if action == VFY_ACTION_EXPORT:
        raise HTTPException(status_code=403, detail="权限不足：巡查员不可导出审计包，仅管理员可导出")
    if action == VFY_ACTION_CREATE:
        raise HTTPException(status_code=403, detail="权限不足：只有管理员可按批次校验")
    if task is not None and task.operator_id != user.id:
        raise HTTPException(status_code=403, detail="权限不足：只能操作自己创建的校验任务")


def _apply_result_desensitization(result: dict, viewer: User) -> dict:
    is_admin = viewer.role == Role.ADMIN
    can_see = is_admin
    result["can_see_batch_detail"] = can_see
    if can_see:
        return result
    events = result.get("events") or []
    for e in events:
        if e.get("is_batch_operation"):
            e["changed_fields"] = None
            e["before_snapshot"] = None
            e["after_snapshot"] = None
            e["diffs"] = None
            e["batch_id"] = None
            e["batch_no"] = None
            e["fail_reason"] = None
    all_events = result.get("all_events")
    if all_events:
        for e in all_events:
            if e.get("is_batch_operation"):
                e["changed_fields"] = None
                e["before_snapshot"] = None
                e["after_snapshot"] = None
                e["diffs"] = None
                e["batch_id"] = None
                e["batch_no"] = None
                e["fail_reason"] = None
    return result


def _parse_task_result(task: VerificationTask) -> Optional[dict]:
    if not task.result_json:
        return None
    try:
        return json.loads(task.result_json)
    except Exception:
        return None


def _update_task_counts(task: VerificationTask, result: dict):
    summary = result.get("summary", {})
    task.result_summary = (
        f"事件数:{summary.get('total_events', 0)}, "
        f"冲突数:{summary.get('conflict_count', 0)}, "
        f"批次数:{summary.get('batch_count', 0)}"
    )
    task.result_json = json.dumps(result, ensure_ascii=False)
    task.conflict_count = summary.get("conflict_count", 0)
    task.event_count = summary.get("total_events", 0)
    task.failed_event_count = summary.get("failed_count", 0)
    task.batch_count = summary.get("batch_count", 0)
    task.status = VerificationStatus.COMPLETED
    task.completed_at = datetime.utcnow()
    task.error_message = None


def _get_config_value(db: Session, key: str, default=None):
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    if not cfg:
        return default
    val = cfg.config_value
    ctype = cfg.config_type
    if ctype == "int":
        try:
            return int(val)
        except (ValueError, TypeError):
            return default
    if ctype == "bool":
        return str(val).lower() in ("true", "1", "yes", "on")
    if ctype == "float":
        try:
            return float(val)
        except (ValueError, TypeError):
            return default
    return val


def _set_config_value(db: Session, key: str, value, ctype: str = "string",
                      description: str = "", updated_by: str = ""):
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    if cfg:
        cfg.config_value = str(value)
        cfg.config_type = ctype
        if description:
            cfg.description = description
        cfg.updated_by = updated_by or cfg.updated_by
    else:
        db.add(SystemConfig(
            config_key=key,
            config_value=str(value),
            config_type=ctype,
            description=description,
            updated_by=updated_by,
        ))


def _generate_task_no() -> str:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    rand = uuid.uuid4().hex[:6].upper()
    return f"VFY-{ts}-{rand}"


def _detect_conflicts_in_events(events: List[dict]) -> List[dict]:
    conflicts = []
    seen_statuses = []
    batch_ops = []

    for i, e in enumerate(events):
        et = e.get("event_type", "")
        success = e.get("success", True)
        is_batch = e.get("is_batch_operation", False)

        if is_batch:
            batch_ops.append(e)

        if not success and et not in ("snapshot_skip", "batch_revoke_skip"):
            conflicts.append({
                "index": i,
                "type": "failed_event",
                "severity": "high",
                "event_type": et,
                "event_label": e.get("event_label", et),
                "message": f"操作失败：{e.get('fail_reason', '未知原因')}",
                "created_at": e.get("created_at"),
            })

        if is_batch and et == "batch_revoke_skip":
            conflicts.append({
                "index": i,
                "type": "revoke_skip",
                "severity": "medium",
                "event_type": et,
                "event_label": e.get("event_label", et),
                "message": f"批次撤销被跳过：{e.get('fail_reason', e.get('note', ''))}",
                "created_at": e.get("created_at"),
            })

        if is_batch and et in ("snapshot_overwrite", "snapshot_create"):
            prev_non_batch = None
            for j in range(i - 1, -1, -1):
                if not events[j].get("is_batch_operation"):
                    prev_non_batch = events[j]
                    break
            if prev_non_batch and prev_non_batch.get("success"):
                conflicts.append({
                    "index": i,
                    "type": "snapshot_override",
                    "severity": "info",
                    "event_type": et,
                    "event_label": e.get("event_label", et),
                    "message": f"快照覆盖/新建了人工操作记录（操作人：{prev_non_batch.get('operator_name', '未知')}）",
                    "created_at": e.get("created_at"),
                })

    if len(batch_ops) >= 4:
        conflicts.append({
            "type": "multiple_batch_ops",
            "severity": "warning",
            "message": f"该工单涉及 {len(batch_ops)} 次批量操作，建议核查数据一致性",
            "batch_count": len(batch_ops),
        })

    status_flow = [e.get("to_status") for e in events if e.get("to_status") and e.get("success")]
    for i in range(1, len(status_flow)):
        if status_flow[i] and status_flow[i - 1]:
            if status_flow[i - 1] in ("已完成", "已撤销") and status_flow[i] not in ("已完成", "已撤销"):
                conflicts.append({
                    "type": "status_regression",
                    "severity": "high",
                    "message": f"状态倒退：从「{status_flow[i - 1]}」回到「{status_flow[i]}」",
                })

    return conflicts


def _build_verification_result(order: WorkOrder, traces: List[OrderTrace],
                                can_see_batch_detail: bool) -> dict:
    events = []
    for t in traces:
        try:
            changed_fields = json.loads(t.changed_fields_json) if t.changed_fields_json else None
        except Exception:
            changed_fields = None
        try:
            before_snap = json.loads(t.before_snapshot_json) if t.before_snapshot_json else None
        except Exception:
            before_snap = None
        try:
            after_snap = json.loads(t.after_snapshot_json) if t.after_snapshot_json else None
        except Exception:
            after_snap = None

        event_type_val = t.event_type.value if hasattr(t.event_type, "value") else str(t.event_type)
        diffs = _build_field_diffs(before_snap, after_snap)

        expose_batch = can_see_batch_detail or not t.is_batch_operation

        events.append({
            "id": t.id,
            "trace_uid": t.trace_uid,
            "event_type": event_type_val,
            "event_label": EVENT_LABEL_MAP.get(event_type_val, event_type_val),
            "operator_id": t.operator_id,
            "operator_name": t.operator_name,
            "operator_role": t.operator_role,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "from_status": t.from_status,
            "to_status": t.to_status,
            "changed_fields": changed_fields if expose_batch else None,
            "before_snapshot": before_snap if expose_batch else None,
            "after_snapshot": after_snap if expose_batch else None,
            "diffs": diffs if expose_batch else None,
            "batch_id": t.batch_id if can_see_batch_detail else None,
            "batch_no": t.batch_no if can_see_batch_detail else None,
            "is_batch_operation": t.is_batch_operation,
            "success": t.success,
            "fail_reason": t.fail_reason if expose_batch else None,
            "note": t.note,
        })

    conflicts = _detect_conflicts_in_events(events)

    event_counts = {}
    for e in events:
        et = e["event_type"]
        event_counts[et] = event_counts.get(et, 0) + 1

    batch_ids = {e["batch_id"] for e in events if e["batch_id"]}

    summary = {
        "total_events": len(events),
        "event_counts": event_counts,
        "batch_count": len(batch_ids),
        "success_count": sum(1 for e in events if e["success"]),
        "failed_count": sum(1 for e in events if not e["success"]),
        "conflict_count": len(conflicts),
        "high_conflict_count": sum(1 for c in conflicts if c.get("severity") == "high"),
        "medium_conflict_count": sum(1 for c in conflicts if c.get("severity") == "medium"),
    }

    order_info = {
        "id": order.id,
        "order_no": order.order_no,
        "road": order.road,
        "tree_no": order.tree_no,
        "risk_level": order.risk_level.value if hasattr(order.risk_level, "value") else str(order.risk_level),
        "current_status": order.status.value if hasattr(order.status, "value") else str(order.status),
        "reporter_id": order.reporter_id,
        "reporter_name": order.reporter.name if order.reporter else None,
        "reported_at": order.reported_at.isoformat() if order.reported_at else None,
    }

    return {
        "order": order_info,
        "events": events,
        "conflicts": conflicts,
        "summary": summary,
        "can_see_batch_detail": can_see_batch_detail,
        "verification_time": datetime.utcnow().isoformat(),
    }


def _run_verification_for_order(db: Session, order: WorkOrder, user: User) -> dict:
    is_admin = user.role == Role.ADMIN
    is_reporter = order.reporter_id == user.id

    if not is_admin and not is_reporter:
        raise HTTPException(status_code=403, detail="权限不足：只能校验自己提交的工单")

    traces = db.query(OrderTrace).filter(OrderTrace.order_id == order.id).order_by(
        OrderTrace.created_at.asc(), OrderTrace.id.asc()
    ).all()

    return _build_verification_result(order, traces, True)


def _run_verification_for_batch(db: Session, batch_id: int, user: User) -> dict:
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="权限不足：只有管理员可按批次校验")

    batch = db.query(RestoreBatch).filter(RestoreBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    items = db.query(RestoreBatchItem).filter(RestoreBatchItem.batch_id == batch_id).all()
    order_nos = [item.order_no for item in items]

    traces = db.query(OrderTrace).filter(OrderTrace.batch_id == batch_id).order_by(
        OrderTrace.order_no.asc(), OrderTrace.created_at.asc(), OrderTrace.id.asc()
    ).all()

    order_traces_map = {}
    for t in traces:
        if t.order_no not in order_traces_map:
            order_traces_map[t.order_no] = []
        order_traces_map[t.order_no].append(t)

    orders = db.query(WorkOrder).filter(WorkOrder.order_no.in_(order_nos)).all()
    order_map = {o.order_no: o for o in orders}

    per_order_results = []
    all_events = []
    all_conflicts = []
    total_event_count = 0
    total_conflict_count = 0

    for item in items:
        o = order_map.get(item.order_no)
        item_traces = order_traces_map.get(item.order_no, [])

        if o:
            order_result = _build_verification_result(o, item_traces, True)
            per_order_results.append(order_result)
            all_events.extend(order_result["events"])
            all_conflicts.extend([
                {**c, "order_no": o.order_no}
                for c in order_result["conflicts"]
            ])
            total_event_count += order_result["summary"]["total_events"]
            total_conflict_count += order_result["summary"]["conflict_count"]
        else:
            per_order_results.append({
                "order": {
                    "order_no": item.order_no,
                    "current_status": "已删除/不存在",
                },
                "events": [],
                "conflicts": [{"type": "order_missing", "severity": "high",
                               "message": "该工单在系统中已不存在"}],
                "summary": {"total_events": 0, "conflict_count": 1},
                "can_see_batch_detail": True,
            })
            all_conflicts.append({
                "type": "order_missing",
                "severity": "high",
                "message": f"工单 {item.order_no} 在系统中已不存在",
                "order_no": item.order_no,
            })
            total_conflict_count += 1

    summary = {
        "total_orders": len(per_order_results),
        "total_events": total_event_count,
        "total_conflicts": total_conflict_count,
        "batch_id": batch.id,
        "batch_no": batch.batch_no,
        "batch_status": batch.status.value if hasattr(batch.status, "value") else str(batch.status),
    }

    return {
        "batch": {
            "id": batch.id,
            "batch_no": batch.batch_no,
            "status": batch.status.value if hasattr(batch.status, "value") else str(batch.status),
            "operator_name": batch.operator_name,
            "created_at": batch.created_at.isoformat() if batch.created_at else None,
            "total_count": batch.total_count,
            "imported_count": batch.imported_count,
            "skipped_count": batch.skipped_count,
            "rejected_count": batch.rejected_count,
            "failed_count": batch.failed_count,
            "revoked_count": batch.revoked_count,
        },
        "orders": per_order_results,
        "all_events": all_events,
        "all_conflicts": all_conflicts,
        "summary": summary,
        "verification_time": datetime.utcnow().isoformat(),
    }


def _cleanup_expired_verifications(db: Session) -> int:
    if not _get_config_value(db, "verification_auto_clean_enabled", True):
        return 0

    retention_days = _get_config_value(db, "verification_retention_days", 30)
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    expired = db.query(VerificationTask).filter(VerificationTask.created_at < cutoff).all()
    count = len(expired)
    for t in expired:
        db.delete(t)
    db.commit()
    return count


def _build_audit_package(task: VerificationTask, result: dict, user: User) -> dict:
    return {
        "audit_package_version": "1.0.0",
        "generated_at": datetime.utcnow().isoformat(),
        "generated_by": {"id": user.id, "name": user.name, "role": user.role.value},
        "task": {
            "id": task.id,
            "task_no": task.task_no,
            "task_type": task.task_type,
            "target_order_no": task.target_order_no,
            "target_order_id": task.target_order_id,
            "target_batch_id": task.target_batch_id,
            "target_batch_no": task.target_batch_no,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "rerun_count": task.rerun_count,
            "export_count": task.export_count,
        },
        "verification_result": result,
        "summary": result.get("summary", {}),
    }


# ---- Pydantic schemas for verification ----

class VerificationTaskOut(BaseModel):
    id: int
    task_no: str
    task_type: str
    target_order_no: Optional[str]
    target_order_id: Optional[int]
    target_batch_id: Optional[int]
    target_batch_no: Optional[str]
    status: str
    operator_id: int
    operator_name: Optional[str]
    result_summary: Optional[str]
    conflict_count: int
    event_count: int
    failed_event_count: int
    batch_count: int
    error_message: Optional[str]
    created_at: Optional[datetime]
    completed_at: Optional[datetime]
    rerun_count: int
    last_rerun_at: Optional[datetime]
    export_count: int
    last_export_at: Optional[datetime]

    class Config:
        from_attributes = True


class VerificationTaskDetailOut(VerificationTaskOut):
    result: Optional[dict] = None


class CreateVerificationIn(BaseModel):
    task_type: str = Field(default="order_trace", description="校验类型: order_trace / batch_trace")
    order_id: Optional[int] = Field(default=None, description="工单ID")
    order_no: Optional[str] = Field(default=None, description="工单号")
    batch_id: Optional[int] = Field(default=None, description="批次ID")
    batch_no: Optional[str] = Field(default=None, description="批次号")


class RerunVerificationIn(BaseModel):
    reason: Optional[str] = Field(default="手动重跑", description="重跑原因")


class SystemConfigOut(BaseModel):
    id: int
    config_key: str
    config_value: str
    config_type: str
    description: Optional[str]
    updated_at: Optional[datetime]
    updated_by: Optional[str]

    class Config:
        from_attributes = True


class UpdateSystemConfigIn(BaseModel):
    config_value: str


def _task_to_out(task: VerificationTask, result_dict: Optional[dict] = None) -> VerificationTaskDetailOut:
    return VerificationTaskDetailOut(
        id=task.id,
        task_no=task.task_no,
        task_type=task.task_type,
        target_order_no=task.target_order_no,
        target_order_id=task.target_order_id,
        target_batch_id=task.target_batch_id,
        target_batch_no=task.target_batch_no,
        status=task.status.value if hasattr(task.status, "value") else str(task.status),
        operator_id=task.operator_id,
        operator_name=task.operator_name,
        result_summary=task.result_summary,
        conflict_count=task.conflict_count,
        event_count=task.event_count,
        failed_event_count=task.failed_event_count,
        batch_count=task.batch_count,
        error_message=task.error_message,
        created_at=task.created_at,
        completed_at=task.completed_at,
        rerun_count=task.rerun_count,
        last_rerun_at=task.last_rerun_at,
        export_count=task.export_count,
        last_export_at=task.last_export_at,
        result=result_dict,
    )


# ---- Verification APIs ----

@app.post("/api/verification/tasks", response_model=VerificationTaskDetailOut)
def create_verification_task(
    data: CreateVerificationIn,
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    target_order = None
    target_batch = None

    if data.task_type == "order_trace":
        if data.order_id:
            target_order = db.query(WorkOrder).filter(WorkOrder.id == data.order_id).first()
        elif data.order_no:
            target_order = db.query(WorkOrder).filter(WorkOrder.order_no == data.order_no).first()

        if not target_order:
            raise HTTPException(status_code=404, detail="工单不存在")

        if user.role != Role.ADMIN and target_order.reporter_id != user.id:
            raise HTTPException(status_code=403, detail="权限不足：只能校验自己提交的工单")

    elif data.task_type == "batch_trace":
        _check_vfy_permission(user, None, VFY_ACTION_CREATE)

        if data.batch_id:
            target_batch = db.query(RestoreBatch).filter(RestoreBatch.id == data.batch_id).first()
        elif data.batch_no:
            target_batch = db.query(RestoreBatch).filter(RestoreBatch.batch_no == data.batch_no).first()

        if not target_batch:
            raise HTTPException(status_code=404, detail="批次不存在")
    else:
        raise HTTPException(status_code=400, detail=f"不支持的校验类型: {data.task_type}")

    task = VerificationTask(
        task_no=_generate_task_no(),
        task_type=data.task_type,
        target_order_no=target_order.order_no if target_order else None,
        target_order_id=target_order.id if target_order else None,
        target_batch_id=target_batch.id if target_batch else None,
        target_batch_no=target_batch.batch_no if target_batch else None,
        status=VerificationStatus.RUNNING,
        operator_id=user.id,
        operator_name=user.name,
    )
    db.add(task)
    db.flush()

    try:
        if data.task_type == "order_trace" and target_order:
            result = _run_verification_for_order(db, target_order, user)
        elif data.task_type == "batch_trace" and target_batch:
            result = _run_verification_for_batch(db, target_batch, user)
        else:
            result = {}

        _update_task_counts(task, result)

        log = AuditLog(
            action="verification_create",
            operator_id=user.id,
            operator_name=user.name,
            target_type="verification_task",
            target_id=task.task_no,
            detail=f"创建校验任务 {task.task_no}，类型={data.task_type}，"
                   f"目标={target_order.order_no if target_order else target_batch.batch_no if target_batch else '未知'}",
        )
        db.add(log)
        db.commit()
        db.refresh(task)

    except Exception as e:
        task.status = VerificationStatus.FAILED
        task.error_message = str(e)
        task.completed_at = datetime.utcnow()
        db.commit()
        db.refresh(task)

    result_dict = _parse_task_result(task)
    if result_dict:
        result_dict = _apply_result_desensitization(result_dict, user)
    return _task_to_out(task, result_dict)


@app.get("/api/verification/tasks", response_model=List[VerificationTaskOut])
def list_verification_tasks(
    status: Optional[str] = None,
    task_type: Optional[str] = None,
    order_no: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    q = db.query(VerificationTask).order_by(VerificationTask.id.desc())

    if user.role != Role.ADMIN:
        q = q.filter(VerificationTask.operator_id == user.id)

    if status:
        try:
            q = q.filter(VerificationTask.status == VerificationStatus(status))
        except ValueError:
            pass
    if task_type:
        q = q.filter(VerificationTask.task_type == task_type)
    if order_no:
        q = q.filter(VerificationTask.target_order_no.contains(order_no))

    tasks = q.limit(limit).all()
    return [
        VerificationTaskOut(
            id=t.id,
            task_no=t.task_no,
            task_type=t.task_type,
            target_order_no=t.target_order_no,
            target_order_id=t.target_order_id,
            target_batch_id=t.target_batch_id,
            target_batch_no=t.target_batch_no,
            status=t.status.value if hasattr(t.status, "value") else str(t.status),
            operator_id=t.operator_id,
            operator_name=t.operator_name,
            result_summary=t.result_summary,
            conflict_count=t.conflict_count,
            event_count=t.event_count,
            failed_event_count=t.failed_event_count,
            batch_count=t.batch_count,
            error_message=t.error_message,
            created_at=t.created_at,
            completed_at=t.completed_at,
            rerun_count=t.rerun_count,
            last_rerun_at=t.last_rerun_at,
            export_count=t.export_count,
            last_export_at=t.last_export_at,
        )
        for t in tasks
    ]


@app.get("/api/verification/tasks/{task_id}", response_model=VerificationTaskDetailOut)
def get_verification_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    task = db.query(VerificationTask).filter(VerificationTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="校验任务不存在")

    _check_vfy_permission(user, task, VFY_ACTION_VIEW)

    result_dict = _parse_task_result(task)
    if result_dict:
        result_dict = _apply_result_desensitization(result_dict, user)
    return _task_to_out(task, result_dict)


@app.post("/api/verification/tasks/{task_id}/rerun", response_model=VerificationTaskDetailOut)
def rerun_verification_task(
    task_id: int,
    data: RerunVerificationIn = RerunVerificationIn(),
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    task = db.query(VerificationTask).filter(VerificationTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="校验任务不存在")

    _check_vfy_permission(user, task, VFY_ACTION_RERUN)

    task.status = VerificationStatus.RUNNING
    task.rerun_count = (task.rerun_count or 0) + 1
    task.last_rerun_at = datetime.utcnow()
    task.last_rerun_by_id = user.id
    task.last_rerun_by_name = user.name
    db.flush()

    try:
        if task.task_type == "order_trace" and task.target_order_id:
            order = db.query(WorkOrder).filter(WorkOrder.id == task.target_order_id).first()
            if not order:
                order = db.query(WorkOrder).filter(WorkOrder.order_no == task.target_order_no).first()
            if not order:
                raise HTTPException(status_code=404, detail="关联工单已不存在")
            result = _run_verification_for_order(db, order, user)
        elif task.task_type == "batch_trace" and task.target_batch_id:
            result = _run_verification_for_batch(db, task.target_batch_id, user)
        else:
            result = {}

        _update_task_counts(task, result)

        log = AuditLog(
            action="verification_rerun",
            operator_id=user.id,
            operator_name=user.name,
            target_type="verification_task",
            target_id=task.task_no,
            detail=f"重跑校验任务 {task.task_no}，原因={data.reason}，"
                   f"第{task.rerun_count}次重跑",
        )
        db.add(log)
        db.commit()
        db.refresh(task)

    except Exception as e:
        task.status = VerificationStatus.FAILED
        task.error_message = str(e)
        db.commit()
        db.refresh(task)

    result_dict = _parse_task_result(task)
    if result_dict:
        result_dict = _apply_result_desensitization(result_dict, user)
    return _task_to_out(task, result_dict)


@app.get("/api/verification/tasks/{task_id}/export/json")
def export_verification_json(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    if not _get_config_value(db, "verification_export_enabled", True):
        raise HTTPException(status_code=403, detail="校验导出功能已被管理员禁用")

    task = db.query(VerificationTask).filter(VerificationTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="校验任务不存在")

    _check_vfy_permission(user, task, VFY_ACTION_EXPORT)

    if not task.result_json:
        raise HTTPException(status_code=400, detail="校验任务无结果数据")

    try:
        result = json.loads(task.result_json)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"结果解析失败: {e}")

    result = _apply_result_desensitization(result, user)

    task.export_count = (task.export_count or 0) + 1
    task.last_export_at = datetime.utcnow()
    task.last_export_by_id = user.id
    task.last_export_by_name = user.name

    audit_pkg = _build_audit_package(task, result, user)
    content = json.dumps(audit_pkg, ensure_ascii=False, indent=2).encode("utf-8")

    log = AuditLog(
        action="verification_export",
        operator_id=user.id,
        operator_name=user.name,
        target_type="verification_task",
        target_id=task.task_no,
        detail=f"导出校验任务 {task.task_no} 的审计包（JSON），第{task.export_count}次导出",
    )
    db.add(log)
    db.commit()

    resp = StreamingResponse(
        iter([content]),
        media_type="application/json; charset=utf-8",
    )
    fname = f"verification_audit_{task.task_no}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


@app.get("/api/verification/tasks/{task_id}/export/csv")
def export_verification_csv(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    if not _get_config_value(db, "verification_export_enabled", True):
        raise HTTPException(status_code=403, detail="校验导出功能已被管理员禁用")

    task = db.query(VerificationTask).filter(VerificationTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="校验任务不存在")

    _check_vfy_permission(user, task, VFY_ACTION_EXPORT)

    if not task.result_json:
        raise HTTPException(status_code=400, detail="校验任务无结果数据")

    try:
        result = json.loads(task.result_json)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"结果解析失败: {e}")

    result = _apply_result_desensitization(result, user)
    can_see_batch = result.get("can_see_batch_detail", False)

    events = result.get("events", [])
    if not events and task.task_type == "batch_trace":
        events = result.get("all_events", [])

    rows = []
    for idx, e in enumerate(events):
        rows.append({
            "seq": idx + 1,
            "event_time": e.get("created_at", ""),
            "event_type": e.get("event_type", ""),
            "event_label": e.get("event_label", ""),
            "operator_id": e.get("operator_id", ""),
            "operator_name": e.get("operator_name", ""),
            "operator_role": e.get("operator_role", ""),
            "from_status": e.get("from_status", ""),
            "to_status": e.get("to_status", ""),
            "changed_fields": ", ".join(e.get("changed_fields", [])) if e.get("changed_fields") else "",
            "is_batch_operation": "是" if e.get("is_batch_operation") else "否",
            "batch_no": e.get("batch_no", "") if can_see_batch else "",
            "success": "成功" if e.get("success") else "失败",
            "fail_reason": e.get("fail_reason", "") if e.get("success") is False else "",
            "note": e.get("note", ""),
        })

    task.export_count = (task.export_count or 0) + 1
    task.last_export_at = datetime.utcnow()
    task.last_export_by_id = user.id
    task.last_export_by_name = user.name

    log = AuditLog(
        action="verification_export",
        operator_id=user.id,
        operator_name=user.name,
        target_type="verification_task",
        target_id=task.task_no,
        detail=f"导出校验任务 {task.task_no} 的审计包（CSV），第{task.export_count}次导出",
    )
    db.add(log)
    db.commit()

    if not rows:
        headers = []
    else:
        headers = list(rows[0].keys())

    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)

    header_lines = [
        ["task_no", task.task_no],
        ["task_type", task.task_type],
        ["target_order_no", task.target_order_no or ""],
        ["target_batch_no", task.target_batch_no or ""],
        ["created_at", task.created_at.isoformat() if task.created_at else ""],
        ["completed_at", task.completed_at.isoformat() if task.completed_at else ""],
        ["rerun_count", str(task.rerun_count)],
        ["export_count", str(task.export_count)],
        ["event_count", str(task.event_count)],
        ["conflict_count", str(task.conflict_count)],
        ["exported_by", user.name],
        ["exported_at", datetime.utcnow().isoformat()],
        [],
    ]
    header_buf = io.StringIO(newline="")
    hw = csv.writer(header_buf)
    for hl in header_lines:
        hw.writerow(hl)

    content = (header_buf.getvalue() + buf.getvalue()).encode("utf-8-sig")
    resp = StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
    )
    fname = f"verification_audit_{task.task_no}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


@app.get("/api/verification/cleanup")
def cleanup_verification_tasks(
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    _require_role(user, Role.ADMIN)
    count = _cleanup_expired_verifications(db)
    log = AuditLog(
        action="verification_cleanup",
        operator_id=user.id,
        operator_name=user.name,
        target_type="verification_task",
        target_id="all",
        detail=f"清理过期校验任务，共清理 {count} 条",
    )
    db.add(log)
    db.commit()
    return {"cleaned": count}


# ---- System Config APIs ----

@app.get("/api/system/configs", response_model=List[SystemConfigOut])
def list_system_configs(
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    _require_role(user, Role.ADMIN)
    configs = db.query(SystemConfig).order_by(SystemConfig.id).all()
    return configs


@app.get("/api/system/configs/{config_key}", response_model=SystemConfigOut)
def get_system_config(
    config_key: str,
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    _require_role(user, Role.ADMIN)
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == config_key).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="配置项不存在")
    return cfg


@app.put("/api/system/configs/{config_key}", response_model=SystemConfigOut)
def update_system_config(
    config_key: str,
    data: UpdateSystemConfigIn,
    db: Session = Depends(get_db),
    user: User = Depends(_current_user),
):
    _require_role(user, Role.ADMIN)
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == config_key).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="配置项不存在")

    if cfg.config_type == "int":
        try:
            int(data.config_value)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="值必须为整数")
    elif cfg.config_type == "bool":
        if str(data.config_value).lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
            raise HTTPException(status_code=400, detail="值必须为布尔值 (true/false)")

    cfg.config_value = data.config_value
    cfg.updated_by = user.name
    db.commit()
    db.refresh(cfg)

    log = AuditLog(
        action="system_config_update",
        operator_id=user.id,
        operator_name=user.name,
        target_type="system_config",
        target_id=config_key,
        detail=f"修改系统配置 {config_key} = {data.config_value}",
    )
    db.add(log)
    db.commit()

    return cfg
