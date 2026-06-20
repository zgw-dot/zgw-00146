"""最小化复现测试"""
import requests
import sys

BASE = "http://127.0.0.1:8005/api"

admin = requests.Session()
admin.headers["X-User-Id"] = "1"

print("1. 获取一个工单...")
r = admin.get(f"{BASE}/orders?limit=1")
print(f"   Status: {r.status_code}")
if r.status_code != 200:
    print(f"   Error: {r.text}")
    sys.exit(1)
orders = r.json()
if not orders:
    print("   没有工单")
    sys.exit(1)
order_no = orders[0]["order_no"]
print(f"   使用工单: {order_no}")

print()
print("2. 创建校验任务...")
r = admin.post(f"{BASE}/verification/tasks", json={
    "task_type": "order_trace",
    "order_no": order_no,
})
print(f"   Status: {r.status_code}")
print(f"   Response: {r.text[:500]}")
if r.status_code < 300:
    data = r.json()
    task_id = data.get("id")
    print(f"   任务ID: {task_id}")
    
    print()
    print("3. 获取任务详情...")
    r2 = admin.get(f"{BASE}/verification/tasks/{task_id}")
    print(f"   Status: {r2.status_code}")
    if r2.status_code < 300:
        d = r2.json()
        print(f"   事件数: {d.get('event_count')}")
        print(f"   冲突数: {d.get('conflict_count')}")
        result = d.get("result", {})
        print(f"   result keys: {list(result.keys())}")
    else:
        print(f"   Error: {r2.text[:300]}")
