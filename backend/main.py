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
    o.status = OrderStatus.IN_PROGRESS
    o.started_at = datetime.utcnow()
    o.start_operator_id = user.id
    _record_history(db, o, old_status, OrderStatus.IN_PROGRESS, user, note="开始作业")
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
    o.status = OrderStatus.PENDING_REVIEW
    o.submitted_at = datetime.utcnow()
    o.submit_operator_id = user.id
    _record_history(db, o, old_status, OrderStatus.PENDING_REVIEW, user, note="作业完成，提交复核")
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
    o.status = OrderStatus.COMPLETED
    o.reviewed_at = datetime.utcnow()
    o.reviewer_id = user.id
    o.review_note = data.review_note
    _record_history(db, o, old_status, OrderStatus.COMPLETED, user,
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
    o.status = OrderStatus.CANCELLED
    o.cancelled_at = datetime.utcnow()
    o.canceller_id = user.id
    o.cancel_reason = data.reason.strip()
    _record_history(db, o, old_status, OrderStatus.CANCELLED, user,
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
    payload = {
        "snapshot_version": SNAPSHOT_VERSION,
        "exported_at": datetime.utcnow().isoformat(),
        "exported_by": {"id": user.id, "name": user.name, "role": user.role.value},
        "total": None,
        "orders": None,
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
    imported_count = 0
    skipped_count = 0
    rejected_count = 0
    failed_count = 0
    not_selected_count = 0

    for item in data.orders:
        order_no = item.get("order_no", "")

        if has_user_selection and order_no not in selected_map:
            not_selected_count += 1
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
            results.append(SnapshotImportItemOut(
                order_no=order_no, action="skip", success=True,
                reason="用户选择跳过",
            ))
            continue

        decision = user_decision or auto_decision

        if decision == "reject":
            reject_reasons = "; ".join(c["message"] for c in conflicts if c.get("action") == "reject")
            rejected_count += 1
            results.append(SnapshotImportItemOut(
                order_no=order_no, action="reject", success=False,
                reason=reject_reasons or "存在不可恢复冲突",
            ))
            continue

        if decision == "skip":
            skip_reasons = "; ".join(c["message"] for c in conflicts if c.get("action") == "skip")
            skipped_count += 1
            results.append(SnapshotImportItemOut(
                order_no=order_no, action="skip", success=True,
                reason=skip_reasons or "终态记录自动跳过",
            ))
            continue

        existing = db.query(WorkOrder).filter(WorkOrder.order_no == order_no).first()

        try:
            if existing and decision == "overwrite_or_skip":
                snap_status_str = item.get("status", "")
                try:
                    snap_status = OrderStatus(snap_status_str)
                except ValueError:
                    skipped_count += 1
                    results.append(SnapshotImportItemOut(
                        order_no=order_no, action="skip", success=False,
                        reason=f"快照状态「{snap_status_str}」无效",
                    ))
                    continue

                snap_rank = STATUS_RANK.get(snap_status, -1)
                existing_rank = STATUS_RANK.get(existing.status, 0)

                if existing.status in (OrderStatus.COMPLETED, OrderStatus.CANCELLED):
                    rejected_count += 1
                    results.append(SnapshotImportItemOut(
                        order_no=order_no, action="reject", success=False,
                        reason=f"当前工单状态为「{existing.status.value}」，禁止覆盖",
                    ))
                    continue

                if snap_rank < existing_rank:
                    skipped_count += 1
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
                existing.team_id = team_obj.id if team_obj else existing.team_id
                existing.vehicle_id = vehicle_obj.id if vehicle_obj else existing.vehicle_id
                existing.road_close_start = snap_rcs or existing.road_close_start
                existing.road_close_end = snap_rce or existing.road_close_end

                reported_at = _parse_iso(item.get("reported_at"))
                if reported_at:
                    existing.reported_at = reported_at
                assigned_at = _parse_iso(item.get("assigned_at"))
                if assigned_at:
                    existing.assigned_at = assigned_at
                started_at = _parse_iso(item.get("started_at"))
                if started_at:
                    existing.started_at = started_at
                submitted_at = _parse_iso(item.get("submitted_at"))
                if submitted_at:
                    existing.submitted_at = submitted_at
                reviewed_at = _parse_iso(item.get("reviewed_at"))
                if reviewed_at:
                    existing.reviewed_at = reviewed_at
                existing.review_note = item.get("review_note") or existing.review_note
                cancelled_at = _parse_iso(item.get("cancelled_at"))
                if cancelled_at:
                    existing.cancelled_at = cancelled_at
                existing.cancel_reason = item.get("cancel_reason") or existing.cancel_reason

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

                imported_count += 1
                results.append(SnapshotImportItemOut(
                    order_no=order_no, action="create", success=True,
                    reason=f"新建工单，状态={snap_status.value}",
                ))
                continue

            skipped_count += 1
            results.append(SnapshotImportItemOut(
                order_no=order_no, action="skip", success=True,
                reason="无匹配操作，自动跳过",
            ))

        except Exception as e:
            failed_count += 1
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

    log = AuditLog(
        action="snapshot_import",
        operator_id=user.id,
        operator_name=user.name,
        target_type="snapshot",
        target_id=data.exported_at or "unknown",
        detail=audit_detail,
        snapshot_version=data.snapshot_version,
    )
    db.add(log)
    db.commit()

    return SnapshotImportOut(
        total=len(data.orders),
        imported=imported_count,
        skipped=skipped_count,
        rejected=rejected_count,
        failed=failed_count,
        not_selected=not_selected_count,
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
