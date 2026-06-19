"""
持久化验证脚本 - 重启后验证批次数据完整性
"""
import requests, json, sys

BASE = 'http://127.0.0.1:8000/api'
H = {'X-User-Id': '1', 'Content-Type': 'application/json'}

PASS = '\033[92m✅\033[0m'
FAIL = '\033[91m❌\033[0m'

def check(cond, msg):
    if cond:
        print(f"  {PASS} {msg}")
    else:
        print(f"  {FAIL} {msg}")
        sys.exit(1)

print("=" * 60)
print("持久化验证")
print("=" * 60)

# 1. 批次列表
r = requests.get(f'{BASE}/restore-batches', headers=H)
batches = r.json()
print(f"\n1. 批次列表: {len(batches)} 条")

check(len(batches) >= 6, "至少6条批次记录")

# 2. 统计状态分布
status_counts = {}
for b in batches:
    s = b['status']
    status_counts[s] = status_counts.get(s, 0) + 1

print(f"\n2. 状态分布: {status_counts}")
check(status_counts.get('revoked', 0) >= 3, "至少3条已撤销批次")
check(status_counts.get('completed', 0) >= 1, "至少1条已完成批次")

# 3. 已撤销批次详情
revoked = [b for b in batches if b['status'] == 'revoked']
batch_id = revoked[0]['id']
batch_no = revoked[0]['batch_no']

r = requests.get(f'{BASE}/restore-batches/{batch_id}', headers=H)
detail = r.json()

print(f"\n3. 已撤销批次详情 ({batch_no})")
check(detail['status'] == 'revoked', "状态=revoked")
check(detail.get('revoked_at') is not None, "有撤销时间")
check(detail.get('revoked_by_name') is not None, "有撤销人")
check(detail.get('revoke_reason') is not None, "有撤销原因")
check(len(detail['items']) >= 1, "至少1条子项")

item = detail['items'][0]
check(item['is_revoked'] == True, "子项已撤销")
check(item.get('revoked_at') is not None, "子项有撤销时间")

# 4. 已完成批次详情
completed = [b for b in batches if b['status'] == 'completed']
comp_id = completed[0]['id']
comp_no = completed[0]['batch_no']

r = requests.get(f'{BASE}/restore-batches/{comp_id}', headers=H)
comp_detail = r.json()

print(f"\n4. 已完成批次详情 ({comp_no})")
check(comp_detail['status'] == 'completed', "状态=completed")
check(comp_detail.get('revoked_at') is None, "没有撤销时间")
check(comp_detail.get('revoke_reason') is None, "没有撤销原因")

comp_item = comp_detail['items'][0]
check(comp_item['is_revoked'] == False, "子项未撤销")

# 5. 导出追溯信息
r = requests.get(f'{BASE}/export/json', headers=H)
snap = r.json()

print(f"\n5. 导出JSON追溯信息")
check('traceability' in snap, "包含 traceability 字段")
check('last_restore_batch' in snap['traceability'], "包含最后批次信息")
last = snap['traceability']['last_restore_batch']
check(last is not None, "最后批次不为空")
check('batch_no' in last, "有批次号")
check('status' in last, "有批次状态")

# 6. 导出再导入测试
print(f"\n6. 导出再导入验证")
orders_for_import = snap['orders'][:1]  # 取第一条
r2 = requests.post(f'{BASE}/snapshot/import', headers=H, json={
    'orders': orders_for_import,
    'snapshot_version': snap['snapshot_version'],
    'exported_at': snap.get('exported_at'),
})
check(r2.status_code == 200, "导入成功")
imp_result = r2.json()
check(imp_result.get('batch_id') is not None, "生成了新批次")
check(imp_result.get('batch_no') is not None, "有批次号")

print(f"    新批次: {imp_result['batch_no']}")

# 7. 新批次详情可查
new_batch_id = imp_result['batch_id']
r3 = requests.get(f'{BASE}/restore-batches/{new_batch_id}', headers=H)
check(r3.status_code == 200, "新批次详情可查")
new_detail = r3.json()
check(new_detail['status'] == 'completed', "新批次状态=completed")

print()
print(f"\033[92m🎉 所有持久化验证通过！\033[0m")
print(f"   批次记录、撤销状态、导出追溯、导出再导入全部持久化正确")
