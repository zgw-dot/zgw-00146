"""
精确复现：模拟完整的 overwrite 流程，比较保存的 after_snap 和当前序列化结果
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

from database import SessionLocal, WorkOrder, RestoreBatch, RestoreBatchItem, RestoreBatchStatus, RestoreBatchItemAction
from main import _serialize_order_snapshot, _check_order_modified_since, _parse_iso
from sqlalchemy import or_

db = SessionLocal()

try:
    print("=" * 70)
    print("步骤 1: 找一个已派工的工单")
    print("=" * 70)
    order = db.query(WorkOrder).filter(
        WorkOrder.status == '已派工',
        WorkOrder.team_id.isnot(None),
        WorkOrder.vehicle_id.isnot(None),
    ).order_by(WorkOrder.id.desc()).first()

    if not order:
        print("找不到已派工的工单")
        exit(1)

    print(f"工单: {order.order_no} (id={order.id})")
    order_status_val = order.status.value if hasattr(order.status, 'value') else str(order.status)
    print(f"当前状态: {order_status_val}")
    print(f"team_id: {order.team_id}, vehicle_id: {order.vehicle_id}")
    print(f"assigned_at: {order.assigned_at}")
    print(f"started_at: {order.started_at}")

    # 保存恢复前的快照
    before_snap = _serialize_order_snapshot(order)
    before_status = order_status_val

    print()
    print("=" * 70)
    print("步骤 2: 推进工单到作业中（模拟人工操作）")
    print("=" * 70)
    from database import OrderStatus
    order.status = OrderStatus.IN_PROGRESS
    db.flush()
    order_status_val2 = order.status.value if hasattr(order.status, 'value') else str(order.status)
    print(f"推进后状态: {order_status_val2}")

    # 模拟快照数据（从 before_snap 构造）
    snap_item = {
        "order_no": order.order_no,
        "road": before_snap["road"],
        "tree_no": before_snap["tree_no"],
        "risk_level": before_snap["risk_level"],
        "status": before_snap["status"],
        "team": "修剪一队",  # team_id=1 对应的名称
        "vehicle": "京A·12345",  # vehicle_id=1 对应的车牌号
        "need_road_close": "是" if before_snap["need_road_close"] else "否",
        "road_close_start": before_snap["road_close_start"],
        "road_close_end": before_snap["road_close_end"],
        "assigned_at": before_snap["assigned_at"],
        "started_at": before_snap["started_at"],
        "description": before_snap["description"],
        "suggested_time": before_snap["suggested_time"],
        "reported_at": before_snap["reported_at"],
        "histories": [],
    }

    print()
    print("=" * 70)
    print("步骤 3: 执行 overwrite（与 main.py 第1186-1245行完全一致）")
    print("=" * 70)

    # 解析快照字段
    snap_rcs = _parse_iso(snap_item.get("road_close_start"))
    snap_rce = _parse_iso(snap_item.get("road_close_end"))
    snap_team_name = snap_item.get("team", "")
    snap_vehicle_plate = snap_item.get("vehicle", "")

    from database import Team, Vehicle
    team_obj = db.query(Team).filter(Team.name == snap_team_name).first() if snap_team_name else None
    vehicle_obj = db.query(Vehicle).filter(Vehicle.plate == snap_vehicle_plate).first() if snap_vehicle_plate else None

    old_status_val = order.status.value if hasattr(order.status, 'value') else str(order.status)
    order.road = snap_item.get("road", order.road)
    order.tree_no = snap_item.get("tree_no", order.tree_no)
    order.need_road_close = snap_item.get("need_road_close", "否") == "是"
    order.description = snap_item.get("description", order.description) or order.description
    try:
        order.status = OrderStatus(before_snap["status"])
    except ValueError:
        order.status = before_snap["status"]
    order.team_id = team_obj.id if team_obj else order.team_id
    order.vehicle_id = vehicle_obj.id if vehicle_obj else order.vehicle_id
    order.road_close_start = snap_rcs or order.road_close_start
    order.road_close_end = snap_rce or order.road_close_end

    assigned_at = _parse_iso(snap_item.get("assigned_at"))
    if assigned_at:
        order.assigned_at = assigned_at
    started_at = _parse_iso(snap_item.get("started_at"))
    if started_at:
        order.started_at = started_at
    submitted_at = _parse_iso(snap_item.get("submitted_at"))
    if submitted_at:
        order.submitted_at = submitted_at
    reviewed_at = _parse_iso(snap_item.get("reviewed_at"))
    if reviewed_at:
        order.reviewed_at = reviewed_at
    cancelled_at = _parse_iso(snap_item.get("cancelled_at"))
    if cancelled_at:
        order.cancelled_at = cancelled_at

    # 关键：db.flush() 然后序列化
    db.flush()

    # 这里保存的 after_snap
    saved_after_snap = _serialize_order_snapshot(order)
    print(f"flush 后序列化 saved_after_snap:")
    for k, v in sorted(saved_after_snap.items()):
        print(f"  {k}: {v!r}")

    print()
    print("=" * 70)
    print("步骤 4: 提交事务，重新查询工单")
    print("=" * 70)
    db.commit()

    # 重新查询
    order2 = db.query(WorkOrder).filter(WorkOrder.id == order.id).first()
    current_after_snap = _serialize_order_snapshot(order2)
    print(f"重新查询后序列化 current_after_snap:")
    for k, v in sorted(current_after_snap.items()):
        print(f"  {k}: {v!r}")

    print()
    print("=" * 70)
    print("步骤 5: 比较两个快照")
    print("=" * 70)
    changed = []
    for key in saved_after_snap:
        if key in ("id", "order_no"):
            continue
        sv = saved_after_snap.get(key)
        cv = current_after_snap.get(key)
        if sv != cv:
            changed.append(key)
            print(f"  FAIL {key}: saved={sv!r} vs current={cv!r}")

    if changed:
        print(f"\nFAIL 发现 {len(changed)} 个差异字段: {changed}")
    else:
        print(f"\nPASS 没有差异字段，基线一致")

    print()
    print("=" * 70)
    print("步骤 6: 调用 _check_order_modified_since")
    print("=" * 70)
    after_snap_json = json.dumps(saved_after_snap, ensure_ascii=False)
    modified, reason = _check_order_modified_since(db, order2, after_snap_json)
    print(f"是否被修改: {modified}")
    print(f"原因: {reason}")

    if not modified:
        print("\nPASS 不会被拦截，可以正常撤销")
    else:
        print(f"\nFAIL 会被拦截！根因是: {reason}")

    # 额外检查：started_at 字段
    print()
    print("=" * 70)
    print("额外检查：started_at 字段细节")
    print("=" * 70)
    print(f"overwrite 前 order.started_at: {order.started_at!r}")
    print(f"快照 snap_item.started_at: {snap_item.get('started_at')!r}")
    print(f"_parse_iso 结果: {_parse_iso(snap_item.get('started_at'))!r}")
    print(f"overwrite 后 order.started_at: {order.started_at!r}")
    print(f"saved_after_snap.started_at: {saved_after_snap.get('started_at')!r}")
    print(f"current_after_snap.started_at: {current_after_snap.get('started_at')!r}")

    db.rollback()

except Exception as e:
    db.rollback()
    raise
finally:
    db.close()
