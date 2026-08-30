# UniLabOS 异常捕获演示

[English](README.md) | **中文**

这个外部设备包演示 Uni-Lab-OS 中异常的两条传播路径，以及它们各自的捕获点：

- **点对点调用**（`DeviceNode.call_device_action`）：远端动作抛出的异常直接
  传播回调用方，由调用方 `try/except` 捕获——`supervisor` 捕获
  `fault_injector.run_step` 注入的 `RuntimeError` 并留下异常类型与错误文本；
- **调度 job**（工作流节点）：失败的 attempt 不会立即终结任务，而是挂入
  Backend 错误决策链（重试 / 标记失败 / 人工替换结果），由决策放行后任务
  才进入 `failed` 终态；
- **业务级兜底**：驱动内部 `try/except` 捕获后返回结构化错误
  （`run_guarded`），动作与 job 都视为成功，错误只体现在返回值里；
- **故障后可用性**：注入异常不破坏设备，`stats` 动作继续服务并如实计数。

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
python -m exception_demo.smoke --backend hostlink --timeout 30
python -m exception_demo.smoke --backend ros2 --timeout 60
```

阶段一（闭环 proof）：`supervisor` 依次远程调用 `fault_injector` 的四个动作并写出
`proof.json`——`warmup` 成功、`explode` 抛异常被调用方捕获（错误文本保真携带
`injected-failure`）、`run_guarded` 返回结构化错误、`stats` 证明设备仍在服务
（3 次尝试 2 次失败）。

阶段二（工作流）：通过管理 HTTP API 真实运行「异常传播演示」工作流，
第三步注入失败后：

- `GET /api/v1/error-decisions` 出现该 job 的待决策报文（异常类型、错误文本、
  可选项 `retry` / `abort` / `operator_intervention`）；
- `POST /api/v1/error-decisions/{decision_id}` 选择 `abort` 放行失败结果；
- 任务终态 `failed`，前两个节点 job `succeeded`（受护步骤的错误在
  `return_info.return_value` 里），第三个 job `failed` 且带 `error_info`。

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

## 默认子工作流与错误决策链

`exception_demo/workflows.py` 用主仓的 `@workflow` 装饰器声明了「异常传播演示」，
同一设备三步串行：

1. `run_step(fail=False)`——成功基线；
2. `run_guarded(fail=True)`——异常在驱动内部被捕获，job 成功，错误在返回值里；
3. `run_step(fail=True)`——异常穿出动作边界，job 失败。

host 启动时 AST 扫描发现该模块，按函数相对路径派生稳定 uuid 幂等上报到本机
Workflow Authority。未配置 `error_policy` 的动作失败会进入统一的 Backend
决策链，等待 `abort` / `retry` / `operator_intervention` 决策——所以「任务失败」
在这套系统里是一个显式决策结果，而不是隐式超时。

## 目录

```text
graph/exception_demo.json          两种 backend 共用的一份图
exception_demo/
  fault_injector.py                注入故障的目标设备（run_step/run_guarded/stats）
  supervisor.py                    跨设备调用并捕获异常的监督设备
  workflows.py                     @workflow 默认子工作流（预期失败）
  smoke.py                         有终止条件的真实运行时证明
tests/test_hostlink_smoke.py       HostLink 集成断言
```
