"""
端到端诊断 v2：
场景：恢复前队伍/车辆/封路/assigned_at 为空，恢复后有值（带Z时间），撤销后真空掉
"""
import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

import requests
from database import SessionLocal, RestoreBatchItem, WorkOrder, init_db
from main import _serialize_order_snapshot, _parse_iso
from datetime import datetime, timezone

BASE_URL = "http://127.0.0.1:8001"
ADMIN_HEADERS = {"X-User-Id": "1"}
INSPECTOR_HEADERS = {"X-User-Id": "2"}

init_db()
db = SessionLocal()

# Step 1: 创建工单（不派工）
print("Step 1: 创建工单（不派工，队伍/车辆/封路/assigned_at 全部为空）")
r = requests.post(f"{BASE_URL}/api/orders/report", json={
    "road": "诊断路Z2",
    "tree_no": f"DIAG-Z2-{int(time.time())}",
    "risk_level": "低",
    "need_road_close": False,
    "description": "Z时间+空值边界诊断",
}, headers=INSPECTOR_HEADERS)
order_id = r.json()["id"]
order_no = r.json()["order_no"]
print(f"  order_id={order_id}, order_no={order_no}")

r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
before = r.json()
print(f"  恢复前: status={before['status']}")
print(f"  team_id={before.get('team_id')!r}, vehicle_id={before.get('vehicle_id')!r}")
print(f"  assigned_at={before.get('assigned_at')!r}")
print(f"  road_close_start={before.get('road_close_start')!r}, road_close_end={before.get('road_close_end')!r}")

# Step 2: 导入快照（need_road_close=False，不检查冲突；状态=已派工 > 待派工）
print("\nStep 2: 导入带 Z 时间的快照（无封路，避免冲突检测）")
snap = {
    "order_no": order_no,
    "road": before["road"],
    "tree_no": before["tree_no"],
    "risk_level": before["risk_level"],
    "status": "已派工",
    "team": "修剪一队",
    "vehicle": "京A·12345",
    "need_road_close": "否",   # 关键：不封路，就不会检查队伍/车辆时间冲突
    "road_close_start": "",
    "road_close_end": "",
    "description": "带 Z 时间+空值边界",
    "suggested_time": "",
    "reported_at": "2026-06-19T18:26:08.946538Z",
    "assigned_at": "2026-06-19T18:26:09.015713Z",   # 带 Z
    "started_at": "2026-06-20T02:00:00.123456Z",      # 带 Z
    "submitted_at": "",
    "reviewed_at": "",
    "review_note": "",
    "cancelled_at": "",
    "cancel_reason": "",
    "histories": [],
}

selected = [{"order_no": order_no, "decision": "overwrite_or_skip"}]
r = requests.post(f"{BASE_URL}/api/snapshot/import", json={
    "snapshot_version": "diag-z2",
    "exported_at": "2026-06-20T00:00:00Z",
    "orders": [snap],
    "selected": selected,
}, headers=ADMIN_HEADERS)
result = r.json()
batch_id = result["batch_id"]
print(f"  批次ID={batch_id}, imported={result.get('imported')}, skipped={result.get('skipped')}")
for item in result.get("items", []):
    print(f"  - {item['order_no']}: action={item['action']}, success={item['success']}, reason={item.get('reason')}")

assert result.get("imported") == 1, f"导入应该成功1条，实际: {result}"

# Step 3: 查 after_snapshot_json 存储内容
print("\nStep 3: 查数据库 after_snapshot_json")
batch_item = db.query(RestoreBatchItem).filter(RestoreBatchItem.batch_id == batch_id).first()
after_snap = json.loads(batch_item.after_snapshot_json) if batch_item.after_snapshot_json else {}
print(f"  时间字段:")
for k in ['reported_at', 'assigned_at', 'started_at']:
    v = after_snap.get(k)
    has_tz = '+00:00' in str(v) or 'Z' in str(v)
    print(f"    {k}: {v!r} {'<-- 带时区！问题！' if has_tz else '<-- 无时区，OK'}")

# Step 4: 当前工单序列化
db_order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
curr_snap = _serialize_order_snapshot(db_order)
print(f"\nStep 4: 当前工单序列化时间字段:")
for k in ['reported_at', 'assigned_at', 'started_at']:
    print(f"    {k}: {curr_snap.get(k)!r}")

# Step 5: 逐字段比较
print("\nStep 5: 逐字段比较（_check_order_modified_since 的逻辑）")
changed = []
for key in curr_snap:
    if key in {"id", "order_no"}:
        continue
    cur_val = curr_snap.get(key)
    snap_val = after_snap.get(key)
    if cur_val != snap_val:
        changed.append(key)
        print(f"  ❌ {key}: snap={snap_val!r} vs curr={cur_val!r}")

if changed:
    print(f"\n结论: ❌ 有 {len(changed)} 个字段不一致: {', '.join(changed)}")
else:
    print(f"\n结论: ✅ 全部一致！时区归一修复有效")

# Step 6: 验证恢复后状态（应有队伍/车辆/assigned_at）
print("\nStep 6: 验证恢复后状态（队伍/车辆/assigned_at 应该有值）")
r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
after_restore = r.json()
assert after_restore["status"] == "已派工", f"状态应该是已派工，实际: {after_restore['status']}"
assert after_restore.get("team_id") is not None, "恢复后 team_id 应该有值"
assert after_restore.get("vehicle_id") is not None, "恢复后 vehicle_id 应该有值"
assert after_restore.get("assigned_at"), "恢复后 assigned_at 应该有值"
print(f"  status={after_restore['status']}")
print(f"  team_id={after_restore.get('team_id')}, vehicle_id={after_restore.get('vehicle_id')}")
print(f"  assigned_at={after_restore.get('assigned_at')!r}")
print(f"  ✅ 恢复后状态正确")

# Step 7: 立即撤销
print("\nStep 7: 立即撤销本批次")
r = requests.post(f"{BASE_URL}/api/restore-batches/{batch_id}/revoke",
                 json={"reason": "诊断-Z时间+空值边界撤销"},
                 headers=ADMIN_HEADERS)
print(f"  状态码: {r.status_code}")
revoke_result = r.json()
print(f"  响应: {json.dumps(revoke_result, ensure_ascii=False, indent=2)}")

# Step 8: 验证撤销后真空掉
print("\nStep 8: 验证撤销后（队伍/车辆/封路/assigned_at 应该真空为 None）")
r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
after_revoke = r.json()
print(f"  status={after_revoke['status']}")
print(f"  team_id={after_revoke.get('team_id')!r}")
print(f"  vehicle_id={after_revoke.get('vehicle_id')!r}")
print(f"  assigned_at={after_revoke.get('assigned_at')!r}")
print(f"  road_close_start={after_revoke.get('road_close_start')!r}, road_close_end={after_revoke.get('road_close_end')!r}")

errors = []
if after_revoke["status"] != "待派工":
    errors.append(f"状态应该回到待派工，实际: {after_revoke['status']}")
if after_revoke.get("team_id") is not None:
    errors.append(f"team_id 应该真空为 None！实际: {after_revoke.get('team_id')!r} — 队伍资源被错误占用！")
if after_revoke.get("vehicle_id") is not None:
    errors.append(f"vehicle_id 应该真空为 None！实际: {after_revoke.get('vehicle_id')!r} — 车辆资源被错误占用！")
if after_revoke.get("assigned_at") not in (None, "", "null"):
    errors.append(f"assigned_at 应该真空！实际: {after_revoke.get('assigned_at')!r}")
if after_revoke.get("road_close_start") not in (None, "", "null"):
    errors.append(f"road_close_start 应该真空！实际: {after_revoke.get('road_close_start')!r}")
if after_revoke.get("road_close_end") not in (None, "", "null"):
    errors.append(f"road_close_end 应该真空！实际: {after_revoke.get('road_close_end')!r}")

if errors:
    print(f"\n❌ 撤销后空值边界验证失败:")
    for e in errors:
        print(f"   - {e}")
else:
    print(f"\n✅ 空值边界验证通过：队伍/车辆/封路/assigned_at 全部真空掉")

db.close()
