"""快照导入+批次撤销追溯验证"""
import requests
import sys

BASE = "http://127.0.0.1:8005/api"

def s(uid):
    r = requests.Session()
    r.headers["X-User-Id"] = str(uid)
    return r

admin = s(1)
inspector = s(2)

# 1. 巡查员上报一张工单
print("=== 1. 巡查员上报工单 ===")
import time
tree_no = f"SNAPTEST-{int(time.time()) % 100000}"
r = inspector.post(f"{BASE}/orders/report", json={
    "road": "快照测试道路",
    "tree_no": tree_no,
    "species": "悬铃木",
    "risk_level": "中",
    "need_road_close": False,
    "description": "原始描述-这是快照测试",
})
order = r.json()
order_id = order["id"]
order_no = order["order_no"]
print(f"  工单 #{order_id} {order_no} tree={tree_no}")

# 2. 管理员派工
print("\n=== 2. 管理员派工 ===")
teams = admin.get(f"{BASE}/teams").json()
admin.post(f"{BASE}/orders/{order_id}/assign", json={"team_id": teams[0]["id"]})
print("  OK")

# 3. 管理员导入快照（覆盖这张工单 + 新建一张 + 一张不选中）
print("\n=== 3. 快照导入（覆盖+新建+未选中） ===")
import time
new_order_no = f"NEW-{int(time.time()) % 100000}"
payload = {
    "orders": [
        {
            "order_no": order_no,
            "road": "快照测试道路（已被快照覆盖",
            "tree_no": tree_no,
            "species": "悬铃木",
            "risk_level": "高",
            "need_road_close": True,
            "status": "已派工",
            "team": teams[0]["name"],
            "description": "这是快照覆盖后的描述",
        },
        {
            "order_no": new_order_no,
            "road": "全新道路",
            "tree_no": "NEW-001",
            "species": "香樟",
            "risk_level": "高",
            "need_road_close": True,
            "suggested_time": "00:00-05:00",
            "status": "待派工",
            "description": "快照新建的工单",
        },
        {
            "order_no": f"UNSELECTED-{int(time.time()) % 100000}",
            "road": "未选中道路",
            "tree_no": "UNSEL-001",
            "species": "银杏",
            "risk_level": "低",
            "need_road_close": False,
            "description": "不会被导入",
        },
    ],
    "selected": [
        {"order_no": order_no, "decision": "overwrite"},
        {"order_no": new_order_no, "decision": "create"},
        # 第三张不选
    ],
}
r = admin.post(f"{BASE}/snapshot/import", json=payload)
result = r.json()
print(f"  total={result.get('total')} imported={result.get('imported')} "
      f"skipped={result.get('skipped')} rejected={result.get('rejected')} "
      f"not_selected={result.get('not_selected')}")
print(f"  batch_id={result.get('batch_id')} batch_no={result.get('batch_no')}")
batch_id = result.get("batch_id")

# 查被覆盖工单的追溯
print(f"\n=== 4. 工单 {order_no} 追溯（含快照覆盖）===")
t = admin.get(f"{BASE}/orders/{order_id}/trace").json()
print(f"  summary: {t['summary']}")
for e in t["events"]:
    flag = "OK" if e["success"] else "FAIL"
    batch = f" [批次{e['batch_no']}]" if e.get("batch_no") else ""
    fields = f" 变更={','.join(e['changed_fields'][:5])}" if e.get("changed_fields") else ""
    diff = f" diffs={len(e['diffs'])}" if e.get("diffs") else ""
    print(f"    [{flag}] [{e['event_label']}] {e['from_status'] or '创'}->{e['to_status']}{batch}{fields}{diff}")

# 5. 再制造人工修改（导致撤销恢复时跳过）
print(f"\n=== 5. 人工修改工单 {order_no}（制造撤销时的冲突）===")
admin.post(f"{BASE}/orders/{order_id}/assign", json={
    "team_id": teams[-1]["id"],
    "note": "人工改派制造冲突",
})
print("  OK")

# 6. 批次撤销/恢复
print(f"\n=== 6. 批次撤销/恢复 ===")
r = admin.post(f"{BASE}/restore-batches/{batch_id}/revoke", json={"reason": "测试追溯-撤销批次"})
revoke = r.json()
print(f"  total_revocable={revoke.get('total_revocable')} revoked={revoke.get('revoked')} failed={revoke.get('failed')}")
for it in revoke.get("items", []):
    flag = "OK" if it["success"] else "FAIL"
    print(f"    [{flag}] {it['order_no']}: [{it['action']}] {it.get('reason', '')[:60]}")

# 7. 最终追溯
print(f"\n=== 7. 工单 {order_no} 最终追溯（含批次撤销跳过） ===")
t = admin.get(f"{BASE}/orders/{order_id}/trace").json()
print(f"  summary: {t['summary']}")
for e in t["events"]:
    flag = "OK" if e["success"] else "FAIL"
    batch = f" [批次{e['batch_no']}]" if e.get("batch_no") else ""
    fields = f" 变更={','.join(e['changed_fields'][:5])}" if e.get("changed_fields") else ""
    fail = f" 失败原因={e['fail_reason'][:40]}" if not e["success"] and e.get("fail_reason") else ""
    print(f"    [{flag}] [{e['event_label']}] {e['from_status'] or '创'}->{e['to_status'] or '删'}{batch}{fields}{fail}")

# 8. 巡查员视角（看不到批次细节）
print(f"\n=== 8. 巡查员视角（本工单）===")
t2 = inspector.get(f"{BASE}/orders/{order_id}/trace").json()
print(f"  can_see_batch_detail={t2['can_see_batch_detail']}")
for e in t2["events"]:
    flag = "OK" if e["success"] else "FAIL"
    batch = f" [批次{e['batch_no']}]" if e.get("batch_no") and t2["can_see_batch_detail"] else ""
    fail = f" 失败={e['fail_reason'][:40]}" if not e["success"] and e.get("fail_reason") and t2["can_see_batch_detail"] else ""
    fields = f" 变更={','.join(e['changed_fields'][:5])}" if e.get("changed_fields") and (not e["is_batch_operation"] or t2["can_see_batch_detail"]) else ""
    print(f"    [{flag}] [{e['event_label']}] {e['from_status'] or '创'}->{e['to_status'] or '删'}{batch}{fields}{fail}")

# 9. 导出
print(f"\n=== 9. 导出 JSON/CSV ===")
rj = admin.get(f"{BASE}/orders/{order_id}/trace/export/json").json()
print(f"  JSON events={len(rj.get('events', []))}")
rc = admin.get(f"{BASE}/orders/{order_id}/trace/export/csv")
lines = rc.content.decode("utf-8-sig").strip().split("\n")
print(f"  CSV lines={len(lines)}")

print("\nDONE - 快照+批次追溯验证完成")
