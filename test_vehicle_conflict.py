"""
车辆排班冲突 Bug 复现 & 回归测试脚本
====================================
用法：
  1. 先运行无参数版本，用旧代码复现 Bug：
     python test_vehicle_conflict.py

  2. 重启服务加载修复代码后，运行：
     python test_vehicle_conflict.py --phase2

  3. 再次重启服务后，运行一致性校验：
     python test_vehicle_conflict.py --phase3
"""
import requests, json, sys, hashlib, os
from datetime import datetime

BASE = 'http://127.0.0.1:8000/api'
H_ADMIN = {'X-User-Id': '1', 'Content-Type': 'application/json'}
H_INSP  = {'X-User-Id': '2', 'Content-Type': 'application/json'}

STATE_FILE = 'd:/workSpace/AI__SPACE/02-label/zgw-00146/test_state.json'
HASH_FILE  = 'd:/workSpace/AI__SPACE/02-label/zgw-00146/test_hash_before.txt'

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

def get_order_detail(oid):
    r = requests.get(f'{BASE}/orders/{oid}', headers=H_ADMIN)
    return r.json()

def get_order_history_count(oid):
    d = get_order_detail(oid)
    return len(d.get('histories', []))

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

def env_check():
    section("(0) 环境检查")
    r = requests.get(f'{BASE}/vehicles', headers=H_ADMIN)
    vehicles = r.json()
    print(f"  车辆总数: {len(vehicles)}")
    for v in vehicles:
        print(f"    ID={v['id']}  车牌={v['plate']}  类型={v.get('type')}")
    r = requests.get(f'{BASE}/health')
    print(f"  服务健康: {r.json()}")
    return vehicles

# ============================================================
# Phase 1: 用旧代码复现 Bug（无参数时运行）
# ============================================================
def phase1():
    vehicles = env_check()
    TARGET_VEHICLE_ID = vehicles[0]['id']
    TARGET_VEHICLE_PLATE = vehicles[0]['plate']
    TARGET_TEAM_A = 1
    TARGET_TEAM_B = 2
    save_state(
        vehicle_id=TARGET_VEHICLE_ID,
        vehicle_plate=TARGET_VEHICLE_PLATE,
        team_a=TARGET_TEAM_A,
        team_b=TARGET_TEAM_B,
    )

    section("Phase 1: 用未修复的旧服务复现 Bug")
    print(f"  {INFO} 预期：重叠派车应返回409，但旧代码会返回200，写脏数据")

    # 上报2张新工单
    print("\n  --- 上报2张测试工单 ---")
    r = requests.post(f'{BASE}/orders/report', headers=H_INSP, json={
        'road': '复兴路', 'tree_no': f'FX-{int(datetime.now().timestamp())}',
        'risk_level': '中', 'need_road_close': True,
        'description': '测试工单A-车辆冲突复现'
    })
    check(r.status_code == 201, f"工单A上报成功: {r.status_code}")
    order_a = r.json()
    print(f"    工单A: ID={order_a['id']} NO={order_a['order_no']} 状态={order_a['status']}")

    r = requests.post(f'{BASE}/orders/report', headers=H_INSP, json={
        'road': '长安街', 'tree_no': f'CA-{int(datetime.now().timestamp())}',
        'risk_level': '高', 'need_road_close': True,
        'description': '测试工单B-车辆冲突复现'
    })
    check(r.status_code == 201, f"工单B上报成功: {r.status_code}")
    order_b = r.json()
    print(f"    工单B: ID={order_b['id']} NO={order_b['order_no']} 状态={order_b['status']}")

    b_init_hist = get_order_history_count(order_b['id'])
    save_state(order_a_id=order_a['id'], order_b_id=order_b['id'], b_init_hist=b_init_hist)

    # 派工 A
    print("\n  --- 派工 工单A：队伍1 + 车辆1 + 09:00-11:00 ---")
    r = requests.post(f'{BASE}/orders/{order_a["id"]}/assign', headers=H_ADMIN, json={
        'team_id': TARGET_TEAM_A, 'vehicle_id': TARGET_VEHICLE_ID,
        'road_close_start': '2026-06-25T09:00:00Z',
        'road_close_end':   '2026-06-25T11:00:00Z',
    })
    check(r.status_code == 200, f"工单A派工成功: {r.status_code}")

    # 派工 B（重叠时段 + 同一辆车）
    print("\n  --- 派工 工单B：队伍2 + 车辆1 + 10:00-12:00（重叠1小时）---")
    r = requests.post(f'{BASE}/orders/{order_b["id"]}/assign', headers=H_ADMIN, json={
        'team_id': TARGET_TEAM_B, 'vehicle_id': TARGET_VEHICLE_ID,
        'road_close_start': '2026-06-25T10:00:00Z',
        'road_close_end':   '2026-06-25T12:00:00Z',
    })

    if r.status_code == 200:
        print(f"  {FAIL} Bug 复现成功！旧代码错误地返回了 200 OK")
        order_b_bad = r.json()
        print(f"    工单B状态被错误置为: {order_b_bad['status']}")
        print(f"    工单B车辆被错误写入: {order_b_bad['vehicle_id']} ({order_b_bad['vehicle_plate']})")
        print(f"    工单B历史被错误追加 {get_order_history_count(order_b['id']) - b_init_hist} 条")
        save_state(bug_reproduced=True,
                   bad_status=order_b_bad['status'],
                   bad_vehicle=order_b_bad['vehicle_id'],
                   bad_hist_added=get_order_history_count(order_b['id']) - b_init_hist)
    else:
        print(f"  {INFO} 当前服务已包含修复，状态码={r.status_code}")
        err = r.json().get('detail', '')
        print(f"    错误: {err}")
        save_state(bug_reproduced=False)

    print(f"\n{INFO} Phase 1 完成。请重启服务加载修复代码后运行：")
    print(f"    python test_vehicle_conflict.py --phase2")

# ============================================================
# Phase 2-6: 修复后验证（--phase2）
# ============================================================
def phase2():
    vehicles = env_check()
    state = load_state()
    if not state:
        print(f"  {FAIL} 请先运行 Phase 1")
        sys.exit(1)

    TARGET_VEHICLE_ID = state['vehicle_id']
    TARGET_VEHICLE_PLATE = state['vehicle_plate']
    TARGET_TEAM_A = state['team_a']
    TARGET_TEAM_B = state['team_b']
    order_a_id = state['order_a_id']
    order_b_id = state['order_b_id']

    section("Phase 2: 修复后 - 验证车辆冲突正确拦截")

    # 获取工单A/B当前状态
    order_a = get_order_detail(order_a_id)
    order_b = get_order_detail(order_b_id)
    print(f"  工单A: 状态={order_a['status']} 车辆={order_a.get('vehicle_plate')}")
    print(f"  工单B: 状态={order_b['status']} 车辆={order_b.get('vehicle_plate')}")

    # 如果旧代码把B写脏了（状态=已派工），先撤销B
    if order_b['status'] == '已派工' and order_b.get('vehicle_id') == TARGET_VEHICLE_ID:
        print(f"\n  {INFO} 清理旧代码写入的脏数据：撤销工单B")
        r = requests.post(f'{BASE}/orders/{order_b_id}/cancel', headers=H_ADMIN,
                        json={'reason': '清理Bug测试脏数据'})
        check(r.status_code == 200, f"工单B撤销成功")
        order_b = get_order_detail(order_b_id)
        check(order_b['status'] == '已撤销', f"工单B已撤销，恢复干净")

    # 新建工单C用于修复后的冲突测试
    print("\n  --- 新建工单C ---")
    r = requests.post(f'{BASE}/orders/report', headers=H_INSP, json={
        'road': '王府井大街', 'tree_no': f'WFJ-{int(datetime.now().timestamp())}',
        'risk_level': '中', 'need_road_close': True,
        'description': '测试工单C-修复后车辆冲突验证'
    })
    order_c = r.json()
    c_init_hist = get_order_history_count(order_c['id'])
    c_init_status = order_c['status']
    c_init_vehicle = order_c.get('vehicle_id')
    print(f"    工单C: ID={order_c['id']} NO={order_c['order_no']} 状态={c_init_status}")
    save_state(order_c_id=order_c['id'])

    # 确认工单A还是正常的
    order_a = get_order_detail(order_a_id)
    check(order_a['status'] == '已派工', f"工单A仍是已派工状态")
    check(order_a['vehicle_id'] == TARGET_VEHICLE_ID, f"工单A车辆正确")
    print(f"  工单A窗口: {order_a['road_close_start']} ~ {order_a['road_close_end']}")

    # 关键测试：给C派工，同一辆车+重叠时段
    print("\n  --- 派工 工单C：队伍2 + 车辆1 + 10:00-12:00（与A重叠）---")
    r = requests.post(f'{BASE}/orders/{order_c["id"]}/assign', headers=H_ADMIN, json={
        'team_id': TARGET_TEAM_B, 'vehicle_id': TARGET_VEHICLE_ID,
        'road_close_start': '2026-06-25T10:00:00Z',
        'road_close_end':   '2026-06-25T12:00:00Z',
    })

    check(r.status_code == 409, f"修复后返回409冲突（实际={r.status_code}）")
    err_detail = r.json().get('detail', '')
    check('车辆排班冲突' in err_detail, f"错误信息包含'车辆排班冲突'")
    check(TARGET_VEHICLE_PLATE in err_detail, f"错误信息包含车牌 {TARGET_VEHICLE_PLATE}")
    check('重叠' in err_detail, f"错误信息包含'重叠'")
    print(f"    ✅ 错误信息: {err_detail}")

    # 验证失败无脏数据
    print("\n  --- 验证失败时无脏数据写入 ---")
    order_c_after = get_order_detail(order_c['id'])
    check(order_c_after['status'] == c_init_status,
          f"工单C状态未变: 期望={c_init_status}, 实际={order_c_after['status']}")
    check(order_c_after.get('vehicle_id') == c_init_vehicle,
          f"工单C车辆未变: 期望={c_init_vehicle}, 实际={order_c_after.get('vehicle_id')}")
    check(get_order_history_count(order_c['id']) == c_init_hist,
          f"工单C历史未追加: 期望={c_init_hist}, 实际={get_order_history_count(order_c['id'])}")

    # 验证已派出的工单A未受影响
    print("\n  --- 验证先派出的工单A未被带坏 ---")
    order_a_after = get_order_detail(order_a_id)
    check(order_a_after['status'] == '已派工', f"工单A状态仍是已派工")
    check(order_a_after['vehicle_id'] == TARGET_VEHICLE_ID, f"工单A车辆未变")
    check(order_a_after['road_close_start'] is not None, f"工单A封路窗口仍在")

    # ================== Phase 3: 回归测试 ==================
    section("Phase 3: 回归测试")

    # 3.1 同队伍时间冲突回归
    print("\n  --- 3.1 同队伍时间冲突（已有功能回归） ---")
    r = requests.post(f'{BASE}/orders/report', headers=H_INSP, json={
        'road': '西单大街', 'tree_no': f'XD-{int(datetime.now().timestamp())}',
        'risk_level': '低', 'need_road_close': True,
        'description': '回归测试-同队伍'
    })
    order_d = r.json()
    r = requests.post(f'{BASE}/orders/{order_d["id"]}/assign', headers=H_ADMIN, json={
        'team_id': TARGET_TEAM_A, 'vehicle_id': 2,
        'road_close_start': '2026-06-25T09:30:00Z',
        'road_close_end':   '2026-06-25T10:30:00Z',
    })
    check(r.status_code == 409, f"同队伍冲突被拦截: {r.status_code}")
    check('队伍时间冲突' in r.json()['detail'], f"错误信息正确")

    # 3.2 同路段封路冲突回归
    print("\n  --- 3.2 同路段封路冲突（已有功能回归） ---")
    r = requests.post(f'{BASE}/orders/report', headers=H_INSP, json={
        'road': '复兴路', 'tree_no': f'FX-{int(datetime.now().timestamp())}',
        'risk_level': '低', 'need_road_close': True,
        'description': '回归测试-同路段'
    })
    order_e = r.json()
    r = requests.post(f'{BASE}/orders/{order_e["id"]}/assign', headers=H_ADMIN, json={
        'team_id': TARGET_TEAM_B, 'vehicle_id': 2,
        'road_close_start': '2026-06-25T09:30:00Z',
        'road_close_end':   '2026-06-25T10:30:00Z',
    })
    check(r.status_code == 409, f"同路段冲突被拦截: {r.status_code}")
    check('同路段封路窗口冲突' in r.json()['detail'], f"错误信息正确")

    # 3.3 正常派工（无冲突）
    print("\n  --- 3.3 正常派工（完全无冲突） ---")
    r = requests.post(f'{BASE}/orders/report', headers=H_INSP, json={
        'road': '东单大街', 'tree_no': f'DD-{int(datetime.now().timestamp())}',
        'risk_level': '低', 'need_road_close': True,
        'description': '回归测试-正常派工'
    })
    order_f = r.json()
    r = requests.post(f'{BASE}/orders/{order_f["id"]}/assign', headers=H_ADMIN, json={
        'team_id': TARGET_TEAM_B, 'vehicle_id': 2,
        'road_close_start': '2026-06-26T09:00:00Z',
        'road_close_end':   '2026-06-26T11:00:00Z',
    })
    check(r.status_code == 200, f"正常派工成功: {r.status_code}")
    order_f = r.json()
    check(order_f['status'] == '已派工', f"工单F状态=已派工")
    check(order_f['vehicle_id'] == 2, f"工单F车辆正确")

    # 3.4 时段重叠但车辆不同 - 应成功
    print("\n  --- 3.4 时段重叠但车辆不同 - 应成功 ---")
    r = requests.post(f'{BASE}/orders/report', headers=H_INSP, json={
        'road': '朝阳路', 'tree_no': f'CY-{int(datetime.now().timestamp())}',
        'risk_level': '低', 'need_road_close': True,
        'description': '回归测试-不同车辆'
    })
    order_g = r.json()
    r = requests.post(f'{BASE}/orders/{order_g["id"]}/assign', headers=H_ADMIN, json={
        'team_id': TARGET_TEAM_B, 'vehicle_id': 3,
        'road_close_start': '2026-06-25T09:00:00Z',
        'road_close_end':   '2026-06-25T11:00:00Z',
    })
    check(r.status_code == 200, f"不同车辆同时段派工成功: {r.status_code}")

    # 3.5 同一辆车但时段不重叠 - 应成功
    print("\n  --- 3.5 同一辆车但时段不重叠 - 应成功 ---")
    r = requests.post(f'{BASE}/orders/report', headers=H_INSP, json={
        'road': '海淀路', 'tree_no': f'HD-{int(datetime.now().timestamp())}',
        'risk_level': '中', 'need_road_close': True,
        'description': '回归测试-同车不同时'
    })
    order_h = r.json()
    r = requests.post(f'{BASE}/orders/{order_h["id"]}/assign', headers=H_ADMIN, json={
        'team_id': TARGET_TEAM_B, 'vehicle_id': TARGET_VEHICLE_ID,
        'road_close_start': '2026-06-25T14:00:00Z',
        'road_close_end':   '2026-06-25T16:00:00Z',
    })
    check(r.status_code == 200, f"同车不同时段派工成功: {r.status_code}")
    order_h = r.json()
    check(order_h['vehicle_id'] == TARGET_VEHICLE_ID, f"车辆正确指派")

    # ================== Phase 4: 列表 & 详情可见性 ==================
    section("Phase 4: 列表 & 详情可见性验证")

    print("\n  --- 4.1 工单列表接口 ---")
    r = requests.get(f'{BASE}/orders', headers=H_ADMIN)
    orders = r.json()
    check(r.status_code == 200, f"列表接口正常")
    print(f"    列表共 {len(orders)} 条工单")

    a_in_list = next((o for o in orders if o['id'] == order_a_id), None)
    check(a_in_list is not None, f"工单A在列表中")
    check(a_in_list['status'] == '已派工', f"列表中工单A状态=已派工")
    check(a_in_list['vehicle_plate'] == TARGET_VEHICLE_PLATE, f"列表中工单A车牌正确")

    c_in_list = next((o for o in orders if o['id'] == order_c['id']), None)
    check(c_in_list is not None, f"工单C在列表中")
    check(c_in_list['status'] == '待派工', f"列表中工单C状态=待派工（未被修改）")
    check(c_in_list['vehicle_plate'] is None, f"列表中工单C车辆为空（未被修改）")

    print("\n  --- 4.2 工单详情接口 ---")
    a_detail = get_order_detail(order_a_id)
    check(a_detail['status'] == '已派工', f"详情中工单A状态=已派工")
    check(a_detail['vehicle_plate'] == TARGET_VEHICLE_PLATE, f"详情中工单A车牌正确")
    check(len(a_detail['histories']) >= 2, f"详情中工单A历史完整")

    c_detail = get_order_detail(order_c['id'])
    check(c_detail['status'] == '待派工', f"详情中工单C状态=待派工")
    check(c_detail['vehicle_plate'] is None, f"详情中工单C车辆为空")
    check(len(c_detail['histories']) == c_init_hist, f"详情中工单C历史未变")

    # ================== Phase 5: 导出 ==================
    section("Phase 5: JSON/CSV 导出验证")

    print("\n  --- 5.1 JSON 导出 ---")
    r = requests.get(f'{BASE}/export/json', headers=H_ADMIN)
    check(r.status_code == 200, f"JSON 导出成功")
    json_data = r.json()
    check('orders' in json_data, f"JSON 包含 orders 字段")
    check(json_data['total'] == len(json_data['orders']), f"JSON total 正确")

    a_export = next((o for o in json_data['orders'] if o['order_no'] == order_a['order_no']), None)
    check(a_export is not None, f"工单A在导出中")
    check(a_export['vehicle'] == TARGET_VEHICLE_PLATE, f"工单A导出车辆正确")
    check(len(a_export['histories']) >= 2, f"工单A导出历史完整")

    c_export = next((o for o in json_data['orders'] if o['order_no'] == order_c['order_no']), None)
    check(c_export is not None, f"工单C在导出中")
    check(c_export['status'] == '待派工', f"工单C导出状态正确")
    check(c_export['vehicle'] == '', f"工单C导出车辆为空")

    print("\n  --- 5.2 CSV 导出 ---")
    r = requests.get(f'{BASE}/export/csv', headers=H_ADMIN)
    check(r.status_code == 200, f"CSV 导出成功")
    check('text/csv' in r.headers.get('Content-Type', ''), f"Content-Type 正确")
    lines = r.text.strip().split('\n')
    check(len(lines) >= 2, f"CSV 至少有表头+1行")
    check('order_no' in lines[0] and 'vehicle' in lines[0] and 'status' in lines[0],
          f"CSV 表头包含关键字段")

    # ================== Phase 6: 计算一致性哈希 ==================
    section("Phase 6: 计算当前数据一致性哈希")

    for_clean = {'orders': json_data['orders']}
    raw = json.dumps(for_clean, ensure_ascii=False, sort_keys=True)
    hash_before = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
    print(f"\n  当前数据哈希 (sha256 前16位): \033[93m{hash_before}\033[0m")
    print(f"  工单总数: {len(json_data['orders'])}")
    for s in ['待派工', '已派工', '作业中', '待复核', '已完成', '已撤销']:
        cnt = len([o for o in json_data['orders'] if o['status'] == s])
        print(f"    {s}: {cnt}")

    with open(HASH_FILE, 'w', encoding='utf-8') as f:
        f.write(hash_before + '\n')
        f.write(f"timestamp: {datetime.now().isoformat()}\n")
        f.write(f"total: {len(json_data['orders'])}\n")

    print(f"\n  {PASS} Phase 2-6 全部通过！")
    print(f"\n  {INFO} 请重启服务后运行一致性校验：")
    print(f"    python test_vehicle_conflict.py --phase3")

# ============================================================
# Phase 7: 重启后一致性验证（--phase3）
# ============================================================
def phase3():
    env_check()
    section("Phase 7: 服务重启后 - 数据一致性验证")

    try:
        with open(HASH_FILE, 'r', encoding='utf-8') as f:
            hash_before = f.readline().strip()
    except FileNotFoundError:
        print(f"  {FAIL} 找不到哈希文件，请先运行 --phase2")
        sys.exit(1)

    r = requests.get(f'{BASE}/export/json', headers=H_ADMIN)
    check(r.status_code == 200, f"重启后 JSON 导出成功")
    json_data = r.json()

    for_clean = {'orders': json_data['orders']}
    raw = json.dumps(for_clean, ensure_ascii=False, sort_keys=True)
    hash_after = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]

    print(f"\n  重启前哈希: \033[93m{hash_before}\033[0m")
    print(f"  重启后哈希: \033[93m{hash_after}\033[0m")

    check(hash_before == hash_after, f"重启前后数据哈希完全一致！")

    print(f"\n  工单总数: {len(json_data['orders'])}（一致）")
    for s in ['待派工', '已派工', '作业中', '待复核', '已完成', '已撤销']:
        cnt = len([o for o in json_data['orders'] if o['status'] == s])
        print(f"    {s}: {cnt} 张")

    print(f"\n  关键工单抽查：")
    for o in json_data['orders']:
        if o['description'] and ('测试工单A' in o['description'] or '测试工单C' in o['description']):
            print(f"    {o['order_no']}: {o['status']} | 队伍={o['team']} | 车辆={o['vehicle']}")

    print(f"\n  {PASS} 重启一致性验证通过！")
    print(f"\n  🎉 全部测试通过！车辆排班冲突 Bug 已修复，无回归问题。")

# ============================================================
# 入口
# ============================================================
if __name__ == '__main__':
    if len(sys.argv) > 1:
        if sys.argv[1] == '--phase2':
            phase2()
        elif sys.argv[1] == '--phase3':
            phase3()
        else:
            print(f"未知参数: {sys.argv[1]}")
            sys.exit(1)
    else:
        phase1()
