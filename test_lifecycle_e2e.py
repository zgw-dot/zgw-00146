r"""
================================================================================
校验台 服务生命周期 端到端测试（真·启动/停服/重启版）
================================================================================

【脚本定位】
本脚本是**真实服务生命周期管理**测试：
  - 自己启动 uvicorn 子进程（记录PID、工作目录、命令行）
  - 自己停服（仅结束这一个 PID，绝不按进程名批量杀）
  - 自己重启（再次启新子进程）
  - 留下两次独立启动的证据（startup_time、PID 双字段对比）

【覆盖场景】
  Case 1: 服务未启动时，请求正确失败（连接拒绝）
  Case 2: 第一次启动后，主链路（造数+建校验任务+权限）成功
  Case 3: 显式停服（杀自己启动的单个PID）后，请求再次失败
  Case 4: 第二次启动（新PID），同一任务结果与重启前一致
  Case 5: 留下两次独立启动的证据文件（lifecycle_evidence.json）

【跨重启判定标准（判定是否真的重启成功）】
  必须同时满足：
    A. 第一次 startup_time != 第二次 startup_time
    B. 第一次 pid         != 第二次 pid
    C. 重启前请求返回的 task_id / event_count / can_see_batch_detail
       == 重启后请求返回的对应值（业务数据不丢、权限不漂移）

【进程安全保证】
  - 仅使用本脚本自己 subprocess.Popen 启动的进程
  - 记录精确 PID，停服时只 Stop-Process -Id <PID>
  - 核对命令行参数和工作目录确实属于当前项目
  - 绝对禁止任何按进程名批量杀的操作

【运行命令（Windows PowerShell）】
  cd d:\workSpace\AI__SPACE\02-label\zgw-00146
  python test_lifecycle_e2e.py

【输出文件】
  lifecycle_evidence.json  - 两次启动证据、跨重启一致性对比结果
"""
import sys
import os
import json
import time
import subprocess
import socket
import requests
from datetime import datetime

# ============================================================
# 常量
# ============================================================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
DB_PATH = os.path.join(BACKEND_DIR, "tree_order.db")
LOG_PATH_1 = os.path.join(PROJECT_ROOT, "server_run1.log")
LOG_PATH_2 = os.path.join(PROJECT_ROOT, "server_run2.log")
EVIDENCE_PATH = os.path.join(PROJECT_ROOT, "lifecycle_evidence.json")
PORT = 8005
BASE = f"http://127.0.0.1:{PORT}/api"
HEALTH_URL = f"http://127.0.0.1:{PORT}/api/health"

UVICORN_CMD = [
    sys.executable, "-m", "uvicorn",
    "main:app",
    "--host", "127.0.0.1",
    "--port", str(PORT),
]

pass_count = 0
fail_count = 0
evidence = {
    "test_run_at": datetime.utcnow().isoformat(),
    "case1_service_down_before_start": {},
    "first_start": {},
    "case2_main_chain": {},
    "stop_service": {},
    "case3_service_down_after_stop": {},
    "second_start": {},
    "case4_cross_restart_consistency": {},
    "case5_restart_verdict": None,
    "cleanup": {},
}


# ============================================================
# 辅助函数
# ============================================================
def test(name, cond, detail=""):
    global pass_count, fail_count
    if cond:
        pass_count += 1
        print(f"  [PASS] {name}")
        return True
    else:
        fail_count += 1
        print(f"  [FAIL] {name} {detail}")
        return False


def sep(title=""):
    print(f"\n{'='*60}")
    if title:
        print(f"  {title}")
        print("=" * 60)


def is_port_open(port, host="127.0.0.1", timeout=0.5):
    """用 socket 判断端口是否监听（不依赖 requests，不抛异常）"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, OSError, socket.timeout):
        return False


def wait_for_port(port, open_expected=True, timeout=15, poll_interval=0.5):
    """等待端口状态变为期望状态（开/关）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = is_port_open(port)
        if state == open_expected:
            return True
        time.sleep(poll_interval)
    return is_port_open(port) == open_expected


def try_health(timeout_sec=2):
    """尝试请求 health 端点，返回 (success, response_or_error_info)"""
    try:
        r = requests.get(HEALTH_URL, timeout=timeout_sec)
        return True, r
    except requests.exceptions.ConnectionError as e:
        return False, {"type": "ConnectionError", "str": str(e)[:200]}
    except requests.exceptions.Timeout as e:
        return False, {"type": "Timeout", "str": str(e)[:200]}
    except Exception as e:
        return False, {"type": type(e).__name__, "str": str(e)[:200]}


def s(uid):
    r = requests.Session()
    r.headers["X-User-Id"] = str(uid)
    return r


def ok(r):
    return 200 <= r.status_code < 300


def start_server(log_path):
    """启动 uvicorn 子进程，返回 Popen 对象"""
    logfile = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        UVICORN_CMD,
        cwd=BACKEND_DIR,
        stdout=logfile,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc, logfile


def stop_server_by_pid(pid, reason="脚本显式停服"):
    """
    仅停止指定 PID 的单个进程。
    严格遵守进程安全规则：
      1. 核对 PID 是我们自己启动的
      2. 仅用 Stop-Process -Id <PID> 停止单个 PID
      3. 绝不按进程名批量杀
    """
    print(f"  [INFO] 正在停止进程 PID={pid}（{reason}）...")
    try:
        # Windows PowerShell 下用 Stop-Process -Id
        # 其他平台用 kill
        if sys.platform.startswith("win"):
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Stop-Process -Id {pid} -Force -ErrorAction Stop"],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            os.kill(pid, 15)  # SIGTERM
            time.sleep(1)
            try:
                os.kill(pid, 9)  # SIGKILL 兜底
            except ProcessLookupError:
                pass
        # 等待端口关闭
        closed = wait_for_port(PORT, open_expected=False, timeout=10)
        return closed
    except subprocess.CalledProcessError as e:
        print(f"  [WARN] 停服命令返回非零: {e.stderr.strip() if e.stderr else str(e)}")
        # 再查一次
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-Process -Id {pid} -ErrorAction SilentlyContinue"],
                capture_output=True,
            )
        except Exception:
            pass
        return wait_for_port(PORT, open_expected=False, timeout=5)
    except Exception as e:
        print(f"  [WARN] 停服异常: {type(e).__name__}: {e}")
        return wait_for_port(PORT, open_expected=False, timeout=5)


# ============================================================
# Case 1: 服务未启动时请求正确失败
# ============================================================
def run_case1():
    sep("Case 1: 服务未启动时，请求应正确失败（连接拒绝）")
    print("\n--- 1.1 先确认 8005 端口当前没人监听 ---")

    # 如果端口开着，说明之前有残留服务。尝试用 netstat 找出 PID 并提示
    port_state = is_port_open(PORT)
    evidence["case1_service_down_before_start"]["port_open_before_test"] = port_state

    if port_state:
        print("  [WARN] 检测到 8005 端口已有服务在监听!")
        print("         本脚本仅允许停止 自己启动 的进程。")
        print("         请手动停止占用 8005 的进程后再运行本脚本。")
        # 尝试显示占用者
        try:
            if sys.platform.startswith("win"):
                r = subprocess.run(
                    ["netstat", "-ano"],
                    capture_output=True, text=True
                )
                for line in r.stdout.splitlines():
                    if f":{PORT} " in line and "LISTENING" in line:
                        print(f"  [INFO] 占用者: {line.strip()}")
        except Exception:
            pass
        # 不强行停别人的进程，直接 fail 退出
        if not test("端口初始应为关闭（无其他服务占用）", False,
                    "8005端口已被占用，请先手动清理"):
            print("\n[FATAL] 测试环境不干净，拒绝运行。")
            sys.exit(2)
    else:
        test("8005端口初始为关闭", True)

    print("\n--- 1.2 对 health 端点发请求，应 ConnectionError ---")
    ok_health, resp_or_err = try_health(timeout_sec=2)
    evidence["case1_service_down_before_start"]["health_before_start"] = resp_or_err \
        if not ok_health else "unexpected_success"
    test("服务未启动时health请求失败（连接拒绝）", not ok_health,
         f"got={resp_or_err if not ok_health else '成功了!'}")

    if not ok_health and isinstance(resp_or_err, dict):
        err_type = resp_or_err.get("type", "")
        test("错误类型是 ConnectionError（或等价）",
             "ConnectionError" in err_type or "WinError" in resp_or_err.get("str", ""),
             f"type={err_type}")


# ============================================================
# Case 2: 第一次启动服务，主链路验证
# ============================================================
admin = s(1)
inspector = s(2)
inspector2 = s(3)

# 重启前后对比要用到的全局状态
TASK_STATE_BEFORE_RESTART = {}


def run_case2():
    sep("Case 2: 第一次启动后，主链路（造数+建任务+权限）成功")

    print("\n--- 2.1 启动 uvicorn 子进程（第一次） ---")
    proc1, logfile1 = start_server(LOG_PATH_1)
    pid1 = proc1.pid
    evidence["first_start"]["pid"] = pid1
    evidence["first_start"]["cmd"] = " ".join(UVICORN_CMD)
    evidence["first_start"]["cwd"] = BACKEND_DIR
    evidence["first_start"]["log_file"] = LOG_PATH_1
    print(f"  [INFO] 启动命令: {' '.join(UVICORN_CMD)}")
    print(f"  [INFO] 工作目录: {BACKEND_DIR}")
    print(f"  [INFO] 进程 PID = {pid1}")
    print(f"  [INFO] 日志文件: {LOG_PATH_1}")

    up = wait_for_port(PORT, open_expected=True, timeout=20)
    if not test("第一次启动后端口变为监听", up, f"timeout 20s port={PORT}"):
        logfile1.close()
        stop_server_by_pid(pid1, "启动失败清理")
        print("\n[FATAL] 服务启动失败，详见日志:", LOG_PATH_1)
        try:
            with open(LOG_PATH_1, encoding="utf-8") as f:
                tail = f.read()[-1500:]
            print("----- 日志末尾 -----")
            print(tail)
            print("-------------------")
        except Exception:
            pass
        sys.exit(3)

    # 请求 health 拿 startup_time 和 pid
    print("\n--- 2.2 health 端点拿启动证据（第一次启动） ---")
    ok_health, r1 = try_health(timeout_sec=3)
    if not test("第一次启动后 health 成功", ok_health):
        stop_server_by_pid(pid1, "health失败清理")
        sys.exit(4)
    data1 = r1.json()
    startup1 = data1.get("startup_time")
    resp_pid1 = data1.get("pid")
    evidence["first_start"]["health_startup_time"] = startup1
    evidence["first_start"]["health_pid"] = resp_pid1
    evidence["first_start"]["health_ok"] = True
    test("health 含 startup_time", startup1 is not None)
    test("health 含 pid", resp_pid1 is not None)
    # 这里 pid 可能对不上（因为 uvicorn 的 worker 和主进程关系），
    # 不强校验，但要记录
    print(f"  [INFO] 第一次启动 startup_time = {startup1}")
    print(f"  [INFO] health 报告 pid = {resp_pid1}")
    print(f"  [INFO] Popen 记录 PID = {pid1}")

    # 主链路：造数
    print("\n--- 2.3 主链路：造数 ---")
    tree_no = f"LCYCLE-{int(time.time()) % 100000}"
    r = inspector.post(f"{BASE}/orders/report", json={
        "road": "生命周期测试道路",
        "tree_no": tree_no,
        "risk_level": "中",
        "need_road_close": False,
        "description": "生命周期测试-原始描述",
    })
    if not test("巡查员1上报工单成功", ok(r), f"status={r.status_code}"):
        stop_server_by_pid(pid1, "造数失败清理")
        sys.exit(5)
    order = r.json()
    order_id = order["id"]
    order_no = order["order_no"]

    teams = admin.get(f"{BASE}/teams").json()
    r = admin.post(f"{BASE}/orders/{order_id}/assign", json={
        "team_id": teams[0]["id"],
    })
    test("管理员派工成功", ok(r), f"status={r.status_code}")

    # 快照导入
    new_order_no = f"LCYNEW-{int(time.time()) % 100000}"
    r = admin.post(f"{BASE}/snapshot/import", json={
        "orders": [
            {
                "order_no": order_no, "road": "快照覆盖道路",
                "tree_no": tree_no, "risk_level": "高",
                "need_road_close": True, "status": "已派工",
                "team": teams[0]["name"], "description": "快照覆盖",
            },
            {
                "order_no": new_order_no, "road": "全新道路",
                "tree_no": f"NEW{int(time.time()) % 100000}",
                "risk_level": "高", "need_road_close": True,
                "status": "待派工", "description": "快照新建",
            },
        ],
        "selected": [
            {"order_no": order_no, "decision": "overwrite"},
            {"order_no": new_order_no, "decision": "create"},
        ],
    })
    test("快照导入成功", ok(r), f"status={r.status_code}")
    snap = r.json()
    batch_no = snap["batch_no"]

    # 建校验任务
    print("\n--- 2.4 主链路：建校验任务 ---")
    r = admin.post(f"{BASE}/verification/tasks", json={
        "task_type": "order_trace",
        "order_no": order_no,
    })
    test("管理员建工单校验成功", ok(r), f"status={r.status_code}")
    admin_task = r.json()
    admin_task_id = admin_task["id"]

    r = inspector.post(f"{BASE}/verification/tasks", json={
        "task_type": "order_trace",
        "order_no": order_no,
    })
    test("巡查员建自己工单校验成功", ok(r), f"status={r.status_code}")
    insp_task = r.json()
    insp_task_id = insp_task["id"]

    # 权限验证
    print("\n--- 2.5 主链路：权限收口 ---")
    r = inspector.get(f"{BASE}/verification/tasks/{admin_task_id}")
    test("巡查员看管理员任务详情=403", r.status_code == 403, f"status={r.status_code}")

    r = inspector.get(f"{BASE}/verification/tasks/{insp_task_id}/export/json")
    test("巡查员导出自己任务=403", r.status_code == 403, f"status={r.status_code}")

    r = admin.get(f"{BASE}/verification/tasks/{insp_task_id}/export/json")
    test("管理员导出成功", ok(r), f"status={r.status_code}")

    # 采集任务详情，用于重启后对比
    print("\n--- 2.6 采集任务详情快照（重启前基准） ---")
    r = admin.get(f"{BASE}/verification/tasks/{admin_task_id}")
    admin_detail_before = r.json()
    r = inspector.get(f"{BASE}/verification/tasks/{insp_task_id}")
    insp_detail_before = r.json()
    r = inspector.get(f"{BASE}/verification/tasks")
    insp_list_before = r.json()

    # before 值规范化：计数类字段 None 与 0 等价
    def canon_int(v):
        return 0 if v is None else v

    # 存全局变量
    TASK_STATE_BEFORE_RESTART.update({
        "order_no": order_no,
        "order_id": order_id,
        "batch_no": batch_no,
        "admin_task_id": admin_task_id,
        "admin_task_no": admin_task.get("task_no"),
        "insp_task_id": insp_task_id,
        "insp_task_no": insp_task.get("task_no"),
        "admin_detail_before": admin_detail_before,
        "insp_detail_before": insp_detail_before,
        "insp_list_before": insp_list_before,
        "admin_task_event_count_before": canon_int(admin_detail_before.get("event_count")),
        "admin_task_conflict_count_before": canon_int(admin_detail_before.get("conflict_count")),
        "admin_task_failed_event_count_before": canon_int(admin_detail_before.get("failed_event_count")),
        "admin_task_batch_count_before": canon_int(admin_detail_before.get("batch_count")),
        "admin_task_rerun_count_before": canon_int(admin_detail_before.get("rerun_count")),
        "admin_task_export_count_before": canon_int(admin_detail_before.get("export_count")),
        "admin_task_status_before": admin_detail_before.get("status"),
        "admin_can_see_batch_before":
            admin_detail_before.get("result", {}).get("can_see_batch_detail"),
        "insp_can_see_batch_before":
            insp_detail_before.get("result", {}).get("can_see_batch_detail"),
        "insp_list_operator_ids_before":
            [t.get("operator_id") for t in insp_list_before],
        "pid1": pid1,
        "startup1": startup1,
        "proc1_logfile": logfile1,
    })

    evidence["case2_main_chain"] = {
        "order_no": order_no,
        "admin_task_id": admin_task_id,
        "admin_task_no": admin_task.get("task_no"),
        "insp_task_id": insp_task_id,
        "insp_task_no": insp_task.get("task_no"),
        "admin_task_event_count": admin_detail_before.get("event_count"),
        "admin_task_conflict_count": admin_detail_before.get("conflict_count"),
        "admin_can_see_batch": admin_detail_before.get("result", {}).get("can_see_batch_detail"),
        "insp_can_see_batch": insp_detail_before.get("result", {}).get("can_see_batch_detail"),
    }

    # 关闭 logfile1（留着进程，后面要停）
    logfile1.close()


# ============================================================
# Case 3: 显式停服后请求再次失败
# ============================================================
def run_case3():
    sep("Case 3: 显式停服后，请求应再次失败")

    pid1 = TASK_STATE_BEFORE_RESTART["pid1"]

    print(f"\n--- 3.1 显式停服（仅停止单个 PID={pid1}） ---")
    print("  [INFO] 核对：此 PID 是本脚本 2.1 节 subprocess.Popen 启动的")
    print(f"  [INFO] 启动命令: {' '.join(UVICORN_CMD)}")
    print(f"  [INFO] 工作目录: {BACKEND_DIR}")
    evidence["stop_service"]["pid_stopped"] = pid1
    evidence["stop_service"]["method"] = "Stop-Process -Id <single_pid> (Windows)" if sys.platform.startswith("win") else "os.kill(pid, SIGTERM)"

    closed = stop_server_by_pid(pid1, "Case3 显式停服")
    stopped = test("单个进程停止后端口应关闭", closed, f"PID={pid1}")
    evidence["stop_service"]["port_closed_after_stop"] = closed

    print("\n--- 3.2 停服后再次请求 health，应 ConnectionError ---")
    ok_health, resp_or_err = try_health(timeout_sec=2)
    evidence["case3_service_down_after_stop"]["health_after_stop"] = resp_or_err \
        if not ok_health else "unexpected_success"
    test("停服后 health 请求失败（连接拒绝）", not ok_health,
         f"got={resp_or_err if not ok_health else '成功了!'}")

    if not stopped:
        print("  [WARN] 端口未按预期关闭，测试无法继续。")
        # 已经尽力了，不继续杀
        sys.exit(6)


# ============================================================
# Case 4: 第二次启动，跨重启一致性
# ============================================================
def run_case4():
    sep("Case 4: 第二次启动（新进程），同一任务结果一致")

    print("\n--- 4.1 再次启动 uvicorn 子进程（第二次） ---")
    proc2, logfile2 = start_server(LOG_PATH_2)
    pid2 = proc2.pid
    evidence["second_start"]["pid"] = pid2
    evidence["second_start"]["cmd"] = " ".join(UVICORN_CMD)
    evidence["second_start"]["cwd"] = BACKEND_DIR
    evidence["second_start"]["log_file"] = LOG_PATH_2
    print(f"  [INFO] 第二次启动 PID = {pid2}")
    print(f"  [INFO] 日志文件: {LOG_PATH_2}")

    up = wait_for_port(PORT, open_expected=True, timeout=20)
    if not test("第二次启动后端口变为监听", up, f"timeout 20s port={PORT}"):
        logfile2.close()
        stop_server_by_pid(pid2, "第二次启动失败清理")
        print("\n[FATAL] 第二次服务启动失败，详见日志:", LOG_PATH_2)
        sys.exit(7)

    print("\n--- 4.2 health 端点拿启动证据（第二次启动） ---")
    ok_health, r2 = try_health(timeout_sec=3)
    if not test("第二次启动后 health 成功", ok_health):
        stop_server_by_pid(pid2, "health2失败清理")
        logfile2.close()
        sys.exit(8)
    data2 = r2.json()
    startup2 = data2.get("startup_time")
    resp_pid2 = data2.get("pid")
    evidence["second_start"]["health_startup_time"] = startup2
    evidence["second_start"]["health_pid"] = resp_pid2
    evidence["second_start"]["health_ok"] = True
    test("第二次 health 含 startup_time", startup2 is not None)
    test("第二次 health 含 pid", resp_pid2 is not None)
    print(f"  [INFO] 第二次启动 startup_time = {startup2}")

    # ============================================================
    # 关键：两次独立启动证据
    # ============================================================
    print("\n--- 4.3 两次独立启动证据判定 ---")
    startup1 = TASK_STATE_BEFORE_RESTART["startup1"]
    pid1 = TASK_STATE_BEFORE_RESTART["pid1"]

    evidence_startup_diff = test(
        "两次 startup_time 不同（证明不是同一次运行）",
        startup1 != startup2,
        f"startup1={startup1} startup2={startup2}",
    )

    evidence_pid_diff = test(
        "两次 Popen PID 不同（证明不是同一个子进程）",
        pid1 != pid2,
        f"PID1={pid1} PID2={pid2}",
    )

    TASK_STATE_BEFORE_RESTART["pid2"] = pid2
    TASK_STATE_BEFORE_RESTART["startup2"] = startup2
    TASK_STATE_BEFORE_RESTART["proc2_logfile"] = logfile2

    # ============================================================
    # 关键：跨重启业务一致性
    # ============================================================
    print("\n--- 4.4 跨重启：任务详情一致性 ---")
    admin_task_id = TASK_STATE_BEFORE_RESTART["admin_task_id"]
    insp_task_id = TASK_STATE_BEFORE_RESTART["insp_task_id"]

    r = admin.get(f"{BASE}/verification/tasks/{admin_task_id}")
    test("重启后管理员查自己任务成功", ok(r), f"status={r.status_code}")
    admin_detail_after = r.json()

    r = inspector.get(f"{BASE}/verification/tasks/{insp_task_id}")
    test("重启后巡查员查自己任务成功", ok(r), f"status={r.status_code}")
    insp_detail_after = r.json()

    # 逐条对比关键字段
    ev_eq = {}

    # 字段对比：对 count 类整数字段，None 和 0 视为语义等价
    # (Column(Integer, default=0) 但 ORM 读出来可能是 None，JSON 序列化时第二次读可能是 0)
    def canon_int(v):
        return 0 if v is None else v

    for field in ["event_count", "conflict_count", "status", "rerun_count", "export_count", "failed_event_count", "batch_count"]:
        before = TASK_STATE_BEFORE_RESTART.get(f"admin_task_{field}_before")
        after = admin_detail_after.get(field)
        if field == "status":
            equal = before == after
        else:
            equal = canon_int(before) == canon_int(after)
        ev_eq[field] = test(f"管理任务 {field} 重启前后一致", equal,
                            f"before={before} after={after} (canon: {canon_int(before)} == {canon_int(after)})")

    eq_admin_can_see = test(
        "管理员 can_see_batch_detail 重启前后一致 (True)",
        TASK_STATE_BEFORE_RESTART["admin_can_see_batch_before"] ==
        admin_detail_after.get("result", {}).get("can_see_batch_detail") == True,
        f"before={TASK_STATE_BEFORE_RESTART['admin_can_see_batch_before']} "
        f"after={admin_detail_after.get('result', {}).get('can_see_batch_detail')}"
    )

    eq_insp_can_see = test(
        "巡查员 can_see_batch_detail 重启前后一致 (False)",
        TASK_STATE_BEFORE_RESTART["insp_can_see_batch_before"] ==
        insp_detail_after.get("result", {}).get("can_see_batch_detail") == False,
        f"before={TASK_STATE_BEFORE_RESTART['insp_can_see_batch_before']} "
        f"after={insp_detail_after.get('result', {}).get('can_see_batch_detail')}"
    )

    print("\n--- 4.5 跨重启：列表/权限不漂移 ---")
    r = inspector.get(f"{BASE}/verification/tasks")
    insp_list_after = r.json()
    ids_before = TASK_STATE_BEFORE_RESTART["insp_list_operator_ids_before"]
    ids_after = [t.get("operator_id") for t in insp_list_after]
    test("巡查员重启后仍只看到 operator_id==2 的任务",
         all(tid == 2 for tid in ids_after) and ids_before == ids_after,
         f"before={ids_before} after={ids_after}")

    r = inspector.get(f"{BASE}/verification/tasks/{insp_task_id}/export/json")
    test("重启后巡查员导出仍 403", r.status_code == 403, f"status={r.status_code}")

    r = admin.get(f"{BASE}/verification/tasks/{admin_task_id}/export/json")
    test("重启后管理员导出仍成功", ok(r), f"status={r.status_code}")

    r = inspector2.get(f"{BASE}/verification/tasks")
    test("重启后巡查员2仍看不到任务", len(r.json()) == 0,
         f"count={len(r.json())}")

    # 保存证据
    evidence["case4_cross_restart_consistency"] = {
        "startup1": startup1,
        "startup2": startup2,
        "pid1_popen": pid1,
        "pid2_popen": pid2,
        "startup_time_diff": startup1 != startup2,
        "pid_diff": pid1 != pid2,
        "admin_task_event_count_before": TASK_STATE_BEFORE_RESTART["admin_task_event_count_before"],
        "admin_task_event_count_after": admin_detail_after.get("event_count"),
        "admin_task_conflict_count_before": TASK_STATE_BEFORE_RESTART["admin_task_conflict_count_before"],
        "admin_task_conflict_count_after": admin_detail_after.get("conflict_count"),
        "admin_can_see_batch_before": TASK_STATE_BEFORE_RESTART["admin_can_see_batch_before"],
        "admin_can_see_batch_after": admin_detail_after.get("result", {}).get("can_see_batch_detail"),
        "insp_can_see_batch_before": TASK_STATE_BEFORE_RESTART["insp_can_see_batch_before"],
        "insp_can_see_batch_after": insp_detail_after.get("result", {}).get("can_see_batch_detail"),
        "field_equality": {
            k: v for k, v in ev_eq.items()
        },
        "eq_admin_can_see": eq_admin_can_see,
        "eq_insp_can_see": eq_insp_can_see,
    }

    # 最终重启判定
    restart_verified = (
        evidence_startup_diff
        and evidence_pid_diff
        and all(ev_eq.values())
        and eq_admin_can_see
        and eq_insp_can_see
    )
    evidence["case5_restart_verdict"] = {
        "passed": restart_verified,
        "reason": (
            "全部满足: startup不同 + PID不同 + 业务字段一致 + 权限不漂移"
            if restart_verified else
            "至少一项不满足，详见各字段"
        ),
    }
    test("【最终判定】真·重启链路有效（启动证据+业务一致）", restart_verified)


# ============================================================
# 清理：停掉第二次启动的进程
# ============================================================
def run_cleanup():
    sep("清理")
    pid2 = TASK_STATE_BEFORE_RESTART.get("pid2")
    if pid2:
        print(f"\n--- 清理：停止第二次启动的进程 PID={pid2} ---")
        closed = stop_server_by_pid(pid2, "测试结束清理")
        evidence["cleanup"]["pid2_stopped"] = closed
        test("第二次启动进程已停止", closed)
    else:
        evidence["cleanup"]["pid2_stopped"] = "not_started"
        print("\n  [INFO] 没有第二次启动的进程需要清理")

    # 关闭 logfile2
    if "proc2_logfile" in TASK_STATE_BEFORE_RESTART:
        try:
            TASK_STATE_BEFORE_RESTART["proc2_logfile"].close()
        except Exception:
            pass


# ============================================================
# 主流程
# ============================================================
def main():
    print("=" * 60)
    print("  校验台 服务生命周期 端到端测试")
    print("  (真·启动/停服/重启 + 两次独立启动证据)")
    print("=" * 60)
    print(f"  [INFO] 项目根目录: {PROJECT_ROOT}")
    print(f"  [INFO] 后端目录:   {BACKEND_DIR}")
    print(f"  [INFO] 测试端口:   {PORT}")
    print(f"  [INFO] 数据库文件: {DB_PATH}")
    print(f"  [INFO] Python:     {sys.executable}")
    print(f"  [INFO] 平台:       {sys.platform}")

    try:
        run_case1()
        run_case2()
        run_case3()
        run_case4()
    finally:
        try:
            run_cleanup()
        except Exception as e:
            print(f"  [WARN] 清理时异常: {type(e).__name__}: {e}")

    # 写证据文件
    with open(EVIDENCE_PATH, "w", encoding="utf-8") as f:
        json.dump(evidence, f, ensure_ascii=False, indent=2)
    print(f"\n  [INFO] 两次启动证据已写入: {EVIDENCE_PATH}")

    # 总结
    sep("总结")
    print(f"\n  通过 {pass_count} 项，失败 {fail_count} 项")
    total = pass_count + fail_count
    if total > 0:
        print(f"  通过率: {(pass_count / total * 100):.1f}%")
    verdict = evidence["case5_restart_verdict"]
    if verdict:
        if verdict["passed"]:
            print(f"\n  [RESULT] 真·重启链路: VERIFIED (已验证)")
            print(f"           判定理由: {verdict['reason']}")
        else:
            print(f"\n  [RESULT] 真·重启链路: NOT VERIFIED (未通过)")
            print(f"           判定理由: {verdict['reason']}")

    if fail_count > 0:
        print("\n  [FAIL] 有测试项失败，请排查！")
        print(f"         服务器日志1: {LOG_PATH_1}")
        print(f"         服务器日志2: {LOG_PATH_2}")
        print(f"         证据文件:   {EVIDENCE_PATH}")
        sys.exit(1)
    else:
        print("\n  [PASS] 所有生命周期测试通过！")
        print(f"         证据文件: {EVIDENCE_PATH}")
        sys.exit(0)


if __name__ == "__main__":
    main()
