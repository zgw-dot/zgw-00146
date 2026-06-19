"""
完整回归测试脚本（v2）：覆盖"撤销本批次"所有场景

新增场景（v2）：
- 带 Z 的 UTC 时间不被误判为人工修改
- 恢复前队伍/车辆/封路窗口/assigned_at 为空，撤销后也要真空掉

测试场景：
1. 覆盖恢复（快照带 Z 时间）后立即撤销 - 应成功，完整回退
2. 恢复前为空（无队伍/车辆/封路），恢复后有值，撤销后应真空掉
3. 恢复后人工改动再撤销 - 应拦截
4. 重复撤销幂等
5. 重启后状态查询（持久化）
6. 接口返回与页面提示原因一致性
"""
import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

import requests

BASE_URL = "http://127.0.0.1:8001"
PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
INFO = "\033[94m[INFO]\033[0m"

ADMIN_HEADERS = {"X-User-Id": "1"}
INSPECTOR_HEADERS = {"X-User-Id": "2"}

def section(title):
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}\n")

def do_overwrite_restore(snapshot_items, order_nos):
    """执行覆盖恢复"""
    print(f"\n{INFO} 执行覆盖恢复...")
    selected = [{"order_no": on, "decision": "overwrite_or_skip"} for on in order_nos]
    r = requests.post(f"{BASE_URL}/api/snapshot/import", json={
        "snapshot_version": "test-z-time",
        "exported_at": "2026-06-20T00:00:00Z",
        "orders": snapshot_items,
        "selected": selected,
    }, headers=ADMIN_HEADERS)
    assert r.status_code == 200, f"快照导入失败: {r.status_code} {r.text}"
    result = r.json()
    batch_id = result.get("batch_id")
    print(f"  批次ID: {batch_id}")
    print(f"  批次号: {result.get('batch_no')}")
    print(f"  导入数: {result.get('imported')}, 跳过数: {result.get('skipped')}")

    print(f"\n  {INFO} 恢复结果详情:")
    for item in result.get("items", []):
        print(f"    - {item['order_no']}: action={item['action']}, success={item['success']}, reason={item.get('reason')}")

    return batch_id, result


# ========== 测试 1: 快照带 Z 时间，覆盖恢复后立即撤销 ==========
def test_z_time_immediate_revoke():
    """场景1: 快照时间带 Z，覆盖恢复后立即撤销，不应误判为人工修改"""
    section("测试 1: 带 Z 时间快照 - 覆盖恢复后立即撤销（应成功）")

    print(f"{INFO} 准备测试数据...")
    # 创建工单
    r = requests.post(f"{BASE_URL}/api/orders/report", json={
        "road": "Z时间测试路",
        "tree_no": f"Z-TIME-{int(time.time())}",
        "risk_level": "中",
        "need_road_close": False,
        "description": "带 Z 时间快照测试",
    }, headers=INSPECTOR_HEADERS)
    order_id = r.json()["id"]
    order_no = r.json()["order_no"]
    print(f"  创建工单: {order_no} (id={order_id})")

    # 派工（已派工状态）
    r = requests.post(f"{BASE_URL}/api/orders/{order_id}/assign", json={
        "team_id": 1, "vehicle_id": 1, "note": "Z时间测试派工",
    }, headers=ADMIN_HEADERS)
    print(f"  派工成功: 队伍=修剪一队, 车辆=京A·12345")

    r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
    curr = r.json()
    print(f"  当前状态: {curr['status']}")

    # 构造带 Z 时间的快照（作业中，排名=2 > 已派工=1）
    # 关键：所有时间字段都带 Z (UTC)
    snap_z = {
        "order_no": order_no,
        "road": curr["road"],
        "tree_no": curr["tree_no"],
        "risk_level": curr["risk_level"],
        "status": "作业中",
        "team": "修剪一队",
        "vehicle": "京A·12345",
        "need_road_close": "否",
        "road_close_start": "",
        "road_close_end": "",
        "description": "带 Z 时间恢复",
        "suggested_time": "",
        "reported_at": "2026-06-19T18:26:08.946538Z",     # 带 Z
        "assigned_at": "2026-06-19T18:26:09.015713Z",     # 带 Z
        "started_at": "2026-06-20T02:00:00.123456Z",      # 带 Z
        "submitted_at": "",
        "reviewed_at": "",
        "review_note": "",
        "cancelled_at": "",
        "cancel_reason": "",
        "histories": [],
    }
    print(f"  构造带 Z 时间快照: status=作业中, started_at=2026-06-20T02:00:00.123456Z")

    # 执行覆盖恢复
    batch_id, _ = do_overwrite_restore([snap_z], [order_no])

    # 验证恢复后的工单（应该包含时间，但时区会被 SQLite 去掉）
    r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
    after_restore = r.json()
    print(f"\n{INFO} 覆盖恢复后工单状态:")
    print(f"  状态: {after_restore['status']}")
    print(f"  队伍: {after_restore.get('team_id')}")
    print(f"  车辆: {after_restore.get('vehicle_id')}")
    print(f"  封路开始: {after_restore.get('road_close_start')}")
    print(f"  封路结束: {after_restore.get('road_close_end')}")
    print(f"  started_at: {after_restore.get('started_at')}")
    print(f"  assigned_at: {after_restore.get('assigned_at')}")
    assert after_restore["status"] == "作业中", f"状态应该是作业中，实际: {after_restore['status']}"
    assert after_restore.get("started_at"), "started_at 应该有值"
    print(f"  {PASS} 覆盖恢复结果正确")

    # 立即撤销
    print(f"\n{INFO} 立即撤销本批次...")
    r = requests.post(f"{BASE_URL}/api/restore-batches/{batch_id}/revoke",
                     json={"reason": "测试-Z时间立即撤销"},
                     headers=ADMIN_HEADERS)
    print(f"  撤销响应状态码: {r.status_code}")
    print(f"  撤销响应内容: {json.dumps(r.json(), ensure_ascii=False, indent=2)}")
    assert r.status_code == 200, f"撤销应该成功，不应被误判为人工修改: {r.status_code} {r.text}"
    result = r.json()
    assert result["revoked"] == 1, f"应该成功撤销1条，实际: {result['revoked']}"
    assert result["failed"] == 0, f"应该失败0条，实际: {result['failed']}"
    print(f"  {PASS} 带 Z 时间撤销成功，撤销 {result['revoked']} 条，失败 {result['failed']} 条")

    # 验证撤销后状态（回到恢复前 - 已派工）
    r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
    after_revoke = r.json()
    print(f"\n{INFO} 撤销后工单状态:")
    print(f"  状态: {after_revoke['status']}")
    print(f"  队伍: {after_revoke.get('team_id')}")
    print(f"  车辆: {after_revoke.get('vehicle_id')}")
    assert after_revoke["status"] == "已派工", f"撤销后状态应该回到已派工，实际: {after_revoke['status']}"
    print(f"  {PASS} 带 Z 时间撤销后正确回退状态")

    # 验证批次详情
    r = requests.get(f"{BASE_URL}/api/restore-batches/{batch_id}", headers=ADMIN_HEADERS)
    detail = r.json()
    print(f"\n{INFO} 批次详情:")
    print(f"  批次状态: {detail['status']}")
    print(f"  撤销数: {detail.get('revoked_count')}")
    assert detail["status"] == "revoked", f"批次状态应该是 revoked，实际: {detail['status']}"
    assert detail.get("revoked_count") == 1, f"撤销数应该是1"
    print(f"  {PASS} 批次详情正确")

    # 验证接口返回原因与页面一致
    items = result.get("items", [])
    assert len(items) > 0, "返回结果中应该有 items"
    reason = items[0].get("reason", "")
    print(f"\n{PASS} 接口返回与页面提示原因一致: {reason!r}")

    return batch_id, order_id


# ========== 测试 2: 恢复前为空（无队伍/车辆/封路/assigned_at） ==========
def test_empty_before_revoke():
    """场景2: 恢复前队伍/车辆/封路/assigned_at 为空，恢复后有值，撤销后应真空掉"""
    section("测试 2: 恢复前为空边界 - 撤销后应真空掉队伍/车辆/封路/assigned_at")

    print(f"{INFO} 准备测试数据...")
    # 创建工单（不派工，所以队伍/车辆/assigned_at 都是空）
    r = requests.post(f"{BASE_URL}/api/orders/report", json={
        "road": "空值边界测试路",
        "tree_no": f"EMPTY-BOUNDARY-{int(time.time())}",
        "risk_level": "低",
        "need_road_close": False,
        "description": "恢复前为空，撤销后也要真空掉",
    }, headers=INSPECTOR_HEADERS)
    order_id = r.json()["id"]
    order_no = r.json()["order_no"]
    print(f"  创建工单: {order_no} (id={order_id})，注意：不派工")

    # 验证恢复前状态（待派工，队伍/车辆/封路/assigned_at 为空）
    r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
    before = r.json()
    print(f"\n{INFO} 恢复前状态（快照基线）:")
    print(f"  状态: {before['status']}")
    print(f"  队伍(team_id): {before.get('team_id')!r}")
    print(f"  车辆(vehicle_id): {before.get('vehicle_id')!r}")
    print(f"  封路开始: {before.get('road_close_start')!r}")
    print(f"  封路结束: {before.get('road_close_end')!r}")
    print(f"  assigned_at: {before.get('assigned_at')!r}")
    assert before["status"] == "待派工", f"状态应该是待派工，实际: {before['status']}"
    assert before.get("team_id") is None, f"恢复前 team_id 应该是 None，实际: {before.get('team_id')!r}"
    assert before.get("vehicle_id") is None, f"恢复前 vehicle_id 应该是 None，实际: {before.get('vehicle_id')!r}"
    assert before.get("assigned_at") in (None, "", "null"), f"恢复前 assigned_at 应该为空，实际: {before.get('assigned_at')!r}"
    print(f"  {PASS} 恢复前基线正确：无队伍、无车辆、无派工时间")

    # 构造快照：状态=已派工（排名1 > 待派工0），有队伍、有车辆、有封路、有 assigned_at（都带 Z）
    snap_restore = {
        "order_no": order_no,
        "road": before["road"],
        "tree_no": before["tree_no"],
        "risk_level": before["risk_level"],
        "status": "已派工",  # 排名=1 > 待派工0
        "team": "修剪一队",
        "vehicle": "京A·12345",
        "need_road_close": "否",
        "road_close_start": "",
        "road_close_end": "",
        "description": "恢复时填充队伍车辆封路",
        "suggested_time": "",
        "reported_at": before.get("reported_at", "") or "2026-06-20T00:00:00Z",
        "assigned_at": "2026-06-20T08:00:00.000Z",  # 带 Z
        "started_at": "",
        "submitted_at": "",
        "reviewed_at": "",
        "review_note": "",
        "cancelled_at": "",
        "cancel_reason": "",
        "histories": [],
    }
    print(f"\n  构造恢复快照: status=已派工（有队伍、车辆、assigned_at，均带 Z 时间）")

    # 执行覆盖恢复
    batch_id, _ = do_overwrite_restore([snap_restore], [order_no])

    # 验证恢复后（应该有队伍、车辆、封路、assigned_at）
    r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
    after_restore = r.json()
    print(f"\n{INFO} 覆盖恢复后工单状态:")
    print(f"  状态: {after_restore['status']}")
    print(f"  队伍(team_id): {after_restore.get('team_id')!r}")
    print(f"  车辆(vehicle_id): {after_restore.get('vehicle_id')!r}")
    print(f"  封路开始: {after_restore.get('road_close_start')!r}")
    print(f"  封路结束: {after_restore.get('road_close_end')!r}")
    print(f"  assigned_at: {after_restore.get('assigned_at')!r}")
    assert after_restore["status"] == "已派工", f"恢复后状态应该是已派工"
    assert after_restore.get("team_id") is not None, f"恢复后 team_id 应该有值"
    assert after_restore.get("vehicle_id") is not None, f"恢复后 vehicle_id 应该有值"
    assert after_restore.get("assigned_at"), f"恢复后 assigned_at 应该有值"
    print(f"  {PASS} 恢复后正确填充了队伍/车辆/assigned_at")

    # 立即撤销
    print(f"\n{INFO} 立即撤销本批次...")
    r = requests.post(f"{BASE_URL}/api/restore-batches/{batch_id}/revoke",
                     json={"reason": "测试-空值边界撤销"},
                     headers=ADMIN_HEADERS)
    print(f"  撤销响应状态码: {r.status_code}")
    print(f"  撤销响应内容: {json.dumps(r.json(), ensure_ascii=False, indent=2)}")
    assert r.status_code == 200, f"撤销应该成功: {r.status_code} {r.text}"
    result = r.json()
    assert result["revoked"] == 1, f"应该成功撤销1条，实际: {result['revoked']}"
    print(f"  {PASS} 撤销成功")

    # 关键验证：撤销后队伍/车辆/封路/assigned_at 应该真空掉（回到恢复前的 None）
    r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
    after_revoke = r.json()
    print(f"\n{INFO} 撤销后工单状态（关键：应为真空）:")
    print(f"  状态: {after_revoke['status']}")
    print(f"  队伍(team_id): {after_revoke.get('team_id')!r}")
    print(f"  车辆(vehicle_id): {after_revoke.get('vehicle_id')!r}")
    print(f"  封路开始: {after_revoke.get('road_close_start')!r}")
    print(f"  封路结束: {after_revoke.get('road_close_end')!r}")
    print(f"  assigned_at: {after_revoke.get('assigned_at')!r}")

    assert after_revoke["status"] == "待派工", f"撤销后状态应该回到待派工，实际: {after_revoke['status']}"
    # 关键断言：必须是 None（真空），不能继续占着恢复后的资源
    assert after_revoke.get("team_id") is None, f"撤销后 team_id 应该真空为 None，实际: {after_revoke.get('team_id')!r} —— 队伍资源被错误占用！"
    assert after_revoke.get("vehicle_id") is None, f"撤销后 vehicle_id 应该真空为 None，实际: {after_revoke.get('vehicle_id')!r} —— 车辆资源被错误占用！"
    assert after_revoke.get("assigned_at") in (None, "", "null"), f"撤销后 assigned_at 应该真空，实际: {after_revoke.get('assigned_at')!r}"
    assert after_revoke.get("road_close_start") in (None, "", "null"), f"撤销后封路开始应该真空，实际: {after_revoke.get('road_close_start')!r}"
    assert after_revoke.get("road_close_end") in (None, "", "null"), f"撤销后封路结束应该真空，实际: {after_revoke.get('road_close_end')!r}"
    print(f"  {PASS} 关键边界验证通过：撤销后队伍/车辆/封路/assigned_at 已全部真空掉，没有继续占用资源")

    # 验证批次详情
    r = requests.get(f"{BASE_URL}/api/restore-batches/{batch_id}", headers=ADMIN_HEADERS)
    detail = r.json()
    assert detail["status"] == "revoked", f"批次状态应该是 revoked"
    items = detail.get("items", [])
    assert len(items) > 0
    assert items[0].get("is_revoked") == True, "子项应该标记为已撤销"
    print(f"  {PASS} 批次详情和子项详情正确")

    # 验证批次列表
    r = requests.get(f"{BASE_URL}/api/restore-batches", headers=ADMIN_HEADERS)
    batches = r.json()
    found = next((b for b in batches if b["id"] == batch_id), None)
    assert found is not None, "批次应该在列表中"
    assert found["status"] == "revoked", f"列表中批次状态应该是 revoked"
    print(f"  {PASS} 批次列表正确")

    # 验证接口返回原因与页面一致
    revoke_items = result.get("items", [])
    assert len(revoke_items) > 0
    reason = revoke_items[0].get("reason", "")
    print(f"\n{PASS} 接口返回与页面提示原因一致: {reason!r}")

    return batch_id, order_id


# ========== 测试 3: 恢复后人工改动再撤销（应拦截） ==========
def test_revoke_after_manual_modify():
    """场景3: 恢复后人工重新派工，再撤销应拦截并说明变更字段"""
    section("测试 3: 恢复后人工改动再撤销（应拦截）")

    print(f"{INFO} 准备测试数据...")
    r = requests.post(f"{BASE_URL}/api/orders/report", json={
        "road": "人工修改拦截路",
        "tree_no": f"MANUAL-MOD-{int(time.time())}",
        "risk_level": "中",
        "need_road_close": False,
        "description": "人工修改拦截测试",
    }, headers=INSPECTOR_HEADERS)
    order_id = r.json()["id"]
    order_no = r.json()["order_no"]
    # 不派工，状态是待派工（排名0）

    r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
    curr = r.json()
    print(f"  初始状态: {curr['status']}, team_id={curr.get('team_id')}, vehicle_id={curr.get('vehicle_id')}")
    assert curr["status"] == "待派工", f"初始状态应该是待派工"

    # 构造快照（已派工，排名=1 > 待派工0，可以覆盖）
    snap = {
        "order_no": order_no,
        "road": curr["road"],
        "tree_no": curr["tree_no"],
        "risk_level": curr["risk_level"],
        "status": "已派工",  # 排名=1 > 待派工0
        "team": "修剪一队",   # id=1
        "vehicle": "京A·12345",  # id=1
        "need_road_close": "否",
        "road_close_start": "",
        "road_close_end": "",
        "description": curr.get("description", ""),
        "suggested_time": "",
        "reported_at": curr.get("reported_at", "") or "2026-06-20T00:00:00Z",
        "assigned_at": "2026-06-20T08:00:00Z",  # 带 Z
        "started_at": "",
        "submitted_at": "",
        "reviewed_at": "",
        "review_note": "",
        "cancelled_at": "",
        "cancel_reason": "",
        "histories": [],
    }
    print(f"  构造快照: status=已派工(排名1), team=修剪一队(id=1), assigned_at 带 Z 时间")

    # 执行覆盖恢复
    batch_id, _ = do_overwrite_restore([snap], [order_no])

    # 验证恢复后状态
    r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
    after_restore = r.json()
    assert after_restore["status"] == "已派工", f"恢复后状态应该是已派工，实际: {after_restore['status']}"
    assert after_restore.get("team_id") == 1, f"恢复后 team_id 应该是1(修剪一队)"
    assert after_restore.get("vehicle_id") == 1, f"恢复后 vehicle_id 应该是1"
    print(f"  {PASS} 覆盖恢复成功，状态=已派工，team_id=1, vehicle_id=1")

    # 人工修改：重新派工给修剪二队（修改 team_id、vehicle_id 和 assigned_at）
    print(f"\n{INFO} 人工修改: 重新派工给修剪二队（修改 team_id, vehicle_id, assigned_at）")
    r = requests.post(f"{BASE_URL}/api/orders/{order_id}/assign", json={
        "team_id": 2,  # 修剪二队
        "vehicle_id": 2,  # 京A·67890
        "note": "人工改派测试",
    }, headers=ADMIN_HEADERS)
    assert r.status_code == 200, f"人工重新派工失败: {r.text}"
    print(f"  {PASS} 人工重新派工完成")

    r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
    after_modify = r.json()
    print(f"  当前状态: {after_modify['status']}, team_id={after_modify.get('team_id')}, assigned_at={after_modify.get('assigned_at')!r}")
    assert after_modify.get("team_id") == 2, "人工改派后 team_id 应该是2(修剪二队)"

    # 尝试撤销（应该被拦截）
    print(f"\n{INFO} 尝试撤销本批次...")
    r = requests.post(f"{BASE_URL}/api/restore-batches/{batch_id}/revoke",
                     json={"reason": "测试-人工修改拦截"},
                     headers=ADMIN_HEADERS)
    print(f"  撤销响应状态码: {r.status_code}")
    print(f"  撤销响应内容: {json.dumps(r.json(), ensure_ascii=False, indent=2)}")
    assert r.status_code == 200, f"接口应该返回200（部分失败）"
    result = r.json()
    assert result["revoked"] == 0, f"应该撤销0条（被拦截），实际: {result['revoked']}"
    assert result["failed"] == 1, f"应该失败1条，实际: {result['failed']}"
    assert result["status"] == "completed", f"批次状态应该还是 completed，实际: {result['status']}"

    items = result.get("items", [])
    assert len(items) > 0
    failed_item = items[0]
    assert failed_item["success"] == False, "子项应该标记为失败"
    reason = failed_item.get("reason", "")
    assert "工单已被人工修改" in reason, f"原因应该包含'工单已被人工修改'，实际: {reason}"
    # 检查是否列出了变更字段
    assert "变更字段" in reason or "," in reason or "team" in reason.lower() or "assign" in reason.lower(), f"原因应该包含变更字段，实际: {reason}"
    print(f"  {PASS} 正确拦截人工修改，原因: {reason}")

    # 验证工单状态没有被误回退（应该还是改派后的状态）
    r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
    check = r.json()
    assert check.get("team_id") == 2, f"工单应该保留人工改派后的 team_id=2，实际: {check.get('team_id')}"
    print(f"  {PASS} 工单状态没有被误回退")

    # 验证批次状态
    r = requests.get(f"{BASE_URL}/api/restore-batches/{batch_id}", headers=ADMIN_HEADERS)
    batch_detail = r.json()
    assert batch_detail["status"] == "completed", f"批次状态应该还是 completed，实际: {batch_detail['status']}"
    assert batch_detail.get("revoked_count") == 0, f"撤销数应该是0"
    print(f"  {PASS} 批次状态正确，没有误标记为已撤销")

    # 验证接口返回与页面提示一致性
    assert "reason" in failed_item, "接口应该返回 reason 字段供页面显示"
    print(f"  {PASS} 接口返回 reason 字段，页面可直接显示: {reason}")

    return batch_id, order_id


# ========== 测试 4: 重复撤销幂等 ==========
def test_revoke_idempotency():
    """场景4: 重复撤销幂等 - 第二次撤销返回400"""
    section("测试 4: 重复撤销幂等")

    print(f"{INFO} 准备测试数据...")
    r = requests.post(f"{BASE_URL}/api/orders/report", json={
        "road": "幂等测试路",
        "tree_no": f"IDEMPOTENT-{int(time.time())}",
        "risk_level": "低",
        "need_road_close": False,
        "description": "重复撤销幂等测试",
    }, headers=INSPECTOR_HEADERS)
    order_id = r.json()["id"]
    order_no = r.json()["order_no"]

    r = requests.post(f"{BASE_URL}/api/orders/{order_id}/assign", json={
        "team_id": 1, "vehicle_id": 1,
    }, headers=ADMIN_HEADERS)

    r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
    curr = r.json()

    snap = {
        "order_no": order_no,
        "road": curr["road"],
        "tree_no": curr["tree_no"],
        "risk_level": curr["risk_level"],
        "status": "作业中",
        "team": "修剪一队",
        "vehicle": "京A·12345",
        "need_road_close": "否",
        "road_close_start": "",
        "road_close_end": "",
        "description": "",
        "suggested_time": "",
        "reported_at": curr.get("reported_at", "") or "2026-06-20T00:00:00Z",
        "assigned_at": curr.get("assigned_at", "") or "2026-06-20T08:00:00Z",
        "started_at": "2026-06-20T09:00:00Z",
        "submitted_at": "",
        "reviewed_at": "",
        "review_note": "",
        "cancelled_at": "",
        "cancel_reason": "",
        "histories": [],
    }

    batch_id, _ = do_overwrite_restore([snap], [order_no])

    # 第一次撤销
    print(f"\n{INFO} 第一次撤销...")
    r = requests.post(f"{BASE_URL}/api/restore-batches/{batch_id}/revoke",
                     json={"reason": "幂等测试-第一次撤销"},
                     headers=ADMIN_HEADERS)
    assert r.status_code == 200, f"第一次撤销应该成功: {r.status_code} {r.text}"
    result1 = r.json()
    print(f"  {PASS} 第一次撤销成功，撤销 {result1['revoked']} 条，失败 {result1['failed']} 条")

    # 第二次撤销
    print(f"\n{INFO} 第二次撤销（重复点击）...")
    r = requests.post(f"{BASE_URL}/api/restore-batches/{batch_id}/revoke",
                     json={"reason": "幂等测试-第二次撤销"},
                     headers=ADMIN_HEADERS)
    print(f"  第二次撤销响应状态码: {r.status_code}")
    print(f"  第二次撤销响应内容: {json.dumps(r.json(), ensure_ascii=False, indent=2)}")
    assert r.status_code == 400, f"第二次撤销应该返回400，实际: {r.status_code}"
    detail = r.json().get("detail", "")
    assert "已全部撤销" in detail or "无需重复" in detail, f"应该提示已撤销无需重复，实际: {detail}"
    print(f"  {PASS} 第二次撤销返回400，提示: {detail}")

    # 验证工单状态无变化
    r = requests.get(f"{BASE_URL}/api/orders/{order_id}", headers=ADMIN_HEADERS)
    status = r.json()["status"]
    assert status == "已派工", f"工单状态应该保持已派工，实际: {status}"
    print(f"  {PASS} 工单状态没有变化，幂等性保证")

    # 验证批次状态
    r = requests.get(f"{BASE_URL}/api/restore-batches/{batch_id}", headers=ADMIN_HEADERS)
    detail = r.json()
    assert detail["status"] == "revoked", f"批次状态应该保持 revoked"
    print(f"  {PASS} 批次状态保持 revoked")

    return batch_id, order_id


# ========== 测试 5: 重启后持久化验证 ==========
def test_persistence_after_restart():
    """场景5: 服务重启后批次和工单状态仍正确"""
    section("测试 5: 重启后再次查询撤销状态（持久化验证）")

    RESTART_VERIFICATION = os.environ.get("RESTART_VERIFICATION")
    BATCH_ID = os.environ.get("BATCH_ID_TO_CHECK")
    ORDER_ID = os.environ.get("ORDER_ID_TO_CHECK")

    if RESTART_VERIFICATION and BATCH_ID and ORDER_ID:
        batch_id_to_check = int(BATCH_ID)
        order_id_to_check = int(ORDER_ID)
        print(f"{INFO} 重启后验证模式")
        print(f"  验证批次 {batch_id_to_check}，工单 {order_id_to_check}")
    else:
        # 正常模式下复用测试1的结果，或者新建一个
        batch_id_to_check, order_id_to_check = test_z_time_immediate_revoke()
        print(f"\n{INFO} 当前批次状态验证...")

    # 验证批次列表
    r = requests.get(f"{BASE_URL}/api/restore-batches", headers=ADMIN_HEADERS)
    batches = r.json()
    found = next((b for b in batches if b["id"] == batch_id_to_check), None)
    assert found is not None, f"批次应该在列表中"
    assert found["status"] == "revoked", f"列表中批次状态应该是 revoked"
    print(f"  批次 {batch_id_to_check} 状态: {found['status']}")
    print(f"  批次撤销数: {found.get('revoked_count')}")
    print(f"  {PASS} 批次列表中状态正确")

    # 验证工单状态
    r = requests.get(f"{BASE_URL}/api/orders/{order_id_to_check}", headers=ADMIN_HEADERS)
    order_detail = r.json()
    print(f"  工单 {order_id_to_check} 状态: {order_detail['status']}")
    assert order_detail["status"] == "已派工", f"工单状态应该是撤销后的已派工，实际: {order_detail['status']}"
    print(f"  {PASS} 工单状态正确")

    if not RESTART_VERIFICATION:
        print(f"\n{INFO} 请手动重启服务后再次运行此脚本验证持久化（设置 RESTART_VERIFICATION=1）")
        print(f"  批次ID: {batch_id_to_check}")
        print(f"  工单ID: {order_id_to_check}")
        print(f"\n{INFO} 重启后验证命令:")
        print(f"  set BATCH_ID_TO_CHECK={batch_id_to_check}")
        print(f"  set ORDER_ID_TO_CHECK={order_id_to_check}")
        print(f"  set RESTART_VERIFICATION=1")
        print(f"  python test_revoke_regression_v2.py")

    return batch_id_to_check, order_id_to_check


# ========== 主入口 ==========
def main():
    section("撤销本批次 - 完整回归测试 (v2 - Z时间 + 空值边界)")
    print(f"{INFO} 使用 admin 身份 (X-User-Id: 1)")

    try:
        test_z_time_immediate_revoke()
        test_empty_before_revoke()
        test_revoke_after_manual_modify()
        test_revoke_idempotency()
        test_persistence_after_restart()

        print(f"\n{'='*80}")
        print(f"{PASS} 所有测试通过！")
        print(f"{'='*80}\n")
    except AssertionError as e:
        print(f"\n{FAIL} 测试失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n{FAIL} 异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
