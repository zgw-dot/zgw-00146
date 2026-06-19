"""
勾选单条恢复 专用回归测试
==========================
专门验证：选中哪条就只处理哪条，未勾选数据不入库、不计入成功。
以及后续回归：单条恢复后列表/详情可见、整包导入、冲突拒绝、服务重启后再次导入。

用法：
  python test_selected_restore.py
  python test_selected_restore.py --phase2   # 重启后运行
"""
import requests, json, sys, os, hashlib, time
from datetime import datetime

BASE = 'http://127.0.0.1:8000/api'
H_ADMIN = {'X-User-Id': '1', 'Content-Type': 'application/json'}
H_INSP  = {'X-User-Id': '2', 'Content-Type': 'application/json'}

STATE_FILE = 'd:/workSpace/AI__SPACE/02-label/zgw-00146/test_sel_state.json'
HASH_FILE  = 'd:/workSpace/AI__SPACE/02-label/zgw-00146/test_sel_hash.txt'

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

def compute_hash(data):
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]

def make_order(no, road, tree_no, status="待派工", risk="低", team="", vehicle="",
               rcs="", rce="", need_close="否", desc="", histories=None):
    return {
        "order_no": no, "road": road, "tree_no": tree_no,
        "risk_level": risk, "status": status,
        "need_road_close": need_close, "team": team, "vehicle": vehicle,
        "road_close_start": rcs, "road_close_end": rce,
        "reported_at": datetime.utcnow().isoformat(),
        "description": desc or f"测试工单 {no}",
        "histories": histories or [
            {"from": "", "to": status, "operator": "系统管理员",
             "note": "测试导入", "at": datetime.utcnow().isoformat()}
        ],
    }


# ============================================================
# Phase 1: 核心 bug 复现 + 回归
# ============================================================
def phase1():
    section("(0) 环境检查")
    r = requests.get(f'{BASE}/health')
    check(r.status_code == 200, "服务正常运行")

    section("(1) 预检 - 核心 bug 复现：两条都能恢复的工单，只勾选一条")
    NO_A = f"WO_SEL_TEST_A_{int(time.time())}"
    NO_B = f"WO_SEL_TEST_B_{int(time.time())}"

    snap_orders = [
        make_order(NO_A, "勾选测试路A", "SEL-A-001", desc="勾选A-应被导入"),
        make_order(NO_B, "勾选测试路B", "SEL-B-001", desc="勾选B-不应被导入"),
    ]

    r = requests.post(f'{BASE}/snapshot/precheck', headers=H_ADMIN,
                   json={"orders": snap_orders})
    check(r.status_code == 200, f"预检成功: {r.status_code}")
    precheck = r.json()
    check(precheck["total"] == 2, "预检总数=2")
    for item in precheck["items"]:
        check(item["decision"] == "ok", f"{item['order_no']} 可恢复（decision=ok）")

    save_state(NO_A=NO_A, NO_B=NO_B)

    section("(2) 正式导入 — 只勾选 A，验证只有 A 入库")
    r = requests.post(f'{BASE}/snapshot/import', headers=H_ADMIN, json={
        "orders": snap_orders,
        "selected": [{"order_no": NO_A, "decision": "ok"}],
    })
    check(r.status_code == 200, f"导入接口成功: {r.status_code}")
    result = r.json()

    check(result["total"] == 2, f"total=2（快照里两条都在结果里）")
    check(result["imported"] == 1, f"imported=1（只有勾选的那条成功）")
    check(result["not_selected"] == 1, f"not_selected=1（未勾选那条标记为未勾选）")
    check(result["failed"] == 0, f"failed=0")
    check(result["rejected"] == 0, f"rejected=0")
    check(result["skipped"] == 0, f"skipped=0")

    item_a = next(i for i in result["items"] if i["order_no"] == NO_A)
    item_b = next(i for i in result["items"] if i["order_no"] == NO_B)

    check(item_a["action"] == "create", f"A action=create")
    check(item_a["success"] == True, "A success=True")
    check(item_b["action"] == "not_selected", f"B action=not_selected（关键断言）")
    check(item_b["success"] == False, "B success=False")
    check("未勾选" in item_b["reason"], "B 原因包含「未勾选」")
    check(item_b["action"] not in ("create", "overwrite", "imported"),
          "B 不能被记为 create/overwrite/imported")

    section("(3) 数据库级验证：A 存在、B 不存在")
    r = requests.get(f'{BASE}/orders', headers=H_ADMIN)
    all_orders = r.json()
    a_in_db = any(o["order_no"] == NO_A for o in all_orders)
    b_in_db = any(o["order_no"] == NO_B for o in all_orders)
    check(a_in_db == True, f"{NO_A} 在数据库列表中存在")
    check(b_in_db == False, f"{NO_B} 不在数据库中（关键断言）")

    section("(4) 单条恢复后的列表和详情可见性验证")
    a_obj = next(o for o in all_orders if o["order_no"] == NO_A)
    check(a_obj["status"] == "待派工", "A 状态=待派工")
    check(a_obj["road"] == "勾选测试路A", "A 路段=勾选测试路A")
    check(a_obj["tree_no"] == "SEL-A-001", "A 树木编号=SEL-A-001")

    r = requests.get(f'{BASE}/orders/{a_obj["id"]}', headers=H_ADMIN)
    a_detail = r.json()
    check(r.status_code == 200, f"A 详情接口成功: {r.status_code}")
    check(a_detail["order_no"] == NO_A, "详情工单号正确")
    check(a_detail["description"] == "勾选A-应被导入", "详情描述正确")
    check(len(a_detail.get("histories", [])) >= 1, "详情含状态历史")

    section("(5) 整包导入（不带 selected）— 两条都入库）")
    NO_C = f"WO_SEL_TEST_C_{int(time.time())}"
    NO_D = f"WO_SEL_TEST_D_{int(time.time())}"
    snap_orders2 = [
        make_order(NO_C, "整包测试路C", "SEL-C-001", desc="整包C"),
        make_order(NO_D, "整包测试路D", "SEL-D-001", desc="整包D"),
    ]
    r = requests.post(f'{BASE}/snapshot/import', headers=H_ADMIN, json={
        "orders": snap_orders2,
    })
    check(r.status_code == 200, f"整包导入成功: {r.status_code}")
    result2 = r.json()
    check(result2["imported"] == 2, f"整包 imported=2")
    check(result2.get("not_selected", 0) == 0, "整包 not_selected=0")
    for item in result2["items"]:
        check(item["action"] == "create", f"{item['order_no']} action=create")
        check(item["success"] == True, f"{item['order_no']} success=True")
        check(item["action"] != "not_selected",
              f"{item['order_no']} 不是 not_selected")

    r = requests.get(f'{BASE}/orders', headers=H_ADMIN)
    db_after_full = r.json()
    check(any(o["order_no"] == NO_C for o in db_after_full), "C 存在")
    check(any(o["order_no"] == NO_D for o in db_after_full), "D 存在")
    save_state(NO_C=NO_C, NO_D=NO_D)

    section("(6) 冲突拒绝：同一份快照勾选含冲突工单，勾选它应被拒绝")
    NO_E = f"WO_SEL_TEST_E_{int(time.time())}"
    NO_F = f"WO_SEL_TEST_F_{int(time.time())}"

    snap_orders3 = [
        make_order(NO_E, "冲突测试路E", "SEL-E-001",
                   status="已派工", team="修剪一队", vehicle="京A·12345",
                   rcs="2026-07-10T08:00:00Z", rce="2026-07-10T10:00:00Z",
                   need_close="是", desc="冲突E-有冲突"),
        make_order(NO_F, "冲突测试路F", "SEL-F-001",
                   desc="冲突F-无冲突"),
    ]
    r = requests.post(f'{BASE}/snapshot/precheck', headers=H_ADMIN,
                   json={"orders": snap_orders3})
    precheck3 = r.json()
    item_e = next(i for i in precheck3["items"] if i["order_no"] == NO_E)
    item_f = next(i for i in precheck3["items"] if i["order_no"] == NO_F)
    check(item_f["decision"] == "ok", f"F 可恢复")
    print(f"    E decision={item_e['decision']}（可能因车辆/队伍状态导致的冲突）")

    r = requests.post(f'{BASE}/snapshot/import', headers=H_ADMIN, json={
        "orders": snap_orders3,
        "selected": [
            {"order_no": NO_E, "decision": item_e["decision"]},
            {"order_no": NO_F, "decision": item_f["decision"]},
        ],
    })
    result3 = r.json()
    check(result3["total"] == 2, f"total=2")
    check(result3["imported"] >= 1, f"至少 F 被导入（F 一定无冲突）")
    check(result3["not_selected"] == 0, "not_selected=0")

    item_e_out = next(i for i in result3["items"] if i["order_no"] == NO_E)
    item_f_out = next(i for i in result3["items"] if i["order_no"] == NO_F)
    check(item_f_out["action"] == "create", f"F action=create（无冲突正常导入）")
    check(item_f_out["success"] == True, "F success=True")

    if item_e["decision"] == "reject":
        check(item_e_out["action"] in ("reject",), f"E action=reject（有冲突被拒）")
        check(item_e_out["success"] == False, "E success=False")
        check(item_e_out["reason"] != "", "E 有失败原因")
    else:
        check(item_e_out["action"] in ("create", "reject"),
              "E action=create 或 reject（状态可能 ok 则 create，冲突则 reject）")

    save_state(NO_E=NO_E, NO_F=NO_F)

    section("(7) 导出一致性哈希 — 重启后校验")
    r = requests.get(f'{BASE}/export/json', headers=H_ADMIN)
    final_snap = r.json()
    final_hash = compute_hash(final_snap["orders"])
    print(f"    当前哈希: {final_hash}")
    print(f"    工单总数: {final_snap['total']}")
    with open(HASH_FILE, 'w', encoding='utf-8') as f:
        f.write(final_hash + '\n')
        f.write(f"timestamp: {datetime.now().isoformat()}\n")
        f.write(f"total: {final_snap['total']}\n")

    save_state(final_total=final_snap['total'])

    print(f"\n  {PASS} Phase 1 全部通过！")
    print(f"\n  {INFO} 请重启服务后运行：")
    print(f"    python test_selected_restore.py --phase2")


# ============================================================
# Phase 2: 重启后再次导入一致性
# ============================================================
def phase2():
    section("(8) 服务重启后 — 数据哈希一致")
    try:
        with open(HASH_FILE, 'r', encoding='utf-8') as f:
            hash_before = f.readline().strip()
    except FileNotFoundError:
        print(f"  {FAIL} 找不到哈希文件，请先运行 Phase 1")
        sys.exit(1)

    r = requests.get(f'{BASE}/health')
    check(r.status_code == 200, "服务正常运行")

    r = requests.get(f'{BASE}/export/json', headers=H_ADMIN)
    snap_after = r.json()
    hash_after = compute_hash(snap_after["orders"])

    print(f"  重启前哈希: {hash_before}")
    print(f"  重启后哈希: {hash_after}")
    check(hash_before == hash_after, "重启前后数据哈希完全一致")

    state = load_state()
    NO_A = state.get("NO_A")
    NO_B = state.get("NO_B")
    NO_C = state.get("NO_C")
    NO_D = state.get("NO_D")

    section("(9) 重启后再次勾选单条导入 — 重复导入不产生脏数据")

    snap_after_orders = snap_after["orders"]

    r = requests.post(f'{BASE}/snapshot/import', headers=H_ADMIN, json={
        "orders": snap_after_orders,
        "selected": [{"order_no": NO_A, "decision": "overwrite_or_skip"}],
    })
    check(r.status_code == 200, f"重启后导入成功: {r.status_code}")
    result = r.json()

    not_sel_count = sum(1 for i in result["items"] if i["action"] == "not_selected")
    expected_not_sel = len(snap_after_orders) - 1
    check(not_sel_count == expected_not_sel,
          f"未勾选={not_sel_count}，预期={expected_not_sel}")

    a_out = next(i for i in result["items"] if i["order_no"] == NO_A)
    check(a_out["action"] in ("skip", "overwrite"),
          f"A 重复导入 action={a_out['action']}（非 create）")
    check(a_out["action"] != "not_selected",
          "A 不是 not_selected（因为勾选了）")

    section("(10) 重启后数据量不变 — 不产生脏数据验证")
    r = requests.get(f'{BASE}/orders', headers=H_ADMIN)
    after_reimport = r.json()
    check(len(after_reimport) == snap_after["total"],
          f"工单总数不变：{len(after_reimport)} == {snap_after['total']}")

    count_a = sum(1 for o in after_reimport if o["order_no"] == NO_A)
    count_b = sum(1 for o in after_reimport if o["order_no"] == NO_B)
    check(count_a == 1, f"{NO_A} 仍只有1条")
    check(count_b == 0, f"{NO_B} 仍为0条（从未导入）")

    section("(11) 审计日志 — 重启后仍可查，所有导入操作可追溯")
    r = requests.get(f'{BASE}/audit-logs', headers=H_ADMIN)
    check(r.status_code == 200, f"审计日志接口正常: {r.status_code}")
    logs = r.json()
    check(len(logs) > 0, f"审计日志不为空")
    import_logs = [l for l in logs if l["action"] == "snapshot_import"]
    check(len(import_logs) >= 3,
          f"至少3条导入日志（实际={len(import_logs)}）")
    print(f"\n  {PASS} Phase 2 全部通过！")
    print(f"\n  🎉 勾选单条恢复回归测试全部通过！")


if __name__ == '__main__':
    if len(sys.argv) > 1:
        if sys.argv[1] == '--phase2':
            phase2()
        else:
            print(f"未知参数: {sys.argv[1]}")
            sys.exit(1)
    else:
        phase1()
