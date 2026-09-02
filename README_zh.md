# Uni-Lab-OS 异常捕获演示

[English](README.md) | **中文**

这个外部设备包演示 Uni-Lab-OS 中异常**沿网页工作流提交路径**的传播方式：像网页一样创建
任务（`POST /api/v1/workflow-tasks`），失败的 attempt 出现在错误决策链
（`GET /api/v1/error-decisions`），由操作者的决策（`POST /api/v1/error-decisions/{id}`）
决定结果。设备内部不自跑任何闭环，每一条路径都是一个工作流节点：

- **异常穿出动作边界**（`fault_injector.run_step(fail=True)`）：job 失败并挂入错误决策链，
  等待 `retry` / `abort` / `operator_intervention` 放行——「任务失败」是显式决策结果，
  而不是隐式超时；
- **点对点调用、调用侧捕获**（`supervisor.probe_remote_failure`）：监督器经
  `DeviceNode.call_device_action` 调用 `fault_injector.run_step(fail=True)`，远端
  `RuntimeError` 直接传播回来并被它的 `try/except` 捕获，因此这个 job **成功**，异常只体现在
  返回值里；
- **业务级兜底**（`fault_injector.run_guarded`）：驱动内部捕获后返回结构化错误，job 成功；
- **人工替换结果**：决策选 `operator_intervention` 并携带替代结果，失败 attempt 以
  `suc_type=operator_intervention` 成功放行，任务继续；
- **重试即新 attempt**（`fault_injector.run_flaky`）：决策选 `retry`，失败 attempt 保留在
  节点运行的历史里，同一事务内为同一节点运行追加 `attempt 2`、任务不中断；第二次调用成功后
  成为该节点的当前结果，DAG 继续；
- **故障后可用性**：`stats` 继续服务并如实计数。

## 从 GitHub 安装

```bash
unilab package install https://github.com/Xuwznln/LabDeviceExceptionDemo --ref <commit-sha>
```

本地开发可使用：

```bash
git clone https://github.com/Xuwznln/LabDeviceExceptionDemo.git
cd LabDeviceExceptionDemo
python -m pip install -e .
```

本地演示不需要 AK/SK，也不依赖云端实验室。

## 有终止条件的双运行时 smoke

```bash
python -m exception_demo.smoke --backend hostlink --timeout 40
python -m exception_demo.smoke --backend ros2 --timeout 60
```

smoke 启动真实运行时（`unilab -g graph/exception_demo.json`，启动时把 `@workflow` 模板上报到
本机 Workflow Authority），然后经管理 HTTP API 完整复现网页的操作：

节点结果一律读 `GET /api/v1/workflow-tasks/{uuid}/node-runs`：每个工作流节点一条，`status /
return_info` 是当前 attempt 的结果，`attempts` 内嵌该节点的完整执行历史。

1. **「异常传播演示」**（预期终态 `failed`，4 个节点运行）：`run_step(warmup)` 成功 →
   `supervisor.probe_remote_failure` 在调用侧捕获远端 `RuntimeError`（节点 `succeeded`，
   返回值 `caught: true` 且错误文本保真携带 `injected-failure`）→ `run_guarded(fail=True)`
   返回结构化错误（节点 `succeeded`）→ `run_step(final, fail=True)` 异常穿出；待决策报文
   （异常类型、错误文本、可选项 `retry` / `abort` / `operator_intervention`）以 `abort` 放行，
   该节点运行 `failed` 且带 `error_info`，任务终态 `failed`。
2. **「人工替换恢复演示」**（预期终态 `succeeded`，2 个节点运行）：`run_step(flaky, fail=True)`
   失败 → 决策选 `operator_intervention` 并携带替代 `result` → 同一 attempt 以
   `suc_type=operator_intervention` 和替代值成功放行 → `stats` 运行并报告
   `attempts=5, failures=4`。
3. **「重试恢复演示」**（预期终态 `succeeded`，2 个节点运行）：`run_flaky(failures_before_success=1)`
   第一次调用失败 → 决策选 `retry` → 同一事务内 attempt 1 记 `failed`
   （`error_resolution.selected_action = "retry"`）、为同一节点运行插入 attempt 2
   （`retry_of_job_uuid`、`trigger=retry_decision`）、节点运行回到 `pending` → 第二次调用成功
   （`calls=2`）成为该节点的当前结果（`attempt_count=2`，历史里保留 attempt 1）→ `stats` 报告
   `attempts=7, failures=5`。

## 手动启动

```bash
python -m unilabos --backend hostlink --skip_env_check \
  --devices ./exception_demo --external_devices_only \
  --visual disable --disable_browser \
  -g ./graph/exception_demo.json

python -m unilabos --backend ros2 --disable_hostlink --skip_env_check \
  --devices ./exception_demo --external_devices_only \
  --visual disable --disable_browser \
  -g ./graph/exception_demo.json
```

然后打开管理页面（或直接调上面的 API）：运行「异常传播演示」，在错误决策里看到待决策项，
选择一个决策。

## 默认子工作流与错误决策链

`exception_demo/workflows.py` 用主仓的 `@workflow` 装饰器声明了两条工作流。host 启动时
AST 扫描发现该模块，按函数相对路径派生稳定 uuid 幂等上报到本机 Workflow Authority。
`run_template("exception_supervisor_demo/…")` 按类名解析唯一的监督器实例；
`run("fault_injector/…")` 按实例 id 指定故障注入器。未配置 `error_policy` 的动作失败进入
统一决策链：`abort` 放行失败结果，`operator_intervention` 用人工提供的结果替换，`retry` 把失败
attempt 记录下来并把同一节点运行作为新 attempt 重新下发。`GET /api/v1/workflow-tasks/{uuid}/node-runs`
是节点级视图（当前结果 + `attempts` 历史）；`GET /api/v1/workflow-tasks/{uuid}/jobs` 平铺列出
attempt，其 `job_uuid` 与错误决策报文里的 `job_id` 一致。

## 目录

```text
graph/exception_demo.json          两种 backend 共用的一份图
exception_demo/
  fault_injector.py                注入故障的目标设备（run_step/run_guarded/run_flaky/stats）
  supervisor.py                    probe_remote_failure：点对点调用并在调用侧捕获远端异常
  workflows.py                     @workflow「异常传播演示」（failed）、「人工替换恢复演示」与「重试恢复演示」（succeeded）
  smoke.py                         经管理 API 驱动的有终止条件真实运行时证明
tests/test_hostlink_smoke.py       HostLink 集成断言
```
