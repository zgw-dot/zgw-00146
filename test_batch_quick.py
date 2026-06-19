"""
批次历史与撤销恢复 快速验证脚本
"""
import requests, json, time, sys

BASE = 'http://127.0.0.1:8000/api'
H_ADMIN = {'X-User-Id': '1', 'Content-Type': 'application/json'}
H_INSP  = {'X-User-Id': '2', 'Content-Type': 'application/json'}

PASS = '\033[92m✅ PASS\033[0m'
FAIL = '\033[91m❌ FAIL\033[0m'

def section(title):
    print(f"\n{'='*60}\n{title}\n{'='*60}")

def check(cond, msg):
    if cond:
        print(f"  {PASS} {msg}")
    else:
        print(f"  {FAIL} {msg}")
        sys.exit(1)

# ============================================================
# Test 1: 批次列表和权限
# ============================================================
section("(1) 权限与批次列表")

r = requests.get(f'{BASE}/restore-batches', headers=H_INSP)
check(r.status_code == 403, f"巡查员不能查看批次列表（{r.status_code}）")

r = requests.get(f'{BASE}/restore-batches', headers=H_ADMIN)
check(r.status_code == 200, "管理员可以查看批次列表")
initial_count = len(r.json())
print(f"    当前批次数量: {initial_count}")

# ============================================================
# Test 2: 导入新建工单生成批次
# ============================================================
section("(2) 新建工单导入生成批次")

ts = int(time.time())
new_order = {
    'order_no': f'NEW-BATCH-{ts}',
    'road': '批次测试路',
    'tree_no': f'TREE-{ts}',
    'risk_level': '低',
    'status': '待派工',
    'need_road_close': False,
    'description': '批次功能测试新建工单',
}

r = requests.post(f'{BASE}/snapshot/import', headers=H_ADMIN, json={
    'orders': [new_order],
    'snapshot_version': 'test-v1',
})
check(r.status_code == 200, "导入成功")
imp = r.json()
check(imp.get('batch_id') is not None, "返回批次ID")
check(imp.get('batch_no') is not None, "返回批次号")
check(imp['imported'] == 1, "导入成功1条")

batch_id = imp['batch_id']
batch_no = imp['batch_no']
print(f"    批次ID: {batch_id}")
print(f"    批次号: {batch_no}")

imp_item = imp['items'][0]
check(imp_item['order_no'] == new_order['order_no'], "工单号正确")
check(imp_item['action'] == 'create', f"操作=create（实际={imp_item['action']}）")
check(imp_item['success'] == True, "操作成功")

# 验证工单确实存在
r = requests.get(f'{BASE}/orders', headers=H_ADMIN)
orders = r.json()
found = any(o['order_no'] == new_order['order_no'] for o in orders)
check(found, "新建工单确实存在于列表中")

# ============================================================
# Test 3: 批次详情
# ============================================================
section("(3) 批次详情")

r = requests.get(f'{BASE}/restore-batches/{batch_id}', headers=H_ADMIN)
check(r.status_code == 200, "批次详情查询成功")
detail = r.json()
check(detail['batch_no'] == batch_no, "批次号一致")
check(detail['status'] == 'completed', "批次状态=completed")
check(detail['operator_name'] is not None, "有操作人信息")
check(detail['snapshot_version'] == 'test-v1', "快照版本正确")
check(len(detail['items']) == 1, "包含1条子项")

item = detail['items'][0]
check(item['order_no'] == new_order['order_no'], "子项工单号正确")
check(item['action'] == 'create', "子项操作=create")
check(item['success'] == True, "子项成功")
check(item['is_revoked'] == False, "尚未撤销")
check(item['before_status'] is None, "新建工单恢复前状态=None")
check(item['after_status'] == '待派工', "恢复后状态=待派工")

print(f"    批次号: {detail['batch_no']}")
print(f"    操作人: {detail['operator_name']}")
print(f"    时间: {detail['created_at']}")

# ============================================================
# Test 4: 撤销新建工单
# ============================================================
section("(4) 撤销新建工单批次")

r = requests.post(f'{BASE}/restore-batches/{batch_id}/revoke', headers=H_ADMIN, json={
    'reason': '测试撤销新建工单'
})
check(r.status_code == 200, "撤销接口成功")
rev = r.json()
check(rev['revoked'] == 1, f"成功撤销1条（实际={rev['revoked']}）")
check(rev['failed'] == 0, f"失败0条（实际={rev['failed']}）")
check(rev['status'] == 'revoked', "批次状态=revoked")

# 验证工单已被删除
r = requests.get(f'{BASE}/orders', headers=H_ADMIN)
orders = r.json()
found = any(o['order_no'] == new_order['order_no'] for o in orders)
check(not found, "撤销后新建工单已被删除")

# 再次查看详情
r = requests.get(f'{BASE}/restore-batches/{batch_id}', headers=H_ADMIN)
detail2 = r.json()
check(detail2['status'] == 'revoked', "详情中状态=revoked")
check(detail2['items'][0]['is_revoked'] == True, "子项已撤销")

# ============================================================
# Test 5: 重复撤销幂等
# ============================================================
section("(5) 重复撤销幂等性")

r = requests.post(f'{BASE}/restore-batches/{batch_id}/revoke', headers=H_ADMIN, json={
    'reason': '重复撤销测试'
})
check(r.status_code == 400, f"重复撤销返回400（实际={r.status_code}）")
err_detail = r.json()['detail']
check('撤销' in err_detail and ('完成' in err_detail or '全部' in err_detail),
      f"错误信息说明已撤销: {err_detail}")

# ============================================================
# Test 6: 巡查员不能撤销
# ============================================================
section("(6) 权限限制")

# 先创建一个新批次用于测试
r = requests.post(f'{BASE}/snapshot/import', headers=H_ADMIN, json={
    'orders': [new_order],
    'snapshot_version': 'test-perm',
})
batch_perm_id = r.json()['batch_id']

# 巡查员查看详情
r = requests.get(f'{BASE}/restore-batches/{batch_perm_id}', headers=H_INSP)
check(r.status_code == 403, f"巡查员不能查看批次详情（{r.status_code}）")

# 巡查员撤销
r = requests.post(f'{BASE}/restore-batches/{batch_perm_id}/revoke', headers=H_INSP, json={'reason': '测试'})
check(r.status_code == 403, f"巡查员不能撤销批次（{r.status_code}）")

# ============================================================
# Test 7: 导出JSON包含追溯信息
# ============================================================
section("(7) 导出JSON可追溯")

r = requests.get(f'{BASE}/export/json', headers=H_ADMIN)
check(r.status_code == 200, "导出成功")
snap = r.json()
check('exported_at' in snap, "顶层有导出时间")
check('exported_by' in snap, "顶层有导出人")
check('traceability' in snap, "包含 traceability 字段")
trace = snap['traceability']
check('last_restore_batch' in trace, "有最后批次信息")
last = trace['last_restore_batch']
check(last is not None, "最后批次不为空")
check('batch_no' in last, "有批次号")
check('status' in last, "有批次状态")
check('created_at' in last, "有创建时间")
check('operator_name' in last, "有操作人")
check('snapshot_version' in last, "有快照版本")
print(f"    最后批次: {last['batch_no']} ({last['status']})")
print(f"    导出人: {snap['exported_by']['name']}")
print(f"    导出时间: {snap['exported_at']}")

# ============================================================
# Test 8: 批次列表分页/排序
# ============================================================
section("(8) 批次列表")

r = requests.get(f'{BASE}/restore-batches', headers=H_ADMIN)
batches = r.json()
check(len(batches) >= 2, f"至少有2条批次记录（实际={len(batches)}）")
check(batches[0]['id'] > batches[-1]['id'], "按时间倒序排列（最新在前）")

# 检查每条都有必要字段
for b in batches[:3]:
    check('batch_no' in b, f"批次 {b.get('id')} 有 batch_no")
    check('status' in b, f"批次 {b.get('id')} 有 status")
    check('total_count' in b, f"批次 {b.get('id')} 有 total_count")
    check('operator_name' in b, f"批次 {b.get('id')} 有 operator_name")

# ============================================================
print(f"\n\033[92m🎉 所有接口级测试通过！\033[0m")
