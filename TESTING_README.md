# 校验台端到端测试说明

## 两个测试脚本的区别（重要！）

| 项目 | `test_verification_console.py` | `test_lifecycle_e2e.py` |
|------|-------------------------------|------------------------|
| **定位** | 纯请求级权限收口验证 | 真·服务生命周期验证（启动/停服/重启） |
| **是否启动服务** | 否（要求服务已在 8005 运行） | 是（自己起两个 uvicorn 子进程） |
| **是否真重启** | 否（阶段11只是再发请求） | 是（显式停单个PID再启新进程） |
| **两次启动证据** | 无 | 有（startup_time + PID 双字段对比） |
| **停服方式** | 不关服务 | 用 Stop-Process -Id 单个PID（不按进程名批量杀） |
| **适用场景** | 开发调试时快速验权限 | CI/上线前验重启链路不漂移 |

**不要混用**：
- 已经起了服务想验权限 → 用 `test_verification_console.py`
- 想验证"重启后权限/数据不丢" → 用 `test_lifecycle_e2e.py`（它会自己管服务生命周期）

---

## 环境准备

Windows PowerShell 下执行：

```powershell
cd d:\workSpace\AI__SPACE\02-label\zgw-00146
pip install -r backend\requirements.txt
pip install requests
```

确认 8005 端口当前未被占用：
```powershell
netstat -ano | findstr :8005
```

如果有输出（`LISTENING`），先手动停止对应 PID（不是本脚本启动的进程不允许自动杀）。

---

## 运行方式 A：纯请求权限验证

```powershell
# 1. 先开一个 PowerShell 窗口启动服务：
cd backend
uvicorn main:app --host 127.0.0.1 --port 8005

# 2. 另开窗口跑测试：
cd d:\workSpace\AI__SPACE\02-label\zgw-00146
python test_verification_console.py
```

### 预期输出（节选）
```
============================================================
  工单历史校验台 - 权限收口端到端测试
  (纯请求验证版，不含真实服务重启)
============================================================

阶段1：造数 [动作：纯请求]
--- 1.1 巡查员1上报工单 ---
  [PASS] 巡查员1上报工单成功
  [INFO] 工单: WO... (id=...)
...
阶段11：服务重启后权限不漂移 [动作：仅发请求，未真重启!]

[WARNING] 注意：本脚本阶段11 并没有真正停服再起!
          这里只是验证同一次运行中请求一致性。
          真·重启链路验证请运行 test_lifecycle_e2e.py
...
测试完成：通过 N 项，失败 0 项
[PASS] 所有测试通过！
```

---

## 运行方式 B：真·服务生命周期验证（推荐用于交付验收）

```powershell
cd d:\workSpace\AI__SPACE\02-label\zgw-00146
python test_lifecycle_e2e.py
```

**不需要**提前启动服务，脚本自己管生命周期。

### 覆盖的 5 个 Case

| Case | 内容 | 判定要点 |
|------|------|----------|
| **Case 1** | 服务未启动时请求失败 | 端口关 + health 抛 ConnectionError |
| **Case 2** | 第一次启动后主链路成功 | 端口开 + 造数 + 建工单校验 + 权限收口通过 |
| **Case 3** | 显式停服后请求再次失败 | 单个 PID 停止 + 端口关 + health 再抛 ConnectionError |
| **Case 4** | 第二次启动后跨重启一致性 | **启动证据不同** + **业务数据相同** + **权限不漂移** |
| **Case 5** | 最终重启判定 | 上一条 5 小项全通过 |

### 跨重启判定标准（最核心）

必须 **同时满足** 以下 5 条才认为"重启链路真的验证通过"：

1. `startup_time 不同`：第一次 `health.startup_time` != 第二次
   - 证明两次是独立进程（模块级常量 APP_STARTUP_TIME 在导入时设）
2. `PID 不同`：第一次 Popen().pid != 第二次
   - 证明我们真的杀了旧进程启了新的
3. `业务字段一致`：重启前后同一 task 的 `event_count`/`conflict_count`/`status` 完全相同
   - 证明持久化没丢（SQLite 文件正确工作）
4. `脱敏结论一致`：`can_see_batch_detail` 管理员 True、巡查员 False 不变
   - 证明权限配置没漂移
5. `列表权限一致`：巡查员只看得到自己创建的（operator_id==2）
   - 证明查询过滤逻辑在重启后仍生效

### 预期输出（节选）

```
============================================================
  校验台 服务生命周期 端到端测试
  (真·启动/停服/重启 + 两次独立启动证据)
============================================================

Case 1: 服务未启动时，请求应正确失败（连接拒绝）
--- 1.1 先确认 8005 端口当前没人监听 ---
  [PASS] 8005端口初始为关闭
--- 1.2 对 health 端点发请求，应 ConnectionError ---
  [PASS] 服务未启动时health请求失败（连接拒绝）
  [PASS] 错误类型是 ConnectionError（或等价）

Case 2: 第一次启动后，主链路（造数+建任务+权限）成功
--- 2.1 启动 uvicorn 子进程（第一次） ---
  [INFO] 启动命令: C:\Python311\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8005
  [INFO] 工作目录: d:\workSpace\AI__SPACE\02-label\zgw-00146\backend
  [INFO] 进程 PID = 12345
  [INFO] 日志文件: d:\workSpace\AI__SPACE\02-label\zgw-00146\server_run1.log
  [PASS] 第一次启动后端口变为监听
...

Case 3: 显式停服后，请求应再次失败
--- 3.1 显式停服（仅停止单个 PID=12345） ---
  [INFO] 核对：此 PID 是本脚本 2.1 节 subprocess.Popen 启动的
  [INFO] 正在停止进程 PID=12345（Case3 显式停服）...
  [PASS] 单个进程停止后端口应关闭
--- 3.2 停服后再次请求 health，应 ConnectionError ---
  [PASS] 停服后 health 请求失败（连接拒绝）

Case 4: 第二次启动（新进程），同一任务结果一致
--- 4.1 再次启动 uvicorn 子进程（第二次） ---
  [INFO] 第二次启动 PID = 23456
...
--- 4.3 两次独立启动证据判定 ---
  [PASS] 两次 startup_time 不同（证明不是同一次运行）
  [PASS] 两次 Popen PID 不同（证明不是同一个子进程）
--- 4.4 跨重启：任务详情一致性 ---
  [PASS] 管理任务 event_count 重启前后一致
  [PASS] 管理任务 conflict_count 重启前后一致
  [PASS] 管理任务 status 重启前后一致
  [PASS] 管理员 can_see_batch_detail 重启前后一致 (True)
  [PASS] 巡查员 can_see_batch_detail 重启前后一致 (False)
...
  [PASS] 【最终判定】真·重启链路有效（启动证据+业务一致）

清理
--- 清理：停止第二次启动的进程 PID=23456 ---
  [PASS] 第二次启动进程已停止

  [INFO] 两次启动证据已写入: d:\workSpace\AI__SPACE\02-label\zgw-00146\lifecycle_evidence.json

总结
  通过 N 项，失败 0 项
  通过率: 100.0%

  [RESULT] 真·重启链路: VERIFIED (已验证)
  [PASS] 所有生命周期测试通过！
```

### 运行后产出的文件

| 文件 | 内容 | 用途 |
|------|------|------|
| `lifecycle_evidence.json` | 两次启动证据+逐字段对比+最终判定 | **交付验收核心证据**，需要归档 |
| `server_run1.log` | 第一次 uvicorn 进程的 stdout/stderr | 排错用 |
| `server_run2.log` | 第二次 uvicorn 进程的 stdout/stderr | 排错用 |

**`lifecycle_evidence.json` 关键结构**：
```json
{
  "first_start": {
    "pid": 12345,
    "health_startup_time": "2026-06-20T10:00:00.123456",
    "health_pid": 12345
  },
  "second_start": {
    "pid": 23456,
    "health_startup_time": "2026-06-20T10:00:30.789012",
    "health_pid": 23456
  },
  "case4_cross_restart_consistency": {
    "startup_time_diff": true,
    "pid_diff": true,
    "admin_task_event_count_before": 5,
    "admin_task_event_count_after": 5,
    "admin_can_see_batch_before": true,
    "admin_can_see_batch_after": true,
    "insp_can_see_batch_before": false,
    "insp_can_see_batch_after": false,
    "field_equality": {
      "event_count": true,
      "conflict_count": true,
      "status": true
    }
  },
  "case5_restart_verdict": {
    "passed": true,
    "reason": "全部满足: startup不同 + PID不同 + 业务字段一致 + 权限不漂移"
  }
}
```

看 `case5_restart_verdict.passed` 是否为 `true` 即可。

---

## 排错提示

### 错误1：`8005端口已被占用`
```
[WARN] 检测到 8005 端口已有服务在监听!
[INFO] 占用者:   TCP    127.0.0.1:8005    0.0.0.0:0    LISTENING    99999
[FAIL] 端口初始应为关闭（无其他服务占用） 8005端口已被占用，请先手动清理
[FATAL] 测试环境不干净，拒绝运行。
```
**原因**：有其他进程（可能是之前手动跑的 uvicorn）占了 8005。
**解决**：
```powershell
# 先确认是不是你自己的进程（看 PID=99999）
Get-Process -Id 99999 | Select-Object Id, ProcessName, Path, StartTime
# 如果确认可以杀，再杀单个 PID
Stop-Process -Id 99999 -Force
```
**注意**：本脚本按安全规则，**不会**自动杀不是自己启动的进程。

---

### 错误2：`服务启动失败`（Case 2 超时）
看 `server_run1.log` 末尾，常见原因：
- SQLite 数据库文件被其他进程锁住（另一个 uvicorn 还在跑）
- 依赖没装全：`pip install -r backend\requirements.txt`
- 端口被防火墙拦截（本机一般不会）

---

### 错误3：`两次 startup_time 相同`
```
[FAIL] 两次 startup_time 不同（证明不是同一次运行）
       startup1=2026-06-20T10:00:00.123456 startup2=2026-06-20T10:00:00.123456
```
**原因**：旧进程没真杀掉，第二次 wait_for_port 其实连到了第一次的残留进程。
**排查**：
1. 看 `server_run1.log` 是否有关闭日志（uvicorn 的 "Shutting down"）
2. 看 Case 3 的 netstat 确认端口真的关了
3. 手动再跑一次：`Stop-Process -Id <PID1>`（换真实 PID）

---

### 错误4：`跨重启 event_count 变了`
```
[FAIL] 管理任务 event_count 重启前后一致
       before=5 after=0
```
**原因**：数据库被清空/重建了，或者两次启动用了不同的 DB_PATH。
**排查**：
1. 确认 `backend/tree_order.db` 没被删
2. 本脚本运行期间不要手动操作其他连接数据库的脚本
3. 看 `server_run1.log` 和 `server_run2.log` 启动日志，确认 `init_db()` 没丢数据

---

### 错误5：health 返回的 pid 和 Popen().pid 不一致
**这不是 bug**。uvicorn 可能会 fork worker 进程，health 返回的是 worker PID，而 Popen().pid 是主进程 PID。我们只要求 **两次的 pid 字段不同**，不强校验和 Popen PID 相等。

---

## 后端最小改动说明（为可测性）

为了支持两次启动证据判定，`backend/main.py` 做了 2 处最小改动，**无业务逻辑变化**：

1. 模块级启动时间戳（main.py L27-L28）：
   ```python
   APP_STARTUP_TIME = datetime.utcnow().isoformat()
   APP_STARTUP_PID = os.getpid()
   ```
   - 导入模块时执行一次，进程生命周期内不变

2. `/api/health` 返回值扩展（main.py L793-L800）：
   ```python
   @app.get("/api/health")
   def health():
       return {
           "ok": True,
           "time": datetime.utcnow().isoformat(),      # 原字段，向后兼容
           "startup_time": APP_STARTUP_TIME,           # 新增：启动时间
           "pid": APP_STARTUP_PID,                     # 新增：启动PID
       }
   ```
   - 旧字段保留，不影响其他调用方
   - 新增字段只用于测试证据

---

## 快速验收清单（交付时打勾）

- [ ] `python test_lifecycle_e2e.py` 无 [FAIL]
- [ ] `lifecycle_evidence.json` 中 `case5_restart_verdict.passed === true`
- [ ] `case4_cross_restart_consistency.startup_time_diff === true`
- [ ] `case4_cross_restart_consistency.pid_diff === true`
- [ ] 所有 `field_equality` 子字段为 `true`
- [ ] 可选：`python test_verification_console.py`（要求服务已运行）也全绿
