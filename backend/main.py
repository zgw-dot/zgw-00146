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
    User, Team, Vehicle, WorkOrder, StatusHistory,
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


@app.get("/api/export/json")
def export_json(db: Session = Depends(get_db), user: User = Depends(_current_user)):
    payload = {
        "exported_at": datetime.utcnow().isoformat(),
        "exported_by": {"id": user.id, "name": user.name, "role": user.role.value},
        "total": None,
        "orders": None,
    }
    payload["orders"] = _serialize_orders_for_export(db)
    payload["total"] = len(payload["orders"])
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
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
