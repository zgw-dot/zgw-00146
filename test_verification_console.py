"""工单历史校验台 端到端测试脚本
覆盖：造数 -> 校验 -> 刷新 -> 重跑 -> 导出 -> 权限验证 -> 配置管理
"""
import requests
import sys
import time
import json
import os

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
        print(f"  ✅ PASS: {name}")
    else:
        fail_count += 1
        print(f"  ❌ FAIL: {name} {detail}")

def ok(r):
    """状态码 2xx 视为成功"""
    return 200 <= r.status_code < 300

def sep(title=""):
    print(f"\n{'='*60}")
    if title:
        print(f"  {title}")
        print("="*60)

print("=" * 60)
print("  工单历史校验台 - 端到端测试")
print("=" * 60)

# ========== 阶段1：造数 ==========
sep("阶段1：造数 - 构建复杂工单历史")

# 1.1 巡查员1上报工单
print("\n--- 1.1 巡查员1上报工单 ---")
tree_no = f"VFYTEST-{int(time.time()) % 100000}"
r = inspector.post(f"{BASE}/orders/report", json={
    "road": "校验台测试道路",
    "tree_no": tree_no,
    "species": "悬铃木",
    "risk_level": "中",
    "need_road_close": False,
    "description": "原始描述-校验台测试工单",
})
test("上报工单成功", ok(r))
order = r.json()
order_id = order["id"]
order_no = order["order_no"]
print(f"  工单: {order_no} (id={order_id})")

# 1.2 管理员派工
print("\n--- 1.2 管理员派工 ---")
teams = admin.get(f"{BASE}/teams").json()
test("获取班组列表", len(teams) > 0)
r = admin.post(f"{BASE}/orders/{order_id}/assign", json={
    "team_id": teams[0]["id"],
    "assign_note": "首次派工",
})
test("派工成功", ok(r))

# 1.3 管理员第一次快照导入（覆盖工单 + 新建工单）
print("\n--- 1.3 第一次快照导入（覆盖+新建） ---")
new_order_no = f"VFYNEW-{int(time.time()) % 100000}"
payload = {
    "orders": [
        {
            "order_no": order_no,
            "road": "道路被快照覆盖A",
            "tree_no": tree_no,
            "species": "悬铃木",
            "risk_level": "高",
            "need_road_close": True,
            "status": "已派工",
            "team": teams[0]["name"],
            "description": "快照A覆盖的描述",
        },
        {
            "order_no": new_order_no,
            "road": "全新道路A",
            "tree_no": f"NEW-A-{int(time.time()) % 100000}",
            "species": "香樟",
            "risk_level": "高",
            "need_road_close": True,
            "status": "待派工",
            "description": "快照新建的工单A",
        },
    ],
    "selected": [
        {"order_no": order_no, "decision": "overwrite"},
        {"order_no": new_order_no, "decision": "create"},
    ],
    "note": "第一次快照导入",
}
r = admin.post(f"{BASE}/snapshot/import", json=payload)
test("第一次快照导入成功", ok(r))
snap_result = r.json()
batch_id = snap_result["batch_id"]
batch_no = snap_result["batch_no"]
print(f"  批次: {batch_no} (id={batch_id})")
print(f"  total={snap_result.get('total')} imported={snap_result.get('imported')}")
test("批次有导入记录", snap_result.get("imported", 0) > 0)

# 1.4 管理员人工改派（制造后续撤销时的冲突）
print("\n--- 1.4 管理员人工改派（制造冲突） ---")
r = admin.post(f"{BASE}/orders/{order_id}/assign", json={
    "team_id": teams[1]["id"] if len(teams) > 1 else teams[0]["id"],
    "note": "人工改派-换班组，制造撤销冲突",
})
test("人工改派成功", ok(r))

# 1.5 管理员第二次快照导入（再次覆盖）
print("\n--- 1.5 第二次快照导入（再次覆盖） ---")
payload2 = {
    "orders": [
        {
            "order_no": order_no,
            "road": "道路被快照覆盖B",
            "tree_no": tree_no,
            "species": "悬铃木",
            "risk_level": "极高",
            "need_road_close": True,
            "status": "已派工",
            "team": teams[0]["name"],
            "description": "快照B覆盖的描述",
        },
    ],
    "selected": [
        {"order_no": order_no, "decision": "overwrite"},
    ],
    "note": "第二次快照导入-重复导入同一张工单",
}
r = admin.post(f"{BASE}/snapshot/import", json=payload2)
test("第二次快照导入成功", ok(r))
snap_result2 = r.json()
batch_id2 = snap_result2["batch_id"]
batch_no2 = snap_result2["batch_no"]
print(f"  批次B: {batch_no2}")

# 1.6 撤销第一个批次（因为中间有人工修改，部分会被跳过）
print("\n--- 1.6 撤销第一个快照批次 ---")
r = admin.post(f"{BASE}/restore-batches/{batch_id}/revoke", json={
    "reason": "测试撤销-制造冲突场景",
})
test("撤销批次成功", ok(r))
revoke_result = r.json()
print(f"  revoked={revoke_result.get('revoked')} failed={revoke_result.get('failed')}")

# 1.7 开始作业
print("\n--- 1.7 开始作业 ---")
r = admin.post(f"{BASE}/orders/{order_id}/start", json={
    "start_note": "开始作业",
})
test("开始作业成功", ok(r))

# ========== 阶段2：校验任务创建 ==========
sep("阶段2：创建校验任务")

# 2.1 管理员按工单创建校验
print("\n--- 2.1 管理员按工单创建校验任务 ---")
r = admin.post(f"{BASE}/verification/tasks", json={
    "task_type": "order_trace",
    "order_no": order_no,
})
test("创建工单校验任务成功", ok(r))
task = r.json()
task_id = task["id"]
task_no = task["task_no"]
print(f"  任务: {task_no} (id={task_id})")
test("任务类型正确", task["task_type"] == "order_trace")
test("任务有工单号", task.get("target_order_no") == order_no)

# 2.2 检查任务状态
print("\n--- 2.2 检查任务状态 ---")
test("任务状态为completed", task.get("status") == "completed")
test("有事件数", task.get("event_count", 0) > 0)
test("有冲突检测", "conflict_count" in task)

# 2.3 管理员按批次创建校验
print("\n--- 2.3 管理员按批次创建校验任务 ---")
r = admin.post(f"{BASE}/verification/tasks", json={
    "task_type": "batch_trace",
    "batch_no": batch_no2,
})
test("创建批次校验任务成功", ok(r))
batch_task = r.json()
batch_task_id = batch_task["id"]
print(f"  批次任务: {batch_task['task_no']}")
test("批次校验类型正确", batch_task["task_type"] == "batch_trace")

# 2.4 巡查员创建自己工单的校验
print("\n--- 2.4 巡查员创建自己工单的校验 ---")
r = inspector.post(f"{BASE}/verification/tasks", json={
    "task_type": "order_trace",
    "order_no": order_no,
})
test("巡查员创建校验成功", ok(r))
inspector_task_id = r.json()["id"]

# 2.5 巡查员尝试创建批次校验（应该失败）
print("\n--- 2.5 巡查员尝试创建批次校验（应拒绝） ---")
r = inspector.post(f"{BASE}/verification/tasks", json={
    "task_type": "batch_trace",
    "batch_no": batch_no,
})
test("巡查员不能创建批次校验", r.status_code == 403)

# 2.6 测试不存在的工单
print("\n--- 2.6 测试不存在的工单 ---")
r = admin.post(f"{BASE}/verification/tasks", json={
    "task_type": "order_trace",
    "order_no": "WO-NOT-EXIST-99999",
})
test("不存在的工单返回404", r.status_code == 404)

# ========== 阶段3：校验任务列表查询 ==========
sep("阶段3：校验任务列表查询")

# 3.1 管理员查看所有任务
print("\n--- 3.1 管理员查看所有任务 ---")
r = admin.get(f"{BASE}/verification/tasks")
test("管理员获取任务列表成功", ok(r))
admin_tasks = r.json()
test("管理员能看到所有任务", len(admin_tasks) >= 3)
print(f"  管理员可见任务数: {len(admin_tasks)}")

# 3.2 巡查员查看任务（只能看到自己的）
print("\n--- 3.2 巡查员查看任务列表 ---")
r = inspector.get(f"{BASE}/verification/tasks")
test("巡查员获取任务列表成功", ok(r))
inspector_tasks = r.json()
print(f"  巡查员1可见任务数: {len(inspector_tasks)}")

# 3.3 另一个巡查员（看不到任何任务）
print("\n--- 3.3 巡查员2查看任务（应该看不到） ---")
r = inspector2.get(f"{BASE}/verification/tasks")
test("巡查员2获取列表成功", ok(r))
inspector2_tasks = r.json()
test("巡查员2看不到别人的任务", len(inspector2_tasks) == 0)
print(f"  巡查员2可见任务数: {len(inspector2_tasks)}")

# 3.4 列表筛选测试
print("\n--- 3.4 列表筛选测试 ---")
r = admin.get(f"{BASE}/verification/tasks?status=completed")
test("按状态筛选", ok(r) and len(r.json()) > 0)

r = admin.get(f"{BASE}/verification/tasks?task_type=order_trace")
test("按类型筛选", ok(r) and len(r.json()) > 0)

r = admin.get(f"{BASE}/verification/tasks?order_no={order_no}")
test("按工单号筛选", ok(r) and len(r.json()) >= 2)

# ========== 阶段4：校验详情 ==========
sep("阶段4：校验任务详情")

# 4.1 管理员查看工单校验详情
print("\n--- 4.1 管理员查看工单校验详情 ---")
r = admin.get(f"{BASE}/verification/tasks/{task_id}")
test("管理员查看详情成功", ok(r))
detail = r.json()
print(f"  任务: {detail['task_no']} 状态: {detail['status']}")
print(f"  事件数: {detail.get('event_count', 0)}")
print(f"  冲突数: {detail.get('conflict_count', 0)}")
print(f"  失败事件数: {detail.get('failed_event_count', 0)}")
print(f"  批次数量: {detail.get('batch_count', 0)}")

# 4.2 校验结果结构
print("\n--- 4.2 校验结果结构检查 ---")
result = detail.get("result", {})
test("结果包含事件列表", "events" in result or "all_events" in result)
events = result.get("events") or result.get("all_events") or []
test("有多个事件", len(events) >= 5)

test("结果包含冲突列表", "conflicts" in result or "all_conflicts" in result)
conflicts = result.get("conflicts") or result.get("all_conflicts") or []
print(f"  检测到冲突数: {len(conflicts)}")
for c in conflicts:
    print(f"    - [{c.get('severity', '?')}] {c.get('type', '?')}: {c.get('message', '')[:60]}")

test("结果包含概览统计", "summary" in result)
summary = result.get("summary", {})
print(f"  概览: {summary}")

test("结果包含工单信息", "order" in result)
test("包含can_see_batch_detail标记", "can_see_batch_detail" in result)
test("管理员能看到批次详情", result.get("can_see_batch_detail") == True)

# 4.3 巡查员查看自己任务的详情
print("\n--- 4.3 巡查员查看自己任务的详情 ---")
r = inspector.get(f"{BASE}/verification/tasks/{inspector_task_id}")
test("巡查员查看自己的任务成功", ok(r))
inspector_detail = r.json()
inspector_result = inspector_detail.get("result", {})
test("巡查员看不到批次详情", inspector_result.get("can_see_batch_detail") == False)
test("巡查员能看到事件列表", len(inspector_result.get("events") or []) > 0)

# 4.4 巡查员尝试查看管理员任务的详情（应该权限不足）
print("\n--- 4.4 巡查员查看管理员任务（应拒绝） ---")
r = inspector.get(f"{BASE}/verification/tasks/{task_id}")
test("巡查员不能看别人的任务详情", r.status_code == 403)

# 4.5 批次校验详情
print("\n--- 4.5 批次校验详情 ---")
r = admin.get(f"{BASE}/verification/tasks/{batch_task_id}")
test("批次校验详情成功", ok(r))
batch_detail = r.json()
batch_result = batch_detail.get("result", {})
test("批次校验有工单汇总", "order_summary" in batch_result or "orders" in batch_result or "all_orders" in batch_result)
print(f"  批次校验事件数: {batch_detail.get('event_count', 0)}")
print(f"  批次校验冲突数: {batch_detail.get('conflict_count', 0)}")

# ========== 阶段5：重跑校验 ==========
sep("阶段5：重跑校验")

# 5.1 管理员重跑任务
print("\n--- 5.1 管理员重跑校验任务 ---")
r = admin.post(f"{BASE}/verification/tasks/{task_id}/rerun", json={
    "reason": "测试重跑",
})
test("重跑成功", ok(r))
rerun_task = r.json()
test("重跑次数+1", rerun_task.get("rerun_count", 0) >= 1)
test("有最后重跑时间", "last_rerun_at" in rerun_task and rerun_task["last_rerun_at"])
test("有最后重跑人", "last_rerun_by" in rerun_task)
print(f"  重跑次数: {rerun_task.get('rerun_count')}")
print(f"  最后重跑: {rerun_task.get('last_rerun_by_name', '?')}")

# 5.2 多次重跑
print("\n--- 5.2 再次重跑（验证幂等性） ---")
r = admin.post(f"{BASE}/verification/tasks/{task_id}/rerun", json={
    "reason": "第二次重跑",
})
test("第二次重跑成功", ok(r))
rerun2 = r.json()
test("重跑次数增加", rerun2.get("rerun_count", 0) >= 2)
print(f"  当前重跑次数: {rerun2.get('rerun_count')}")

# 5.3 巡查员重跑自己的任务
print("\n--- 5.3 巡查员重跑自己的任务 ---")
r = inspector.post(f"{BASE}/verification/tasks/{inspector_task_id}/rerun", json={
    "reason": "巡查员自己重跑",
})
test("巡查员重跑自己任务成功", ok(r))

# 5.4 巡查员重跑别人的任务
print("\n--- 5.4 巡查员重跑别人的任务（应拒绝） ---")
r = inspector.post(f"{BASE}/verification/tasks/{task_id}/rerun", json={
    "reason": "越权重跑",
})
test("巡查员不能重跑别人任务", r.status_code == 403)

# ========== 阶段6：导出审计包 ==========
sep("阶段6：导出审计包")

# 6.1 导出 JSON
print("\n--- 6.1 导出 JSON 审计包 ---")
r = admin.get(f"{BASE}/verification/tasks/{task_id}/export/json")
test("导出JSON成功", ok(r))
test("Content-Type是JSON", "application/json" in r.headers.get("Content-Type", ""))
export_json = r.json()
print(f"  导出JSON大小: {len(json.dumps(export_json))} 字节")
test("导出包含任务信息", "task" in export_json)
test("导出包含事件", "events" in export_json or "all_events" in export_json)
test("导出包含冲突", "conflicts" in export_json or "all_conflicts" in export_json)
test("导出包含工单信息", "order" in export_json or "all_orders" in export_json)
test("导出包含导出信息", "export" in export_json)

# 6.2 导出 CSV
print("\n--- 6.2 导出 CSV 审计包 ---")
r = admin.get(f"{BASE}/verification/tasks/{task_id}/export/csv")
test("导出CSV成功", ok(r))
test("Content-Type是CSV", "text/csv" in r.headers.get("Content-Type", "") or "application/csv" in r.headers.get("Content-Type", ""))
csv_content = r.text
print(f"  导出CSV大小: {len(csv_content)} 字节")
test("CSV有内容", len(csv_content) > 0)
test("CSV有表头", "," in csv_content and "\n" in csv_content)

# 6.3 导出后计数增加
print("\n--- 6.3 导出计数验证 ---")
r = admin.get(f"{BASE}/verification/tasks/{task_id}")
task_after_export = r.json()
test("导出次数增加", task_after_export.get("export_count", 0) >= 2)
print(f"  导出次数: {task_after_export.get('export_count')}")
test("有最后导出时间", "last_export_at" in task_after_export)
test("有最后导出人", "last_export_by" in task_after_export)

# 6.4 巡查员导出自己的任务
print("\n--- 6.4 巡查员导出自己的任务 ---")
r = inspector.get(f"{BASE}/verification/tasks/{inspector_task_id}/export/json")
test("巡查员导出自己任务成功", ok(r))

# 6.5 巡查员导出别人的任务（应该被拒绝）
print("\n--- 6.5 巡查员导出别人的任务（应拒绝） ---")
r = inspector.get(f"{BASE}/verification/tasks/{task_id}/export/json")
test("巡查员不能导出别人的任务", r.status_code == 403)

# ========== 阶段7：系统配置 ==========
sep("阶段7：系统配置管理")

# 7.1 管理员获取所有配置
print("\n--- 7.1 管理员获取系统配置 ---")
r = admin.get(f"{BASE}/system/configs")
test("获取配置列表成功", ok(r))
configs = r.json()
print(f"  配置项数量: {len(configs)}")
config_keys = [c["config_key"] for c in configs]
test("包含校验保留天数配置", "verification_retention_days" in config_keys)
test("包含导出开关配置", "verification_export_enabled" in config_keys)
test("包含自动清理开关", "verification_auto_clean_enabled" in config_keys)
test("包含追溯保留天数", "trace_retention_days" in config_keys)

# 7.2 获取单个配置
print("\n--- 7.2 获取单个配置 ---")
r = admin.get(f"{BASE}/system/configs/verification_retention_days")
test("获取单个配置成功", ok(r))
retention = r.json()
print(f"  校验保留天数: {retention.get('config_value')}")
test("配置值合理", int(retention.get("config_value", 0)) > 0)

# 7.3 修改配置
print("\n--- 7.3 修改校验保留天数 ---")
r = admin.put(f"{BASE}/system/configs/verification_retention_days", json={
    "value": "60",
})
test("修改配置成功", ok(r))
updated = r.json()
test("配置值已更新", updated.get("config_value") == "60")

# 验证修改生效
r = admin.get(f"{BASE}/system/configs/verification_retention_days")
test("配置持久化成功", r.json()["config_value"] == "60")

# 7.4 切换导出开关
print("\n--- 7.4 切换导出开关 ---")
r = admin.put(f"{BASE}/system/configs/verification_export_enabled", json={
    "value": "true",
})
test("启用导出成功", ok(r))

# 7.5 巡查员不能看配置
print("\n--- 7.5 巡查员查看配置（应拒绝） ---")
r = inspector.get(f"{BASE}/system/configs")
test("巡查员不能看配置列表", r.status_code == 403)
r = inspector.get(f"{BASE}/system/configs/verification_retention_days")
test("巡查员不能看单个配置", r.status_code == 403)
r = inspector.put(f"{BASE}/system/configs/verification_retention_days", json={"value": "999"})
test("巡查员不能修改配置", r.status_code == 403)

# 7.6 不存在的配置
print("\n--- 7.6 获取不存在的配置 ---")
r = admin.get(f"{BASE}/system/configs/not_exist_config")
test("不存在的配置返回404", r.status_code == 404)

# 还原配置
admin.put(f"{BASE}/system/configs/verification_retention_days", json={"value": "30"})

# ========== 阶段8：清理功能 ==========
sep("阶段8：过期数据清理")

# 8.1 管理员手动触发清理
print("\n--- 8.1 管理员手动清理过期任务 ---")
r = admin.get(f"{BASE}/verification/cleanup")
test("清理接口调用成功", ok(r))
cleanup_result = r.json()
print(f"  清理结果: {cleanup_result}")
test("清理结果有统计", "deleted_count" in cleanup_result)

# 8.2 巡查员不能触发清理
print("\n--- 8.2 巡查员触发清理（应拒绝） ---")
r = inspector.get(f"{BASE}/verification/cleanup")
test("巡查员不能触发清理", r.status_code == 403)

# ========== 阶段9：数据一致性验证 ==========
sep("阶段9：数据一致性验证")

# 9.1 任务列表和详情数据一致
print("\n--- 9.1 列表与详情数据一致性 ---")
r = admin.get(f"{BASE}/verification/tasks/{task_id}")
detail = r.json()
r = admin.get(f"{BASE}/verification/tasks")
list_item = next((t for t in r.json() if t["id"] == task_id), None)
test("列表中能找到任务", list_item is not None)
test("状态一致", list_item["status"] == detail["status"])
test("事件数一致", list_item.get("event_count") == detail.get("event_count"))
test("冲突数一致", list_item.get("conflict_count") == detail.get("conflict_count"))

# 9.2 多次重跑结果一致（幂等性）
print("\n--- 9.2 重跑结果幂等性验证 ---")
r = admin.post(f"{BASE}/verification/tasks/{task_id}/rerun", json={"reason": "幂等验证1"})
run1 = r.json()
r = admin.post(f"{BASE}/verification/tasks/{task_id}/rerun", json={"reason": "幂等验证2"})
run2 = r.json()
test("事件数一致", run1.get("event_count") == run2.get("event_count"))
test("冲突数一致", run1.get("conflict_count") == run2.get("conflict_count"))
test("结果摘要一致", run1.get("result_summary") == run2.get("result_summary"))

# ========== 阶段10：操作日志验证 ==========
sep("阶段10：操作日志验证")

# 10.1 检查审计日志是否有校验相关记录
print("\n--- 10.1 校验操作是否记入审计日志 ---")
# 这里我们简单验证有操作记录，通过重跑后计数增加来侧面验证
print("  操作日志已通过计数验证（重跑、导出次数正确累加）")
test("重跑次数正确累加", run2.get("rerun_count", 0) >= 4)
test("导出次数正确记录", task_after_export.get("export_count", 0) >= 2)

# ========== 总结 ==========
sep()
print(f"\n测试完成：通过 {pass_count} 项，失败 {fail_count} 项")
print(f"总计测试点: {pass_count + fail_count}")
print(f"通过率: {(pass_count/(pass_count+fail_count)*100):.1f}%")

if fail_count > 0:
    print("\n❌ 有测试失败，请检查！")
    sys.exit(1)
else:
    print("\n✅ 所有测试通过！")
    sys.exit(0)
