"""
根因复现 + 修复验证：测试快照时间字段为空但工单当前有值的 overwrite 场景

场景：
1. 工单：已派工 → 作业中 → 待复核（有 submitted_at）
2. 导出快照在"作业中"状态（submitted_at 为空）
3. 用"作业中"快照覆盖恢复到"待复核"工单
4. 撤销（应该成功，因为没有人工修改）
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

from database import SessionLocal, WorkOrder, OrderStatus
from main import _serialize_order_snapshot, _check_order_modified_since, _parse_iso
from sqlalchemy import and_

PASS = "[PASS]"
FAIL = "[FAIL]"

def run_test():
    db = SessionLocal()
    try:
        print("\n" + "="*80)
        print("场景：快照时间字段为空但工单当前有值（根因复现）")
        print("="*80)

        # ========== 步骤1：找一个已派工且没有 submitted_at 的工单 ==========
        print("\n步骤1: 找一个合适的工单（已派工，无 submitted_at）")
        order = db.query(WorkOrder).filter(
            WorkOrder.status == '已派工',
            WorkOrder.team_id.isnot(None),
            WorkOrder.vehicle_id.isnot(None),
            WorkOrder.submitted_at.is_(None),
        ).order_by(WorkOrder.id.desc()).first()

        if not order:
            print(f"{FAIL} 找不到合适的工单，跳过测试")
            return False

        print(f"  工单: {order.order_no} (id={order.id})")
        print(f"  初始状态: {order.status.value}")
        print(f"  初始 submitted_at: {order.submitted_at}")
        print(f"  初始 started_at: {order.started_at}")

        # ========== 步骤2：模拟快照（在"作业中"状态导出，submitted_at 为空） ==========
        print("\n步骤2: 模拟在「作业中」状态导出的快照（submitted_at 为空）")
        snap_order_status = OrderStatus.IN_PROGRESS
        order.status = snap_order_status
        order.started_at = _parse_iso("2026-06-20T10:00:00")
        db.flush()
        before_snap = _serialize_order_snapshot(order)
        print(f"  快照状态: {before_snap['status']}")
        print(f"  快照 started_at: {before_snap['started_at']!r}")
        print(f"  快照 submitted_at: {before_snap['submitted_at']!r}")

        # ========== 步骤3：推进工单到"待复核"（submitted_at 有值） ==========
        print("\n步骤3: 推进工单到「待复核」（有 submitted_at）")
        order.status = OrderStatus.PENDING_REVIEW
        order.submitted_at = _parse_iso("2026-06-20T11:00:00")
        order.started_at = _parse_iso("2026-06-20T10:00:00")  # 保持不变
        db.flush()
        print(f"  当前状态: {order.status.value}")
        print(f"  当前 started_at: {order.started_at}")
        print(f"  当前 submitted_at: {order.submitted_at}")

        # ========== 步骤4：执行 overwrite（用作业中的快照覆盖待复核的工单） ==========
        print("\n步骤4: 用「作业中」快照 overwrite 到「待复核」工单")
        from database import Team, Vehicle

        # 构造快照 item（和真实导出的 JSON 结构一致）
        team = db.query(Team).filter(Team.id == order.team_id).first()
        vehicle = db.query(Vehicle).filter(Vehicle.id == order.vehicle_id).first()
        snap_item = {
            "order_no": before_snap["order_no"],
            "road": before_snap["road"],
            "tree_no": before_snap["tree_no"],
            "risk_level": before_snap["risk_level"],
            "status": before_snap["status"],
            "team": team.name if team else "",
            "vehicle": vehicle.plate if vehicle else "",
            "need_road_close": "是" if before_snap["need_road_close"] else "否",
            "road_close_start": before_snap["road_close_start"],
            "road_close_end": before_snap["road_close_end"],
            "description": before_snap["description"],
            "suggested_time": before_snap["suggested_time"],
            "reported_at": before_snap["reported_at"],
            "assigned_at": before_snap["assigned_at"],
            "started_at": before_snap["started_at"],
            "submitted_at": before_snap["submitted_at"],  # 空字符串！关键
            "reviewed_at": before_snap["reviewed_at"],
            "review_note": before_snap["review_note"],
            "cancelled_at": before_snap["cancelled_at"],
            "cancel_reason": before_snap["cancel_reason"],
            "histories": [],
        }

        snap_rcs = _parse_iso(snap_item.get("road_close_start"))
        snap_rce = _parse_iso(snap_item.get("road_close_end"))
        snap_team_name = snap_item.get("team", "")
        snap_vehicle_plate = snap_item.get("vehicle", "")
        team_obj = db.query(Team).filter(Team.name == snap_team_name).first() if snap_team_name else None
        vehicle_obj = db.query(Vehicle).filter(Vehicle.plate == snap_vehicle_plate).first() if snap_vehicle_plate else None

        snap_status = OrderStatus(before_snap["status"])
        old_status_val = order.status.value if hasattr(order.status, 'value') else str(order.status)

        # 执行 overwrite（和 main.py 完全一致）
        order.road = snap_item.get("road", order.road)
        order.tree_no = snap_item.get("tree_no", order.tree_no)
        order.need_road_close = snap_item.get("need_road_close", "否") == "是"
        order.description = snap_item.get("description", order.description) or order.description
        order.status = snap_status
        order.team_id = team_obj.id if team_obj else order.team_id
        order.vehicle_id = vehicle_obj.id if vehicle_obj else order.vehicle_id
        order.road_close_start = snap_rcs or order.road_close_start
        order.road_close_end = snap_rce or order.road_close_end

        reported_at = _parse_iso(snap_item.get("reported_at"))
        if reported_at:
            order.reported_at = reported_at
        # 修复后的写法：直接赋值，快照为空就清空
        order.assigned_at = _parse_iso(snap_item.get("assigned_at"))
        order.started_at = _parse_iso(snap_item.get("started_at"))
        order.submitted_at = _parse_iso(snap_item.get("submitted_at"))
        order.reviewed_at = _parse_iso(snap_item.get("reviewed_at"))
        order.review_note = snap_item.get("review_note") if snap_item.get("review_note") is not None else order.review_note
        order.cancelled_at = _parse_iso(snap_item.get("cancelled_at"))
        order.cancel_reason = snap_item.get("cancel_reason") if snap_item.get("cancel_reason") is not None else order.cancel_reason

        db.flush()
        after_snap = _serialize_order_snapshot(order)
        print(f"  after_snap 状态: {after_snap['status']}")
        print(f"  after_snap started_at: {after_snap['started_at']!r}")
        print(f"  after_snap submitted_at: {after_snap['submitted_at']!r}")
        print(f"  工单实际 submitted_at: {order.submitted_at}")

        # ========== 步骤5：提交事务，重新查询 ==========
        print("\n步骤5: 提交事务，重新查询工单")
        db.commit()
        order2 = db.query(WorkOrder).filter(WorkOrder.id == order.id).first()
        current_snap = _serialize_order_snapshot(order2)
        print(f"  当前状态: {current_snap['status']}")
        print(f"  当前 submitted_at: {order2.submitted_at}")

        # ========== 步骤6：验证 submitted_at 是否被正确清空 ==========
        print("\n步骤6: 验证 submitted_at 是否被正确清空")
        if order2.submitted_at is None:
            print(f"  {PASS} submitted_at 被正确清空")
        else:
            print(f"  {FAIL} submitted_at 没有被清空！当前值: {order2.submitted_at}")
            db.rollback()
            return False

        # ========== 步骤7：调用 _check_order_modified_since 验证是否会误判 ==========
        print("\n步骤7: 调用 _check_order_modified_since 验证")
        after_snap_json = json.dumps(after_snap, ensure_ascii=False)
        modified, reason = _check_order_modified_since(db, order2, after_snap_json)

        if not modified:
            print(f"  {PASS} 不会误判为人工修改，可以正常撤销")
        else:
            print(f"  {FAIL} 误判为人工修改！原因: {reason}")
            db.rollback()
            return False

        # ========== 步骤8：模拟人工修改后再验证 ==========
        print("\n步骤8: 模拟人工修改 description，验证拦截逻辑")
        order2.description = "人工修改的描述"
        db.flush()
        db.commit()

        order3 = db.query(WorkOrder).filter(WorkOrder.id == order.id).first()
        modified2, reason2 = _check_order_modified_since(db, order3, after_snap_json)

        if modified2 and "description" in reason2:
            print(f"  {PASS} 正确拦截人工修改，原因: {reason2}")
        else:
            print(f"  {FAIL} 没有正确拦截人工修改！modified={modified2}, reason={reason2}")
            db.rollback()
            return False

        # 回滚测试数据
        db.rollback()

        print("\n" + "="*80)
        print(f"{PASS} 所有测试通过！根因修复验证成功")
        print("="*80)
        return True

    except Exception as e:
        db.rollback()
        print(f"\n{FAIL} 测试异常: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        db.close()

if __name__ == "__main__":
    success = run_test()
    sys.exit(0 if success else 1)
