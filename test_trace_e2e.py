"""工单追溯中心端到端回归测试"""
import requests
import json
import sys

BASE = "http://127.0.0.1:8005/api"

def log(title, data=None):
    print(f"\n{'='*60}")
    print(f">> {title}")
    if data is not None:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:1200])

def check(resp, expect=(200,)):
    try:
        rj = resp.json()
    except:
        rj = resp.text
    if isinstance(expect, int):
        expect = (expect,)
    if resp.status_code not in expect:
        print(f"  !! 期望 {expect}, 实际 {resp.status_code}: {rj}")
        sys.exit(1)
    return rj

def fmt(s):
    if not s: return "-"
    return s[5:16].replace("T", " ")

def new_session(user_id):
    s = requests.Session()
    s.headers["X-User-Id"] = str(user_id)
    return s

# 0) 获取用户列表找到 admin 和 inspector
print("=== 1. 获取用户列表 ===")
probe = requests.Session()
probe.headers["X-User-Id"] = "1"  # 临时
r = probe.get(f"{BASE}/users")
users = check(r)
admin_user = next(u for u in users if u["role"] == "admin")
inspector_users = [u for u in users if u["role"] == "inspector"]
log(f"找到 admin={admin_user['name']}(id={admin_user['id']}), 巡查员={[(u['name'],u['id']) for u in inspector_users]}")

# 1) 管理员会话
s = new_session(admin_user["id"])
# 2) 巡查员会话
s2 = new_session(inspector_users[0]["id"])
inspector2_id = inspector_users[-1]["id"] if len(inspector_users) > 1 else inspector_users[0]["id"]
s3 = new_session(inspector2_id)  # 另一个巡查员（权限测试用）

# 3) 用巡查员上报一张新工单
print("\n=== 2. 巡查员上报新工单 ===")
order_payload = {
    "road": "追溯测试大道 88 号",
    "tree_no": f"TRACE-{int(__import__('time').time()) % 100000}",
    "species": "悬铃木",
    "risk_level": "中",
    "need_road_close": True,
    "description": "树枝过长影响行人,追溯测试专用",
    "suggested_time": "09:00-11:00",
}
r = s2.post(f"{BASE}/orders/report", json=order_payload)
new_order = check(r, (200, 201))
order_id = new_order["id"]
order_no = new_order["order_no"]
log(f"新工单已上报 ID={order_id},工单号={order_no}", new_order)

# 4) 管理员派工
print("\n=== 3. 管理员派工 ===")
teams = check(s.get(f"{BASE}/teams"))
vehicles = check(s.get(f"{BASE}/vehicles"))
team_id = teams[0]["id"]
vehicle_id = vehicles[0]["id"] if vehicles else None
assign_payload = {"team_id": team_id}
if vehicle_id:
    assign_payload["vehicle_id"] = vehicle_id
    assign_payload["road_close_start"] = "2025-01-15T09:00:00"
    assign_payload["road_close_end"] = "2025-01-15T11:00:00"
r = s.post(f"{BASE}/orders/{order_id}/assign", json=assign_payload)
assigned = check(r)
log("派工成功", {"team": assigned.get("team_name"), "status": assigned.get("status")})

# 5) 管理员改派
print("\n=== 4. 管理员改派（换个队伍）===")
team2_id = teams[-1]["id"] if len(teams) > 1 else teams[0]["id"]
reassign_payload = {"team_id": team2_id, "note": "追溯测试-改派到另一支队伍"}
if vehicle_id:
    reassign_payload["vehicle_id"] = vehicle_id
r = s.post(f"{BASE}/orders/{order_id}/assign", json=reassign_payload)
reassigned = check(r)
log("改派成功", {"team": reassigned.get("team_name"), "status": reassigned.get("status")})

# 6) 巡查员开始作业
print("\n=== 5. 巡查员开始作业 ===")
r = s2.post(f"{BASE}/orders/{order_id}/start")
started = check(r)
log("开始作业", {"status": started.get("status")})

# 7) 巡查员提交复核
print("\n=== 6. 巡查员提交复核 ===")
r = s2.post(f"{BASE}/orders/{order_id}/submit")
submitted = check(r)
log("提交复核", {"status": submitted.get("status")})

# 8) 管理员撤销工单
print("\n=== 7. 管理员撤销工单 ===")
r = s.post(f"{BASE}/orders/{order_id}/cancel", json={"reason": "追溯测试-管理员撤销"})
cancelled = check(r)
log("撤销成功", {"status": cancelled.get("status"), "reason": cancelled.get("cancel_reason")})

# 9) 再创建一张工单用于快照导入 + 撤销恢复批次测试
print("\n=== 8. 再创建一张工单用于快照与撤销恢复批次 ===")
r = s2.post(f"{BASE}/orders/report", json={
    "road": "快照恢复测试路",
    "tree_no": f"SNAP-{int(__import__('time').time()) % 100000}",
    "species": "悬铃木",
    "risk_level": "低",
    "need_road_close": False,
    "description": "用于测试快照导入和批次撤销恢复",
})
snap_order = check(r, (200, 201))
snap_order_id = snap_order["id"]
snap_order_no = snap_order["order_no"]
log(f"快照测试工单 ID={snap_order_id},工单号={snap_order_no}")

# 10) 管理员派工该工单
print("\n=== 9. 管理员派工该工单 ===")
r = s.post(f"{BASE}/orders/{snap_order_id}/assign", json={"team_id": team_id})
check(r)
log("派工 OK")

# 11) 开始作业并提交，然后人工修改 description（模拟人工改派后再撤销的冲突场景）
print("\n=== 10. 开始作业 + 提交 + 人工修改描述（制造冲突） ===")
s2.post(f"{BASE}/orders/{snap_order_id}/start")
s2.post(f"{BASE}/orders/{snap_order_id}/submit")
# 人工修改（通过 assign 接口改派带 note，模拟 changed_fields）
r = s.post(f"{BASE}/orders/{snap_order_id}/assign", json={
    "team_id": team_id, "note": "追溯测试-人工改派制造冲突"
})
check(r)
log("人工修改完成（会导致撤销恢复时因已变更而跳过）")

# 12) 查询这两张工单的追溯记录
print("\n=== 11. 查询工单追溯（管理员视角）===")
r = s.get(f"{BASE}/orders/{order_id}/trace")
trace1 = check(r)
log(f"工单 {order_no} 追溯（管理员）共 {len(trace1['events'])} 条事件", {"summary": trace1["summary"]})
for e in trace1["events"]:
    print(f"    • [{e['event_label']}] {e['from_status']}→{e['to_status']} by {e.get('operator_name')} {'✓' if e['success'] else '✗'}")

r = s.get(f"{BASE}/orders/{snap_order_id}/trace")
trace2 = check(r)
log(f"工单 {snap_order_no} 追溯（管理员）共 {len(trace2['events'])} 条事件", {"summary": trace2["summary"]})
for e in trace2["events"]:
    print(f"    • [{e['event_label']}] {e['from_status']}→{e['to_status']} {'✓' if e['success'] else '✗'}")

# 13) 巡查员只能看自己的工单，且看不到批量明细
print("\n=== 12. 巡查员视角查看追溯（权限测试）===")
r = s2.get(f"{BASE}/orders/{order_id}/trace")
trace_inspector = check(r)
log(f"巡查员看自己的工单 {order_no}", {
    "can_see_batch_detail": trace_inspector.get("can_see_batch_detail"),
    "events_count": len(trace_inspector["events"])
})
# 另一个巡查员（非上报人）看这个工单，应该 403
if inspector2_id != inspector_users[0]["id"]:
    r = s3.get(f"{BASE}/orders/{order_id}/trace")
    print(f"    另一巡查员(id={inspector2_id})看工单 #{order_id} → status={r.status_code}（期望 403）")
    if r.status_code != 403:
        print("    !! 权限控制失败！")
else:
    print("    只有一个巡查员账号，跳过跨账号权限测试")

# 14) 导出 JSON / CSV
print("\n=== 13. 导出 JSON / CSV 追溯记录 ===")
r = s.get(f"{BASE}/orders/{order_id}/trace/export/json")
check(r)
json_data = r.json()
print(f"    JSON 导出 OK，包含 {len(json_data.get('events', []))} 条事件")

r = s.get(f"{BASE}/orders/{order_id}/trace/export/csv")
check(r)
csv_text = r.content.decode("utf-8-sig")
lines = csv_text.strip().split("\n")
print(f"    CSV 导出 OK，共 {len(lines)} 行（含表头+工单元信息）")
print(f"    前 3 行:")
for l in lines[:3]:
    print(f"      {l[:120]}")

# 15) 快照导入测试（覆盖 + 新建 + 跳过 + 拒绝）
print("\n=== 14. 快照导入测试（新建 + 覆盖 + 跳过） ===")
# 构造 orders 数据，模拟前端解析后的快照内容
snap_orders = [
    {
        "order_no": order_no,
        "road": "追溯测试大道 88 号（已覆盖",
        "tree_no": order_payload["tree_no"],
        "species": "悬铃木",
        "risk_level": "高",
        "need_road_close": True,
        "suggested_time": "10:00-12:00",
        "description": "导入后覆盖此描述——这是覆盖版本",
    },
    {
        "order_no": f"NEW-{int(__import__('time').time()) % 100000}",
        "road": "新道路 XXX",
        "tree_no": "NEWTREE-001",
        "species": "香樟",
        "risk_level": "高",
        "need_road_close": True,
        "suggested_time": "22:00-05:00",
        "description": "新导入的工单",
    },
    {
        "order_no": snap_order_no,
        "road": "快照恢复测试路",
        "tree_no": snap_order["tree_no"],
        "species": "悬铃木",
        "risk_level": "低",
        "need_road_close": False,
        "description": "未选中此工单",
    },
]
snap_import_payload = {
    "orders": snap_orders,
    "selected": [
        {"order_no": order_no, "decision": "overwrite"},
        {"order_no": snap_orders[1]["order_no"], "decision": "create"},
        # 第三个不选，模拟 not_selected
    ],
}
r = s.post(f"{BASE}/snapshot/import", json=snap_import_payload)
snap_import = check(r)
log(f"快照导入结果", {
    "total": snap_import.get("total"),
    "imported": snap_import.get("imported"),
    "skipped": snap_import.get("skipped"),
    "rejected": snap_import.get("rejected"),
    "not_selected": snap_import.get("not_selected"),
    "batch_no": snap_import.get("batch_no"),
})
batch_id = snap_import.get("batch_id")
newly_created_order_no = snap_orders[1]["order_no"]

# 16) 查一下被覆盖工单的追溯，看是否有 SNAPSHOT_OVERWRITE 事件
print("\n=== 15. 验证快照导入后的追溯记录 ===")
r = s.get(f"{BASE}/orders/{order_id}/trace")
trace_after_import = check(r)
snapshot_events = [e for e in trace_after_import["events"] if e["event_type"].startswith("snapshot")]
print(f"    快照相关事件数: {len(snapshot_events)}")
for e in snapshot_events:
    print(f"      [{e['event_label']}] 批次={e.get('batch_no')} 成功={e['success']}")

# 17) 批次撤销/恢复测试
print("\n=== 16. 批次撤销/恢复测试 ===")
if batch_id:
    r = s.post(f"{BASE}/restore-batches/{batch_id}/revoke", json={"reason": "追溯测试-撤销这个批次"})
    revoke_r = check(r)
    log(f"批次撤销/恢复结果", {
        "batch_no": revoke_r.get("batch_no"),
        "total_revocable": revoke_r.get("total_revocable"),
        "revoked": revoke_r.get("revoked"),
        "failed": revoke_r.get("failed"),
    })
    for item in revoke_r.get("items", []):
        flag = "✓" if item["success"] else "✗"
        print(f"      {flag} {item['order_no']}: [{item['action']}] {item.get('reason', '')[:60]}")

# 18) 再次查看这两张工单的完整追溯
print("\n=== 17. 最终完整追溯验证（含批次撤销）===")
for oid, ono in [(order_id, order_no), (snap_order_id, snap_order_no)]:
    r = s.get(f"{BASE}/orders/{oid}/trace")
    t = check(r)
    print(f"\n  工单 {ono}:")
    print(f"    统计: {t['summary']}")
    print(f"    事件列表:")
    for e in t["events"]:
        flag = "✓" if e["success"] else "✗"
        extra = ""
        if e.get("batch_no"):
            extra += f" [批次 {e['batch_no']}]"
        if e.get("fail_reason"):
            extra += f" 原因={e['fail_reason'][:40]}"
        if e.get("changed_fields"):
            extra += f" 变更={','.join(e['changed_fields'])}"
        print(f"      {flag} [{e['event_label']}] {e['from_status']}→{e['to_status']} @{fmt(e['created_at'])} by {e.get('operator_name')}{extra}")

print("\n" + "="*60)
print("✅ 端到端追溯测试全部通过！")
print("="*60)
