"""
先做一次覆盖恢复，确保有测试数据
"""
import requests, json

BASE = 'http://127.0.0.1:8000/api'
H_ADMIN = {'X-User-Id': '1', 'Content-Type': 'application/json'}

# 找一个已派工的工单
r = requests.get(f'{BASE}/orders', headers=H_ADMIN)
orders = r.json()

assigned = None
for o in orders:
    if o.get('status') == '已派工' and o.get('team_id') and o.get('vehicle_id'):
        assigned = o
        break

if not assigned:
    print("找不到已派工的工单")
    exit(1)

order_id = assigned['id']
order_no = assigned['order_no']
print(f"使用工单: {order_no}")
print(f"当前状态: {assigned['status']}")

# 推进到作业中
r = requests.post(f'{BASE}/orders/{order_id}/start', headers=H_ADMIN)
if r.status_code == 200:
    print(f"推进到: {r.json()['status']}")
else:
    r = requests.get(f'{BASE}/orders/{order_id}', headers=H_ADMIN)
    print(f"当前状态: {r.json()['status']}")

# 导出快照
r = requests.get(f'{BASE}/export/json', headers=H_ADMIN)
snap = r.json()
snap_order = next(o for o in snap['orders'] if o['order_no'] == order_no)
print(f"快照中状态: {snap_order['status']}")

# 覆盖恢复
r = requests.post(f'{BASE}/snapshot/import', headers=H_ADMIN, json={
    'orders': [snap_order],
    'snapshot_version': snap['snapshot_version'],
})
result = r.json()
print(f"批次ID: {result['batch_id']}")
print(f"操作: {result['items'][0]['action']}")
print(f"成功: {result['items'][0]['success']}")

# 验证恢复后的状态
r = requests.get(f'{BASE}/orders/{order_id}', headers=H_ADMIN)
print(f"恢复后状态: {r.json()['status']}")
print("\n准备工作完成，现在运行 debug_reproduce.py 查看根因")
