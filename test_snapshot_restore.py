"""
工单快照恢复 回归测试脚本
==========================
验证内容：
  1. 导出包含版本号、导出时间、操作人
  2. 预检冲突分析正确
  3. 正式导入：新建、覆盖、跳过、拒绝
  4. 重复导入不产生脏数据
  5. 普通巡查员无法恢复（403）
  6. 服务重启后导出内容一致
  7. 操作日志可查

用法：
  python test_snapshot_restore.py
  python test_snapshot_restore.py --phase2   # 重启后运行
"""
import requests, json, sys, os, hashlib, time
from datetime import datetime

BASE = 'http://127.0.0.1:8000/api'
H_ADMIN = {'X-User-Id': '1', 'Content-Type': 'application/json'}
H_INSP  = {'X-User-Id': '2', 'Content-Type': 'application/json'}

STATE_FILE = 'd:/workSpace/AI__SPACE/02-label/zgw-00146/test_snap_state.json'
HASH_FILE  = 'd:/workSpace/AI__SPACE/02-label/zgw-00146/test_snap_hash.txt'

PASS = '\033[92m✅ PASS\033[0m'
FAIL = '\033[91m❌ FAIL\033[0m'
INFO = '\033[94mℹ️\033[0m'

def section(title):
    print(f"\n{'='*60}\n{title}\n{'='*60}")

def check(cond, msg):
    if cond:
        print(f"  {PASS} {msg}")
    else:
        print(f"  {FAIL} {msg}")
        sys.exit(1)

def save_state(**kwargs):
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
    state.update(kwargs)
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def export_json():
    r = requests.get(f'{BASE}/export/json', headers=H_ADMIN)
    check(r.status_code == 200, f"JSON 导出成功: {r.status_code}")
    return r.json()

def compute_hash(data):
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


# ============================================================
# Phase 1: 主测试流程
# ============================================================
def phase1():
    section("(0) 环境检查")
    r = requests.get(f'{BASE}/health')
    check(r.status_code == 200, "服务正常运行")

    section("(1) 导出快照 - 验证元数据")
    snap = export_json()
    check('snapshot_version' in snap, "导出包含 snapshot_version")
    check(snap['snapshot_version'] == '1.0.0', f"版本号 = {snap['snapshot_version']}")
    check('exported_at' in snap and snap['exported_at'], "导出包含 exported_at")
    check('exported_by' in snap and snap['exported_by'], "导出包含 exported_by")
    check(snap['exported_by']['role'] == 'admin', "导出操作人角色 = admin")
    check(snap['exported_by']['id'] == 1, "导出操作人 ID = 1")
    print(f"    版本: {snap['snapshot_version']}")
    print(f"    时间: {snap['exported_at']}")
    print(f"    操作人: {snap['exported_by']}")

    section("(2) 准备测试数据 - 创建几个工单")
    r = requests.post(f'{BASE}/orders/report', headers=H_INSP, json={
        'road': '快照测试路A', 'tree_no': f'SNAP-A-{int(time.time())}',
        'risk_level': '中', 'need_road_close': True,
        'description': '快照恢复测试工单A'
    })
    check(r.status_code == 201, f"创建测试工单A: {r.status_code}")
    order_a = r.json()
    print(f"    工单A: ID={order_a['id']} NO={order_a['order_no']} 状态={order_a['status']}")

    r = requests.post(f'{BASE}/orders/report', headers=H_INSP, json={
        'road': '快照测试路B', 'tree_no': f'SNAP-B-{int(time.time())}',
        'risk_level': '高', 'need_road_close': False,
        'description': '快照恢复测试工单B'
    })
    check(r.status_code == 201, f"创建测试工单B: {r.status_code}")
    order_b = r.json()
    print(f"    工单B: ID={order_b['id']} NO={order_b['order_no']} 状态={order_b['status']}")

    save_state(order_a_no=order_a['order_no'], order_b_no=order_b['order_no'],
               order_a_id=order_a['id'], order_b_id=order_b['id'])

    section("(3) 导出快照作为恢复源")
    snap_before = export_json()
    snap_orders = snap_before['orders']
    snap_hash_before = compute_hash(snap_orders)
    print(f"    快照工单数: {len(snap_orders)}")
    print(f"    快照哈希: {snap_hash_before}")
    save_state(snap_hash_before=snap_hash_before, snap_before_orders=snap_orders,
               snap_before_version=snap_before['snapshot_version'])

    section("(4) 修改数据 - 派工工单A")
    r = requests.post(f'{BASE}/orders/{order_a["id"]}/assign', headers=H_ADMIN, json={
        'team_id': 1, 'vehicle_id': 1,
        'road_close_start': '2026-07-01T09:00:00Z',
        'road_close_end':   '2026-07-01T11:00:00Z',
    })
    check(r.status_code == 200, f"派工工单A成功: {r.status_code}")
    order_a_after = r.json()
    check(order_a_after['status'] == '已派工', f"工单A状态=已派工")

    section("(5) 预检 - 测试冲突分析")
    test_orders = [
        {
            'order_no': order_a['order_no'],
            'road': '快照测试路A', 'tree_no': 'SNAP-A-001',
            'risk_level': '中', 'status': '待派工',
            'need_road_close': '否', 'team': '', 'vehicle': '',
            'road_close_start': '', 'road_close_end': '',
            'description': '快照恢复测试工单A',
            'histories': [],
        },
        {
            'order_no': 'WO_FAKE_NEW_001',
            'road': '全新测试路', 'tree_no': 'NEW-001',
            'risk_level': '低', 'status': '待派工',
            'need_road_close': '否', 'team': '', 'vehicle': '',
            'road_close_start': '', 'road_close_end': '',
            'description': '全新工单预检',
            'histories': [],
        },
        {
            'order_no': 'WO_FAKE_COMPLETED_001',
            'road': '已完成测试路', 'tree_no': 'DONE-001',
            'risk_level': '低', 'status': '已完成',
            'need_road_close': '否', 'team': '', 'vehicle': '',
            'road_close_start': '', 'road_close_end': '',
            'description': '终态工单预检',
            'histories': [],
        },
    ]

    r = requests.post(f'{BASE}/snapshot/precheck', headers=H_ADMIN, json={'orders': test_orders})
    check(r.status_code == 200, f"预检接口成功: {r.status_code}")
    precheck = r.json()
    check(precheck['total'] == 3, f"预检总数=3")

    item_a = next(i for i in precheck['items'] if i['order_no'] == order_a['order_no'])
    check(item_a['decision'] == 'reject', f"工单A决策=拒绝（快照待派工→当前已派工，属于状态倒退）")
    has_dup_conflict = any(c['type'] == 'duplicate_order_no' for c in item_a['conflicts'])
    check(has_dup_conflict, "工单A存在重复工单号冲突")
    has_regression = any(c['type'] == 'status_regression' for c in item_a['conflicts'])
    check(has_regression, "工单A存在状态倒退冲突")

    item_new = next(i for i in precheck['items'] if i['order_no'] == 'WO_FAKE_NEW_001')
    check(item_new['decision'] == 'ok', f"新工单决策=ok")
    check(len(item_new['conflicts']) == 0, "新工单无冲突")

    item_done = next(i for i in precheck['items'] if i['order_no'] == 'WO_FAKE_COMPLETED_001')
    check(item_done['decision'] == 'skip', f"终态工单决策=skip")
    has_terminal = any(c['type'] == 'terminal_status_new' for c in item_done['conflicts'])
    check(has_terminal, "终态工单存在terminal_status_new冲突")

    section("(6) 权限检查 - 巡查员不能恢复")
    r = requests.post(f'{BASE}/snapshot/precheck', headers=H_INSP, json={'orders': test_orders})
    check(r.status_code == 403, f"巡查员预检返回403: {r.status_code}")

    r = requests.post(f'{BASE}/snapshot/import', headers=H_INSP, json={
        'orders': test_orders, 'selected': []
    })
    check(r.status_code == 403, f"巡查员导入返回403: {r.status_code}")

    section("(7) 正式导入 - 新建工单")
    new_order_data = [{
        'order_no': 'WO_IMPORT_NEW_001',
        'road': '导入测试路', 'tree_no': 'IMP-001',
        'risk_level': '低', 'status': '待派工',
        'need_road_close': '否', 'team': '', 'vehicle': '',
        'road_close_start': '', 'road_close_end': '',
        'reported_at': datetime.utcnow().isoformat(),
        'description': '导入新建测试',
        'histories': [{'from': '', 'to': '待派工', 'operator': '系统管理员', 'note': '导入', 'at': datetime.utcnow().isoformat()}],
    }]

    r = requests.post(f'{BASE}/snapshot/import', headers=H_ADMIN, json={
        'orders': new_order_data,
    })
    check(r.status_code == 200, f"导入新工单成功: {r.status_code}")
    result = r.json()
    check(result['imported'] == 1, f"导入成功数=1")
    check(result['failed'] == 0, f"失败数=0")
    item = result['items'][0]
    check(item['action'] == 'create', f"操作=create")
    check(item['success'] == True, f"成功=True")
    save_state(imported_new_no='WO_IMPORT_NEW_001')

    r = requests.get(f'{BASE}/orders', headers=H_ADMIN)
    orders_list = r.json()
    found = next((o for o in orders_list if o['order_no'] == 'WO_IMPORT_NEW_001'), None)
    check(found is not None, "新建工单在列表中存在")
    check(found['status'] == '待派工', "新建工单状态=待派工")

    section("(8) 重复导入 - 不产生脏数据")
    r = requests.post(f'{BASE}/snapshot/import', headers=H_ADMIN, json={
        'orders': new_order_data,
    })
    check(r.status_code == 200, f"重复导入成功: {r.status_code}")
    result2 = r.json()
    dup_item = result2['items'][0]
    check(dup_item['action'] in ('skip', 'overwrite', 'reject'), f"重复导入操作={dup_item['action']}（非create）")

    r = requests.get(f'{BASE}/orders', headers=H_ADMIN)
    orders_after_dup = r.json()
    count_dup = sum(1 for o in orders_after_dup if o['order_no'] == 'WO_IMPORT_NEW_001')
    check(count_dup == 1, f"重复导入后同工单号只有1条（实际={count_dup}）")

    section("(9) 覆盖导入 - 状态不可倒退")
    regression_data = [{
        'order_no': order_a['order_no'],
        'road': '快照测试路A', 'tree_no': 'SNAP-A-001',
        'risk_level': '中', 'status': '待派工',
        'need_road_close': '否', 'team': '', 'vehicle': '',
        'road_close_start': '', 'road_close_end': '',
        'reported_at': datetime.utcnow().isoformat(),
        'description': '状态倒退测试',
        'histories': [],
    }]
    r = requests.post(f'{BASE}/snapshot/import', headers=H_ADMIN, json={
        'orders': regression_data,
    })
    check(r.status_code == 200, f"状态倒退导入成功: {r.status_code}")
    result3 = r.json()
    item3 = result3['items'][0]
    check(item3['action'] == 'reject', f"状态倒退被拒绝: action={item3['action']}")
    check('状态倒退' in item3['reason'] or '落后' in item3['reason'] or '禁止' in item3['reason'] or '倒退' in item3['reason'],
          f"原因包含状态倒退说明: {item3['reason']}")

    order_a_check = requests.get(f'{BASE}/orders/{order_a["id"]}', headers=H_ADMIN).json()
    check(order_a_check['status'] == '已派工', "工单A状态未被倒退")

    section("(10) 终态记录导入 - 已完成/已撤销不写入")
    terminal_data = [{
        'order_no': 'WO_TERMINAL_DONE_001',
        'road': '终态路', 'tree_no': 'TERM-001',
        'risk_level': '低', 'status': '已完成',
        'need_road_close': '否', 'team': '', 'vehicle': '',
        'road_close_start': '', 'road_close_end': '',
        'description': '终态导入测试',
        'histories': [],
    }]
    r = requests.post(f'{BASE}/snapshot/import', headers=H_ADMIN, json={
        'orders': terminal_data,
    })
    check(r.status_code == 200, f"终态导入接口成功: {r.status_code}")
    result4 = r.json()
    check(result4['skipped'] >= 1, f"终态被跳过: skipped={result4['skipped']}")

    r = requests.get(f'{BASE}/orders', headers=H_ADMIN)
    term_found = next((o for o in r.json() if o['order_no'] == 'WO_TERMINAL_DONE_001'), None)
    check(term_found is None, "终态工单未写入数据库")

    section("(11) 车辆冲突导入 - 拒绝写入")
    conflict_data = [{
        'order_no': 'WO_CONFLICT_001',
        'road': '冲突路', 'tree_no': 'CON-001',
        'risk_level': '高', 'status': '已派工',
        'need_road_close': '是', 'team': '修剪一队', 'vehicle': '京A·12345',
        'road_close_start': '2026-07-01T09:00:00Z',
        'road_close_end':   '2026-07-01T11:00:00Z',
        'reported_at': datetime.utcnow().isoformat(),
        'description': '车辆冲突测试',
        'histories': [],
    }]
    r = requests.post(f'{BASE}/snapshot/import', headers=H_ADMIN, json={
        'orders': conflict_data,
    })
    check(r.status_code == 200, f"车辆冲突导入接口成功: {r.status_code}")
    result5 = r.json()
    item5 = result5['items'][0]
    check(item5['action'] == 'reject', f"车辆冲突被拒绝: action={item5['action']}")
    check('冲突' in item5['reason'] or '占用' in item5['reason'],
          f"原因包含冲突说明: {item5['reason']}")

    section("(12) 操作日志检查")
    r = requests.get(f'{BASE}/audit-logs', headers=H_ADMIN)
    check(r.status_code == 200, f"获取审计日志成功: {r.status_code}")
    logs = r.json()
    check(len(logs) > 0, f"审计日志不为空: {len(logs)}条")
    import_logs = [l for l in logs if l['action'] == 'snapshot_import']
    export_logs = [l for l in logs if l['action'] == 'export_json']
    check(len(import_logs) >= 1, f"存在导入日志: {len(import_logs)}条")
    check(len(export_logs) >= 1, f"存在导出日志: {len(export_logs)}条")
    for l in import_logs[:3]:
        print(f"    [{l['action']}] {l['operator_name']} - {l['detail'][:80]}")

    section("(13) 巡查员查看审计日志 - 403")
    r = requests.get(f'{BASE}/audit-logs', headers=H_INSP)
    check(r.status_code == 403, f"巡查员查看审计日志返回403: {r.status_code}")

    section("(14) 导出一致性哈希 - 供重启后校验")
    final_snap = export_json()
    final_hash = compute_hash(final_snap['orders'])
    print(f"    当前数据哈希: {final_hash}")
    print(f"    工单总数: {final_snap['total']}")
    with open(HASH_FILE, 'w', encoding='utf-8') as f:
        f.write(final_hash + '\n')
        f.write(f"timestamp: {datetime.now().isoformat()}\n")
        f.write(f"total: {final_snap['total']}\n")
        f.write(f"version: {final_snap['snapshot_version']}\n")

    print(f"\n  {PASS} Phase 1 全部通过！")
    print(f"\n  {INFO} 请重启服务后运行：")
    print(f"    python test_snapshot_restore.py --phase2")


# ============================================================
# Phase 2: 重启后一致性验证
# ============================================================
def phase2():
    section("(15) 服务重启后 - 数据一致性验证")

    try:
        with open(HASH_FILE, 'r', encoding='utf-8') as f:
            hash_before = f.readline().strip()
    except FileNotFoundError:
        print(f"  {FAIL} 找不到哈希文件，请先运行 Phase 1")
        sys.exit(1)

    r = requests.get(f'{BASE}/health')
    check(r.status_code == 200, "服务正常运行")

    snap = export_json()
    hash_after = compute_hash(snap['orders'])

    print(f"  重启前哈希: {hash_before}")
    print(f"  重启后哈希: {hash_after}")
    check(hash_before == hash_after, "重启前后数据哈希完全一致！")

    print(f"\n  工单总数: {snap['total']}（一致）")
    for s in ['待派工', '已派工', '作业中', '待复核', '已完成', '已撤销']:
        cnt = len([o for o in snap['orders'] if o['status'] == s])
        print(f"    {s}: {cnt}")

    check(snap['snapshot_version'] == '1.0.0', "重启后快照版本号仍为1.0.0")

    section("(16) 重启后再次导出-导入-导出一致性")
    snap1 = export_json()
    hash1 = compute_hash(snap1['orders'])

    r = requests.post(f'{BASE}/snapshot/import', headers=H_ADMIN, json={
        'orders': snap1['orders'],
    })
    check(r.status_code == 200, f"重新导入成功: {r.status_code}")
    result = r.json()
    check(result['failed'] == 0, f"无失败记录: failed={result['failed']}")

    snap2 = export_json()
    hash2 = compute_hash(snap2['orders'])
    print(f"  导入前哈希: {hash1}")
    print(f"  导入后哈希: {hash2}")

    extra_hist = result['imported'] > 0
    if extra_hist:
        print(f"  {INFO} 导入后有 {result['imported']} 条覆盖操作（追加了快照恢复历史记录），哈希会不同")
        print(f"  {INFO} 但工单数量和状态应不变")
        check(snap1['total'] == snap2['total'], f"工单总数不变: {snap1['total']}")
        for s in ['待派工', '已派工', '作业中', '待复核', '已完成', '已撤销']:
            c1 = len([o for o in snap1['orders'] if o['status'] == s])
            c2 = len([o for o in snap2['orders'] if o['status'] == s])
            check(c1 == c2, f"状态「{s}」数量一致: {c1}={c2}")
    else:
        check(hash1 == hash2, "无覆盖操作时，导出-导入-导出哈希一致")

    section("(17) 审计日志重启后仍可查")
    r = requests.get(f'{BASE}/audit-logs', headers=H_ADMIN)
    check(r.status_code == 200, f"审计日志接口正常: {r.status_code}")
    logs = r.json()
    check(len(logs) > 0, f"审计日志不为空: {len(logs)}条")

    print(f"\n  {PASS} Phase 2 全部通过！")
    print(f"\n  🎉 快照恢复功能回归测试全部通过！")


if __name__ == '__main__':
    if len(sys.argv) > 1:
        if sys.argv[1] == '--phase2':
            phase2()
        else:
            print(f"未知参数: {sys.argv[1]}")
            sys.exit(1)
    else:
        phase1()
