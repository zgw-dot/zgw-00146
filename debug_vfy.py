"""快速调试脚本 - 确认API行为"""
import requests
import json

BASE = "http://127.0.0.1:8005/api"

admin = requests.Session()
admin.headers["X-User-Id"] = "1"
inspector = requests.Session()
inspector.headers["X-User-Id"] = "2"

def test_api(name, r, show_body=False, show_keys=False):
    print(f"\n--- {name} ---")
    print(f"  Status: {r.status_code}")
    if 200 <= r.status_code < 300:
        try:
            data = r.json()
            if show_keys:
                if isinstance(data, dict):
                    print(f"  Keys: {list(data.keys())}")
                else:
                    print(f"  Type: {type(data)}")
            if show_body:
                body_str = json.dumps(data, ensure_ascii=False, indent=2)
                print(f"  Body: {body_str[:800]}")
            return data
        except Exception as e:
            print(f"  Parse error: {e}")
            print(f"  Text: {r.text[:300]}")
            return None
    else:
        print(f"  Error: {r.text[:500]}")
        return None

batch_id = None
order_id = None
order_no = None

# 1. 巡查员上报工单
r = inspector.post(f"{BASE}/orders/report", json={
    "road": "调试测试道路",
    "tree_no": "DEBUG-12345",
    "species": "悬铃木",
    "risk_level": "中",
    "need_road_close": False,
    "description": "调试用",
})
order = test_api("巡查员上报工单", r, show_keys=True)
if order:
    order_id = order.get("id")
    order_no = order.get("order_no")

# 2. 管理员派工
if order_id:
    teams = admin.get(f"{BASE}/teams").json()
    r = admin.post(f"{BASE}/orders/{order_id}/assign", json={
        "team_id": teams[0]["id"],
        "assign_note": "调试派工",
    })
    test_api("管理员派工", r, show_keys=True)

# 3. 巡查员编辑
if order_id:
    r = inspector.post(f"{BASE}/orders/{order_id}/edit", json={
        "description": "编辑后的描述",
        "edit_note": "调试编辑",
    })
    test_api("巡查员编辑工单", r, show_keys=True)

# 4. 快照导入
if order_no:
    r = admin.post(f"{BASE}/snapshot/import", json={
        "orders": [{
            "order_no": order_no,
            "road": "被快照覆盖了",
            "tree_no": "DEBUG-12345",
            "species": "悬铃木",
            "risk_level": "高",
            "need_road_close": True,
            "description": "快照描述",
        }],
        "note": "调试快照",
    })
    snap = test_api("快照导入", r, show_keys=True)
    if snap:
        batch_id = snap.get("batch_id")
        print(f"  restored_count: {snap.get('restored_count')}")
        print(f"  created_count: {snap.get('created_count')}")
        print(f"  skipped_count: {snap.get('skipped_count')}")

# 5. 撤销批次
if batch_id:
    r = admin.post(f"{BASE}/snapshot/batches/{batch_id}/revoke", json={
        "reason": "调试撤销",
    })
    test_api("撤销批次", r, show_keys=True)

# 6. 关闭工单
if order_id:
    r = admin.post(f"{BASE}/orders/{order_id}/close", json={
        "close_reason": "调试关闭",
        "close_note": "测试关闭",
    })
    test_api("关闭工单", r, show_keys=True)

# 7. 创建校验任务
if order_no:
    r = admin.post(f"{BASE}/verification/tasks", json={
        "task_type": "order_trace",
        "order_no": order_no,
    })
    task = test_api("创建工单校验任务", r, show_keys=True)
    if task:
        print(f"  任务编号: {task.get('task_no')}")
        print(f"  状态: {task.get('status')}")
        print(f"  事件数: {task.get('event_count')}")
        print(f"  冲突数: {task.get('conflict_count')}")

# 8. 校验任务列表
r = admin.get(f"{BASE}/verification/tasks")
tasks = test_api("校验任务列表", r, show_keys=False)
if tasks:
    print(f"  任务数量: {len(tasks)}")
    if tasks:
        print(f"  第一个任务 keys: {list(tasks[0].keys())}")
