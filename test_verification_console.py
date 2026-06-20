"""
================================================================================
工单历史校验台 权限收口端到端测试（纯请求验证版）
================================================================================

【脚本定位】
本脚本是"纯 HTTP 请求级"验证脚本，**不包含服务启动/停服/重启操作**。
它验证的是：服务在**一次运行期间**各权限收口规则是否生效。

【各阶段动作分类说明】
- 阶段 1~10：纯 HTTP 请求（发请求、验响应）
- 阶段 11：   "重启后验证" --- 注意：此阶段仍然只是发请求，
               并未真正停服再起。真·重启链路验证请运行
               `test_lifecycle_e2e.py`（它会显式管理子进程生命周期）
- 阶段 12：   纯 HTTP 请求（系统开关配置）

【运行前置条件】
1. 服务必须已在 127.0.0.1:8005 启动（自行 `cd backend; uvicorn main:app --port 8005`）
2. 不需要本脚本启动/停服务

【Windows PowerShell 兼容说明】
本脚本不使用 emoji，避免在某些 PowerShell 编码环境下乱码。
输出统一使用 [PASS]/[FAIL]/[INFO] 前缀。

核心规则：自己上报的工单能看结果，但巡查员一律不能导出审计包
"""
import requests
import sys
import time
import json
import os
import subprocess
import signal

BASE = "http://127.0.0.1:8005/api"

def s(uid):
    r = requests.Session()
    r.headers["X-User-Id"] = str(uid)
    return r

admin = s(1)
inspector = s(2)
inspector2 = s(3)

pass_count = 0
fail_count = 0


def test(name, cond, detail=""):
    global pass_count, fail_count
    if cond:
        pass_count += 1
        print(f"  [PASS] {name}")
    else:
        fail_count += 1
        print(f"  [FAIL] {name} {detail}")


def ok(r):
    return 200 <= r.status_code < 300


def sep(title=""):
    print(f"\n{'='*60}")
    if title:
        print(f"  {title}")
        print("=" * 60)


print("=" * 60)
print("  工单历史校验台 - 权限收口端到端测试")
print("  (纯请求验证版，不含真实服务重启)")
print("  核心规则：巡查员可看自己结果，一律不可导出审计包")
print("=" * 60)

# ========== 阶段1：造数 ==========
sep("阶段1：造数 [动作：纯请求]")

print("\n--- 1.1 巡查员1上报工单 ---")
tree_no = f"VFYTEST-{int(time.time()) % 100000}"
r = inspector.post(f"{BASE}/orders/report", json={
    "road": "校验台测试道路",
    "tree_no": tree_no,
    "risk_level": "中",
    "need_road_close": False,
    "description": "原始描述-校验台测试工单",
})
test("巡查员1上报工单成功", ok(r), f"status={r.status_code}")
order = r.json()
order_id = order["id"]
order_no = order["order_no"]
print(f"  [INFO] 工单: {order_no} (id={order_id})")

print("\n--- 1.2 管理员派工 ---")
teams = admin.get(f"{BASE}/teams").json()
test("获取班组列表", len(teams) > 0)
r = admin.post(f"{BASE}/orders/{order_id}/assign", json={
    "team_id": teams[0]["id"],
})
test("派工成功", ok(r), f"status={r.status_code}")

print("\n--- 1.3 管理员快照导入（制造批量操作追溯） ---")
new_order_no = f"VFYNEW-{int(time.time()) % 100000}"
payload = {
    "orders": [
        {
            "order_no": order_no,
            "road": "道路被快照覆盖",
            "tree_no": tree_no,
            "risk_level": "高",
            "need_road_close": True,
            "status": "已派工",
            "team": teams[0]["name"],
            "description": "快照覆盖的描述",
        },
        {
            "order_no": new_order_no,
            "road": "全新道路",
            "tree_no": f"NEW-{int(time.time()) % 100000}",
            "risk_level": "高",
            "need_road_close": True,
            "status": "待派工",
            "description": "快照新建的工单",
        },
    ],
    "selected": [
        {"order_no": order_no, "decision": "overwrite"},
        {"order_no": new_order_no, "decision": "create"},
    ],
}
r = admin.post(f"{BASE}/snapshot/import", json=payload)
test("快照导入成功", ok(r), f"status={r.status_code}")
snap_result = r.json()
batch_id = snap_result["batch_id"]
batch_no = snap_result["batch_no"]
print(f"  [INFO] 批次: {batch_no} (id={batch_id})")

# ========== 阶段2：管理员任务 ==========
sep("阶段2：管理员创建校验任务 [动作：纯请求]")

print("\n--- 2.1 管理员按工单创建校验 ---")
r = admin.post(f"{BASE}/verification/tasks", json={
    "task_type": "order_trace",
    "order_no": order_no,
})
test("管理员创建工单校验成功", ok(r), f"status={r.status_code}")
admin_task = r.json()
admin_task_id = admin_task["id"]
admin_task_no = admin_task["task_no"]
test("任务类型正确", admin_task["task_type"] == "order_trace")
test("任务状态completed", admin_task.get("status") == "completed")
test("有事件数", admin_task.get("event_count", 0) > 0)
print(f"  [INFO] 管理员工单任务: {admin_task_no} (id={admin_task_id})")

print("\n--- 2.2 管理员按批次创建校验 ---")
r = admin.post(f"{BASE}/verification/tasks", json={
    "task_type": "batch_trace",
    "batch_no": batch_no,
})
test("管理员创建批次校验成功", ok(r), f"status={r.status_code}")
admin_batch_task = r.json()
admin_batch_task_id = admin_batch_task["id"]
test("批次校验类型正确", admin_batch_task["task_type"] == "batch_trace")

# ========== 阶段3：巡查员自建任务 ==========
sep("阶段3：巡查员自建任务 [动作：纯请求]")

print("\n--- 3.1 巡查员创建自己工单的校验 ---")
r = inspector.post(f"{BASE}/verification/tasks", json={
    "task_type": "order_trace",
    "order_no": order_no,
})
test("巡查员创建自己工单校验成功", ok(r), f"status={r.status_code}")
inspector_task = r.json()
inspector_task_id = inspector_task["id"]
inspector_task_no = inspector_task["task_no"]
print(f"  [INFO] 巡查员工单任务: {inspector_task_no} (id={inspector_task_id})")

print("\n--- 3.2 巡查员尝试创建批次校验（应403） ---")
r = inspector.post(f"{BASE}/verification/tasks", json={
    "task_type": "batch_trace",
    "batch_no": batch_no,
})
test("巡查员不能创建批次校验", r.status_code == 403, f"status={r.status_code}")

print("\n--- 3.3 巡查员2尝试创建巡查员1工单的校验（应403） ---")
r = inspector2.post(f"{BASE}/verification/tasks", json={
    "task_type": "order_trace",
    "order_no": order_no,
})
test("巡查员不能校验别人工单", r.status_code == 403, f"status={r.status_code}")

print("\n--- 3.4 不存在的工单返回404 ---")
r = admin.post(f"{BASE}/verification/tasks", json={
    "task_type": "order_trace",
    "order_no": "WO-NOT-EXIST-99999",
})
test("不存在工单返回404", r.status_code == 404, f"status={r.status_code}")

# ========== 阶段4：查看自己详情成功 ==========
sep("阶段4：查看校验任务详情 [动作：纯请求]")

print("\n--- 4.1 管理员查看自己的任务详情 ---")
r = admin.get(f"{BASE}/verification/tasks/{admin_task_id}")
test("管理员查看详情成功", ok(r), f"status={r.status_code}")
detail = r.json()
result = detail.get("result", {})
test("管理员can_see_batch_detail=True", result.get("can_see_batch_detail") == True)
test("结果包含事件列表", "events" in result)
test("结果包含冲突列表", "conflicts" in result)
test("结果包含概览统计", "summary" in result)

print("\n--- 4.2 巡查员查看自己的任务详情（成功） ---")
r = inspector.get(f"{BASE}/verification/tasks/{inspector_task_id}")
test("巡查员查看自己详情成功", ok(r), f"status={r.status_code}")
inspector_detail = r.json()
inspector_result = inspector_detail.get("result", {})
test("巡查员can_see_batch_detail=False", inspector_result.get("can_see_batch_detail") == False)
test("巡查员能看到事件列表", len(inspector_result.get("events") or []) > 0)

print("\n--- 4.3 巡查员查看管理员任务详情（应403） ---")
r = inspector.get(f"{BASE}/verification/tasks/{admin_task_id}")
test("巡查员不能看别人任务详情", r.status_code == 403, f"status={r.status_code}")

print("\n--- 4.4 巡查员2查看巡查员1任务详情（应403） ---")
r = inspector2.get(f"{BASE}/verification/tasks/{inspector_task_id}")
test("巡查员2不能看巡查员1任务详情", r.status_code == 403, f"status={r.status_code}")

print("\n--- 4.5 管理员查看巡查员创建的任务（成功+能看批次详情） ---")
r = admin.get(f"{BASE}/verification/tasks/{inspector_task_id}")
test("管理员查看巡查员创建的任务成功", ok(r), f"status={r.status_code}")
admin_view_inspector = r.json()
admin_view_result = admin_view_inspector.get("result", {})
test("管理员查看巡查员任务时can_see_batch_detail=True", admin_view_result.get("can_see_batch_detail") == True,
     f"got={admin_view_result.get('can_see_batch_detail')}")

# ========== 阶段5：导出自己任务和别人任务都返回403（巡查员） ==========
sep("阶段5：导出审计包权限（巡查员一律403）[动作：纯请求]")

print("\n--- 5.1 管理员导出JSON成功 ---")
r = admin.get(f"{BASE}/verification/tasks/{admin_task_id}/export/json")
test("管理员导出JSON成功", ok(r), f"status={r.status_code}")
test("Content-Type是JSON", "application/json" in r.headers.get("Content-Type", ""))
export_json = r.json()
test("导出包含任务信息", "task" in export_json)
test("导出包含校验结果", "verification_result" in export_json)
test("导出包含事件", "events" in export_json.get("verification_result", {}))

print("\n--- 5.2 管理员导出CSV成功 ---")
r = admin.get(f"{BASE}/verification/tasks/{admin_task_id}/export/csv")
test("管理员导出CSV成功", ok(r), f"status={r.status_code}")
test("Content-Type是CSV", "text/csv" in r.headers.get("Content-Type", ""))

print("\n--- 5.3 巡查员导出自己的任务JSON（应403） ---")
r = inspector.get(f"{BASE}/verification/tasks/{inspector_task_id}/export/json")
test("巡查员导出自己任务JSON->403", r.status_code == 403, f"status={r.status_code}")

print("\n--- 5.4 巡查员导出自己的任务CSV（应403） ---")
r = inspector.get(f"{BASE}/verification/tasks/{inspector_task_id}/export/csv")
test("巡查员导出自己任务CSV->403", r.status_code == 403, f"status={r.status_code}")

print("\n--- 5.5 巡查员导出管理员任务JSON（应403） ---")
r = inspector.get(f"{BASE}/verification/tasks/{admin_task_id}/export/json")
test("巡查员导出别人任务JSON->403", r.status_code == 403, f"status={r.status_code}")

print("\n--- 5.6 巡查员导出管理员任务CSV（应403） ---")
r = inspector.get(f"{BASE}/verification/tasks/{admin_task_id}/export/csv")
test("巡查员导出别人任务CSV->403", r.status_code == 403, f"status={r.status_code}")

print("\n--- 5.7 403错误信息包含巡查员不可导出 ---")
r = inspector.get(f"{BASE}/verification/tasks/{inspector_task_id}/export/json")
detail_msg = r.json().get("detail", "") if r.status_code == 403 else ""
test("403提示巡查员不可导出", "巡查员" in detail_msg and "导出" in detail_msg, f"detail={detail_msg}")

# ========== 阶段6：重跑自己任务成功 ==========
sep("阶段6：重跑校验任务 [动作：纯请求]")

print("\n--- 6.1 管理员重跑自己任务 ---")
r = admin.post(f"{BASE}/verification/tasks/{admin_task_id}/rerun", json={"reason": "管理员重跑"})
test("管理员重跑成功", ok(r), f"status={r.status_code}")
rerun_result = r.json()
test("重跑次数+1", rerun_result.get("rerun_count", 0) >= 1)
test("有最后重跑时间", rerun_result.get("last_rerun_at") is not None)

print("\n--- 6.2 巡查员重跑自己的任务（成功） ---")
r = inspector.post(f"{BASE}/verification/tasks/{inspector_task_id}/rerun", json={"reason": "巡查员重跑"})
test("巡查员重跑自己任务成功", ok(r), f"status={r.status_code}")
inspector_rerun = r.json()
test("巡查员重跑后rerun_count+1", inspector_rerun.get("rerun_count", 0) >= 1)

print("\n--- 6.3 巡查员重跑别人的任务（应403） ---")
r = inspector.post(f"{BASE}/verification/tasks/{admin_task_id}/rerun", json={"reason": "越权重跑"})
test("巡查员不能重跑别人任务", r.status_code == 403, f"status={r.status_code}")

print("\n--- 6.4 巡查员2重跑巡查员1任务（应403） ---")
r = inspector2.post(f"{BASE}/verification/tasks/{inspector_task_id}/rerun", json={"reason": "越权重跑"})
test("巡查员2不能重跑巡查员1任务", r.status_code == 403, f"status={r.status_code}")

# ========== 阶段7：任务列表权限 ==========
sep("阶段7：任务列表查询权限 [动作：纯请求]")

print("\n--- 7.1 管理员查看所有任务 ---")
r = admin.get(f"{BASE}/verification/tasks")
test("管理员获取列表成功", ok(r))
admin_tasks = r.json()
test("管理员能看到所有任务", len(admin_tasks) >= 3)
print(f"  [INFO] 管理员可见: {len(admin_tasks)} 条")

print("\n--- 7.2 巡查员只能看到自己的任务 ---")
r = inspector.get(f"{BASE}/verification/tasks")
test("巡查员获取列表成功", ok(r))
inspector_tasks = r.json()
test("巡查员只看到自己创建的", all(t["operator_id"] == 2 for t in inspector_tasks),
     f"operator_ids={[t['operator_id'] for t in inspector_tasks]}")
print(f"  [INFO] 巡查员1可见: {len(inspector_tasks)} 条")

print("\n--- 7.3 巡查员2看不到任何任务 ---")
r = inspector2.get(f"{BASE}/verification/tasks")
test("巡查员2获取列表成功", ok(r))
inspector2_tasks = r.json()
test("巡查员2看不到别人的任务", len(inspector2_tasks) == 0,
     f"count={len(inspector2_tasks)}")

# ========== 阶段8：结果脱敏 ==========
sep("阶段8：结果脱敏验证 [动作：纯请求]")

print("\n--- 8.1 巡查员看自己的任务：批量操作事件隐藏快照/差异 ---")
r = inspector.get(f"{BASE}/verification/tasks/{inspector_task_id}")
detail = r.json()
result = detail.get("result", {})
events = result.get("events", [])
batch_events = [e for e in events if e.get("is_batch_operation")]
if batch_events:
    for e in batch_events:
        test("巡查员：批量事件before_snapshot=None", e.get("before_snapshot") is None,
             f"got={e.get('before_snapshot')}")
        test("巡查员：批量事件after_snapshot=None", e.get("after_snapshot") is None)
        test("巡查员：批量事件diffs=None", e.get("diffs") is None)
        test("巡查员：批量事件batch_no=None", e.get("batch_no") is None)
        test("巡查员：批量事件fail_reason=None", e.get("fail_reason") is None)
        break
else:
    print("  [INFO] (无批量操作事件，跳过脱敏验证)")

print("\n--- 8.2 管理员看巡查员的任务：批量操作事件可见 ---")
r = admin.get(f"{BASE}/verification/tasks/{inspector_task_id}")
detail = r.json()
result = detail.get("result", {})
events = result.get("events", [])
batch_events = [e for e in events if e.get("is_batch_operation")]
if batch_events:
    for e in batch_events:
        test("管理员：批量事件有before_snapshot或after_snapshot",
             e.get("before_snapshot") is not None or e.get("after_snapshot") is not None)
        test("管理员：批量事件有batch_no", e.get("batch_no") is not None)
        break
else:
    print("  [INFO] (无批量操作事件，跳过)")

# ========== 阶段9：导出计数+审计日志 ==========
sep("阶段9：导出计数和审计日志链路 [动作：纯请求]")

print("\n--- 9.1 验证导出次数累加 ---")
r = admin.get(f"{BASE}/verification/tasks/{admin_task_id}")
task_before = r.json()
export_before = task_before.get("export_count", 0)

r = admin.get(f"{BASE}/verification/tasks/{admin_task_id}/export/json")
test("管理员再次导出JSON", ok(r))

r = admin.get(f"{BASE}/verification/tasks/{admin_task_id}")
task_after = r.json()
test("导出次数+1", task_after.get("export_count", 0) == export_before + 1,
     f"before={export_before} after={task_after.get('export_count')}")
test("有最后导出时间", task_after.get("last_export_at") is not None)

print("\n--- 9.2 验证审计日志有校验记录 ---")
r = admin.get(f"{BASE}/audit-logs?action=verification_create&limit=10")
test("获取审计日志成功", ok(r))
logs = r.json()
vfy_logs = [l for l in logs if "verification" in l.get("action", "")]
test("有校验相关审计日志", len(vfy_logs) > 0)

print("\n--- 9.3 重跑后rerun_count+1，状态和计数走同一条链路 ---")
r = admin.get(f"{BASE}/verification/tasks/{admin_task_id}")
before_rerun = r.json()
rerun_before = before_rerun.get("rerun_count", 0)
event_before = before_rerun.get("event_count", 0)
conflict_before = before_rerun.get("conflict_count", 0)

r = admin.post(f"{BASE}/verification/tasks/{admin_task_id}/rerun", json={"reason": "链路验证"})
test("重跑成功", ok(r))
after_rerun = r.json()
test("rerun_count+1", after_rerun.get("rerun_count", 0) == rerun_before + 1,
     f"before={rerun_before} after={after_rerun.get('rerun_count')}")
test("event_count不变", after_rerun.get("event_count") == event_before)
test("conflict_count不变", after_rerun.get("conflict_count") == conflict_before)
test("status回到completed", after_rerun.get("status") == "completed")

# ========== 阶段10：刷新页面后权限结论不漂移 ==========
sep("阶段10：刷新页面后权限不漂移 [动作：纯请求]")

print("\n--- 10.1 重新获取列表确认权限 ---")
r = inspector.get(f"{BASE}/verification/tasks")
test("巡查员刷新后获取列表成功", ok(r))
inspector_tasks_refreshed = r.json()
test("巡查员刷新后仍只看到自己的", all(t["operator_id"] == 2 for t in inspector_tasks_refreshed))

r = inspector2.get(f"{BASE}/verification/tasks")
test("巡查员2刷新后看不到任务", len(r.json()) == 0)

print("\n--- 10.2 重新获取详情确认脱敏 ---")
r = inspector.get(f"{BASE}/verification/tasks/{inspector_task_id}")
test("巡查员刷新后查看详情成功", ok(r))
refreshed_result = r.json().get("result", {})
test("刷新后can_see_batch_detail仍为False", refreshed_result.get("can_see_batch_detail") == False)

print("\n--- 10.3 重新确认巡查员导出仍403 ---")
r = inspector.get(f"{BASE}/verification/tasks/{inspector_task_id}/export/json")
test("刷新后巡查员导出仍403", r.status_code == 403, f"status={r.status_code}")

print("\n--- 10.4 重新确认管理员导出仍成功 ---")
r = admin.get(f"{BASE}/verification/tasks/{admin_task_id}/export/json")
test("刷新后管理员导出仍成功", ok(r), f"status={r.status_code}")

# ========== 阶段11：服务重启后权限不漂移 ==========
sep("阶段11：服务重启后权限不漂移 [动作：仅发请求，未真重启!]")

print("\n[WARNING] 注意：本脚本阶段11 并没有真正停服再起!")
print("          这里只是验证同一次运行中请求一致性。")
print("          真·重启链路验证请运行 test_lifecycle_e2e.py")

print("\n--- 11.1 验证health端点可用（含启动证据字段） ---")
r = requests.get(f"{BASE}/health")
test("health端点正常", ok(r))
health_data = r.json()
test("health含startup_time字段", "startup_time" in health_data)
test("health含pid字段", "pid" in health_data)
print(f"  [INFO] 当前服务 startup_time={health_data.get('startup_time')}")
print(f"  [INFO] 当前服务 pid={health_data.get('pid')}")

print("\n--- 11.2 同次运行中再次验证核心权限 ---")
r = inspector.get(f"{BASE}/verification/tasks")
test("巡查员列表正常", ok(r))
test("巡查员只看到自己的", all(t["operator_id"] == 2 for t in r.json()))

r = inspector.get(f"{BASE}/verification/tasks/{inspector_task_id}")
test("巡查员查看自己详情成功", ok(r))
test("脱敏仍生效", r.json().get("result", {}).get("can_see_batch_detail") == False)

r = inspector.get(f"{BASE}/verification/tasks/{inspector_task_id}/export/json")
test("巡查员导出仍403", r.status_code == 403, f"status={r.status_code}")

r = inspector.get(f"{BASE}/verification/tasks/{admin_task_id}/export/json")
test("巡查员导出别人任务仍403", r.status_code == 403, f"status={r.status_code}")

r = admin.get(f"{BASE}/verification/tasks/{admin_task_id}/export/json")
test("管理员导出仍成功", ok(r), f"status={r.status_code}")

r = admin.get(f"{BASE}/verification/tasks/{inspector_task_id}")
test("管理员看巡查员任务can_see_batch=True",
     r.json().get("result", {}).get("can_see_batch_detail") == True)

# ========== 阶段12：导出开关配置 ==========
sep("阶段12：导出开关配置 [动作：纯请求]")

print("\n--- 12.1 管理员关闭导出开关 ---")
r = admin.put(f"{BASE}/system/configs/verification_export_enabled", json={"config_value": "false"})
test("关闭导出开关", ok(r), f"status={r.status_code}")

print("\n--- 12.2 管理员导出也被禁用 ---")
r = admin.get(f"{BASE}/verification/tasks/{admin_task_id}/export/json")
test("关闭开关后管理员导出也403", r.status_code == 403, f"status={r.status_code}")

print("\n--- 12.3 重新启用导出 ---")
r = admin.put(f"{BASE}/system/configs/verification_export_enabled", json={"config_value": "true"})
test("重新启用导出", ok(r), f"status={r.status_code}")

r = admin.get(f"{BASE}/verification/tasks/{admin_task_id}/export/json")
test("启用后管理员导出恢复成功", ok(r), f"status={r.status_code}")

# ========== 总结 ==========
sep()
print(f"\n测试完成：通过 {pass_count} 项，失败 {fail_count} 项")
print(f"总计测试点: {pass_count + fail_count}")
if pass_count + fail_count > 0:
    print(f"通过率: {(pass_count/(pass_count+fail_count)*100):.1f}%")

print("\n[INFO] 纯请求权限验证完成。")
print("       如需验证真实重启链路，请运行: python test_lifecycle_e2e.py")

if fail_count > 0:
    print("\n[FAIL] 有测试失败，请检查！")
    sys.exit(1)
else:
    print("\n[PASS] 所有测试通过！")
    sys.exit(0)
