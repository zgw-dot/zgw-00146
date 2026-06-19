"""
端到端诊断：导入一个带 Z 时间的快照，查数据库里 after_snapshot_json 存了什么，
然后和当前工单序列化结果逐字段对比
"""
import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

import requests
from database import SessionLocal, RestoreBatchItem, init_db
from main import _serialize_order_snapshot, _parse_iso
from datetime import datetime, timezone

BASE_URL = "http://127.0.0.1:8001"
ADMIN_HEADERS = {"X-User-Id": "1"}
INSPECTOR_HEADERS = {"X-User-Id": "2"}

init_db()
db = SessionLocal()

# Step 1: 创建工单（不派工，队伍/车辆空，避免冲突）
print("Step 1: 创建工单（不派工）")
r = requests.post(f"{BASE_URL}/api/orders/report", json={
    "road": "诊断路Z",
    "tree_no": f"DIAG-Z-{int(time.time())}",
    "risk_level": "中",
    "need_road_close": False,
    "description": "Z时间诊断",
}, headers=INSPECTOR_HEADERS)
order_id = r.json()["id"]
order_no = r.json()["order_no"]
print(f"  order_id={order_id}, order_no={order_no}")
# 不派工！这样队伍/车辆是空的，而且状态是待派工(0)，恢复快照用已派工(1)
print(f"  不派工，状态将是待派工")

r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
curr = r.json()
print(f"  当前状态: {curr['status']}")

# Step 2: 构造带 Z 时间的快照并导入
print("\nStep 2: 导入带 Z 时间的快照")
snap = {
    "order_no": order_no,
    "road": curr["road"],
    "tree_no": curr["tree_no"],
    "risk_level": curr["risk_level"],
    "status": "已派工",  # 排名1 > 待派工0
    "team": "修剪一队",
    "vehicle": "京A·12345",
    "need_road_close": "是",
    "road_close_start": "2199-06-25T01:00:00.000Z",  # 用非常远的未来，避免冲突
    "road_close_end": "2199-06-25T03:00:00.000Z",
    "description": "带 Z 时间诊断",
    "suggested_time": "",
    "reported_at": "2026-06-19T18:26:08.946538Z",
    "assigned_at": "2026-06-19T18:26:09.015713Z",
    "started_at": "2026-06-20T02:00:00.123456Z",
    "submitted_at": "",
    "reviewed_at": "",
    "review_note": "",
    "cancelled_at": "",
    "cancel_reason": "",
    "histories": [],
}

selected = [{"order_no": order_no, "decision": "overwrite_or_skip"}]
r = requests.post(f"{BASE_URL}/api/snapshot/import", json={
    "snapshot_version": "diag-z-time",
    "exported_at": "2026-06-20T00:00:00Z",
    "orders": [snap],
    "selected": selected,
}, headers=ADMIN_HEADERS)
result = r.json()
batch_id = result["batch_id"]
print(f"  批次ID={batch_id}, imported={result.get('imported')}, skipped={result.get('skipped')}")
for item in result.get("items", []):
    print(f"  - {item['order_no']}: action={item['action']}, success={item['success']}, reason={item.get('reason')}")

# Step 3: 从数据库直接查 after_snapshot_json
print("\nStep 3: 从数据库查 RestoreBatchItem.after_snapshot_json")
batch_item = db.query(RestoreBatchItem).filter(RestoreBatchItem.batch_id == batch_id).first()
if batch_item:
    after_snap = json.loads(batch_item.after_snapshot_json) if batch_item.after_snapshot_json else {}
    print(f"  after_snapshot_json 中的时间字段:")
    for k in ['road_close_start', 'road_close_end', 'reported_at', 'assigned_at', 'started_at', 'submitted_at']:
        print(f"    {k}: {after_snap.get(k)!r}")

# Step 4: 从数据库取当前工单，序列化
print("\nStep 4: 当前工单序列化结果")
# 先从 API 拿
r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
curr_api = r.json()

# 直接 DB 序列化
from database import WorkOrder
db_order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
curr_snap = _serialize_order_snapshot(db_order)
print(f"  DB 序列化的时间字段:")
for k in ['road_close_start', 'road_close_end', 'reported_at', 'assigned_at', 'started_at', 'submitted_at']:
    print(f"    {k}: {curr_snap.get(k)!r}")

# Step 5: 逐字段比较 after_snap vs curr_snap
print("\nStep 5: 逐字段比较 (after_snap vs curr_snap) — 这就是 _check_order_modified_since 做的事")
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
    print(f"\n结论: 有 {len(changed)} 个字段不一致，会被误判为人工修改: {', '.join(changed)}")
else:
    print(f"\n结论: ✅ 全部一致，不会误判")

db.close()
