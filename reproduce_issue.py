"""
简化的复现脚本
"""
import requests, json, time

BASE = 'http://127.0.0.1:8000/api'
H_ADMIN = {'X-User-Id': '1', 'Content-Type': 'application/json'}

def section(title):
    print(f"\n{'='*60}\n{title}\n{'='*60}")

# 找一个状态=已派工的工单
section("Step 0: 找一个已派工的工单")
r = requests.get(f'{BASE}/orders', headers=H_ADMIN)
orders = r.json()

# 找一个已派工的
assigned = None
for o in orders:
    if o.get('status') == '已派工' and o.get('team_id') and o.get('vehicle_id'):
        assigned = o
        break

if not assigned:
    print("找不到已派工的工单，退出")
    exit(1)

order_id = assigned['id']
order_no = assigned['order_no']
print(f"选中工单: {order_no}")
print(f"当前状态: {assigned['status']}")
print(f"team_id: {assigned.get('team_id')}")
print(f"vehicle_id: {assigned.get('vehicle_id')}")
print(f"road_close_start: {assigned.get('road_close_start')}")
print(f"road_close_end: {assigned.get('road_close_end')}")

# Step 1: 导出快照
section("Step 1: 导出快照")
r = requests.get(f'{BASE}/export/json', headers=H_ADMIN)
snap = r.json()
snap_order = next(o for o in snap['orders'] if o['order_no'] == order_no)
print(f"快照中状态: {snap_order['status']}")
print(f"快照 team: {snap_order.get('team')}")
print(f"快照 vehicle: {snap_order.get('vehicle')}")
print(f"快照 need_road_close: {snap_order.get('need_road_close')}")

# Step 2: 推进工单到作业中
section("Step 2: 推进工单到作业中")
r = requests.post(f'{BASE}/orders/{order_id}/start', headers=H_ADMIN)
if r.status_code == 200:
    print(f"推进后状态: {r.json()['status']}")
else:
    print(f"推进失败: {r.status_code} - {r.json().get('detail', '')}")
    # 如果已经是作业中，继续
    r = requests.get(f'{BASE}/orders/{order_id}', headers=H_ADMIN)
    print(f"当前状态: {r.json()['status']}")

# Step 3: 覆盖恢复
section("Step 3: 覆盖恢复（回到已派工）")
r = requests.post(f'{BASE}/snapshot/import', headers=H_ADMIN, json={
    'orders': [snap_order],
    'snapshot_version': snap['snapshot_version'],
})
imp_result = r.json()
batch_id = imp_result['batch_id']
batch_no = imp_result['batch_no']
imp_item = imp_result['items'][0]
print(f"批次ID: {batch_id}")
print(f"操作: {imp_item['action']}")
print(f"成功: {imp_item['success']}")
print(f"原因: {imp_item['reason']}")

# 查看恢复后的工单
r = requests.get(f'{BASE}/orders/{order_id}', headers=H_ADMIN)
order_after = r.json()
print(f"恢复后状态: {order_after['status']}")
print(f"恢复后 team_id: {order_after.get('team_id')}")
print(f"恢复后 vehicle_id: {order_after.get('vehicle_id')}")

# Step 4: 查看批次详情的 after_snapshot
section("Step 4: 查看批次详情的 after_snapshot")
r = requests.get(f'{BASE}/restore-batches/{batch_id}', headers=H_ADMIN)
detail = r.json()
item = detail['items'][0]

print(f"\nafter_snapshot_json 内容:")
after_snap = json.loads(item['after_snapshot_json'])
for k, v in sorted(after_snap.items()):
    print(f"  {k}: {v!r}")

# 用后端的 _serialize_order_snapshot 方式来序列化当前工单（模拟撤销时的比较）
print(f"\n当前工单字段（用于比较）:")
r = requests.get(f'{BASE}/orders/{order_id}', headers=H_ADMIN)
cur = r.json()
# 手动模拟 _serialize_order_snapshot
cur_snap = {
    "id": cur['id'],
    "order_no": cur['order_no'],
    "road": cur['road'],
    "tree_no": cur['tree_no'],
    "risk_level": cur['risk_level'],
    "suggested_time": cur.get('suggested_time') or "",
    "need_road_close": cur['need_road_close'],
    "description": cur.get('description') or "",
    "status": cur['status'],
    "team_id": cur.get('team_id'),
    "vehicle_id": cur.get('vehicle_id'),
    "road_close_start": cur.get('road_close_start') or "",
    "road_close_end": cur.get('road_close_end') or "",
    "reported_at": cur.get('reported_at') or "",
    "assigned_at": cur.get('assigned_at') or "",
    "started_at": cur.get('started_at') or "",
    "submitted_at": cur.get('submitted_at') or "",
    "reviewed_at": cur.get('reviewed_at') or "",
    "review_note": cur.get('review_note') or "",
    "cancelled_at": cur.get('cancelled_at') or "",
    "cancel_reason": cur.get('cancel_reason') or "",
    "reporter_name": cur.get('reporter_name') or "",
    "reporter_phone": cur.get('reporter_phone') or "",
}

print(f"\n差异字段比较:")
ignore = {"id", "order_no"}
changed = []
for key in cur_snap:
    if key in ignore:
        continue
    cv = cur_snap.get(key)
    sv = after_snap.get(key)
    if cv != sv:
        changed.append(key)
        print(f"  ❌ {key}: 快照={sv!r} vs 当前={cv!r}")

if changed:
    print(f"\n❌ 发现 {len(changed)} 个差异字段: {changed}")
else:
    print(f"\n✅ 没有差异字段")

# Step 5: 尝试撤销
section("Step 5: 立即撤销")
r = requests.post(f'{BASE}/restore-batches/{batch_id}/revoke', headers=H_ADMIN, json={
    'reason': '复现测试-立即撤销'
})
print(f"撤销状态码: {r.status_code}")
result = r.json()
print(f"撤销结果: {json.dumps(result, ensure_ascii=False, indent=2)}")

# 查看最终批次详情
r = requests.get(f'{BASE}/restore-batches/{batch_id}', headers=H_ADMIN)
final_detail = r.json()
final_item = final_detail['items'][0]
print(f"\n最终批次详情:")
print(f"  批次状态: {final_detail['status']}")
print(f"  子项已撤销: {final_item['is_revoked']}")
print(f"  失败原因: {final_item.get('revoke_failed_reason', '')}")
