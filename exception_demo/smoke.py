"""网页工作流提交模式的有限时 smoke：起真实运行时，经管理 HTTP API 提交工作流并做决策。

网页（edge UI）对本机 Workflow Authority 的操作就是下面这几个 HTTP 调用，本脚本
逐一复现，不在设备内部自跑任何闭环：

1. ``GET  /api/v1/workflows``                    找到 host 启动时上报的 @workflow 模板；
2. ``POST /api/v1/workflow-tasks``               创建任务（网页"运行"按钮）；
3. ``GET  /api/v1/error-decisions``              失败 attempt 的待决策报文出现；
4. ``POST /api/v1/error-decisions/{decision_id}`` 网页选择 ``abort``、``retry`` 或
   ``operator_intervention``（携带替代结果）放行；
5. ``GET  /api/v1/workflow-tasks/{uuid}`` / ``/node-runs``  断言任务终态与每个节点运行
   （当前 attempt 的结果 + ``attempts`` 历史）。

三条工作流：「异常传播演示」预期 failed（abort 放行）；「人工替换恢复演示」预期
succeeded（operator_intervention 提供替代结果后任务继续）；「重试恢复演示」预期
succeeded（retry：失败 attempt 保留在历史里，同一节点运行的 attempt 2 成为当前结果，任务不中断）。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Sequence

#: 与 exception_demo/workflows.py 保持一致（smoke 独立运行，不 import 设备包）。
FAILURE_WORKFLOW_NAME = "异常传播演示"
RECOVERY_WORKFLOW_NAME = "人工替换恢复演示"
RETRY_WORKFLOW_NAME = "重试恢复演示"

#: 人工替换结果：网页决策时由操作者填入，替代失败 attempt 的返回值。
OPERATOR_RESULT = {"success": True, "step_name": "flaky", "replaced_by": "operator"}

TERMINAL = {"succeeded", "failed"}


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------


def assert_failure_workflow(proof: dict[str, Any]) -> None:
    """「异常传播演示」：三种异常形态各在正确位置被捕获，abort 后任务 failed。"""

    assert proof["workflow_name"] == FAILURE_WORKFLOW_NAME
    assert proof["task_status"] == "failed", f"预期任务失败传播，实际: {proof}"

    decision = proof["decision"]
    assert decision["selected_action"] == "abort"
    assert decision["exception_type"] == "RuntimeError", f"决策报文异常类型: {decision}"
    assert "injected-failure" in decision["error_message"], f"决策报文错误文本: {decision}"
    assert {"retry", "abort", "operator_intervention"} <= set(decision["options"])
    assert decision["action_name"].endswith("run_step")
    assert decision["device_id"] == "fault_injector"

    runs = proof["node_runs"]
    assert len(runs) == 4, f"应有 4 个节点运行: {runs}"
    warmup, probe, guarded, final = runs
    assert all(run["attempt_count"] == 1 for run in runs), runs

    assert warmup["status"] == "succeeded"
    assert warmup["return_info"]["return_value"]["step_name"] == "warmup"

    # 点对点：远端异常回到调用方 try/except，节点成功，异常在返回值里
    assert probe["status"] == "succeeded"
    probe_value = probe["return_info"]["return_value"]
    assert probe_value["caught"] is True, probe_value
    assert probe_value["error_type"], probe_value
    assert "injected-failure" in probe_value["error_text"], probe_value

    # 业务级兜底：驱动内部捕获，节点成功，错误在返回值里
    assert guarded["status"] == "succeeded"
    guarded_value = guarded["return_info"]["return_value"]
    assert guarded_value["success"] is False
    assert guarded_value["caught"] == "RuntimeError"

    # 异常穿出动作边界：abort 放行 → 节点运行 failed 并留下 error_info，attempt 记录决策
    assert final["status"] == "failed"
    assert final["error_info"], f"失败节点缺少 error_info: {final}"
    (final_attempt,) = final["attempts"]
    assert final_attempt["status"] == "failed"
    assert final_attempt["error_resolution"]["selected_action"] == "abort", final_attempt


def assert_recovery_workflow(proof: dict[str, Any]) -> None:
    """「人工替换恢复演示」：operator_intervention 替换结果后 job 成功，任务继续并成功。"""

    assert proof["workflow_name"] == RECOVERY_WORKFLOW_NAME
    assert proof["task_status"] == "succeeded", f"预期任务成功，实际: {proof}"

    decision = proof["decision"]
    assert decision["selected_action"] == "operator_intervention"
    assert "transient-failure" in decision["error_message"], decision

    runs = proof["node_runs"]
    assert len(runs) == 2, f"应有 2 个节点运行: {runs}"
    replaced, stats = runs
    # 人工替换：同一 attempt 以替代结果成功放行，不产生新 attempt
    assert replaced["status"] == "succeeded" and replaced["attempt_count"] == 1
    assert replaced["return_info"]["suc_type"] == "operator_intervention", replaced
    assert replaced["return_info"]["return_value"] == OPERATOR_RESULT, replaced
    assert replaced["attempts"][0]["error_resolution"]["selected_action"] == "operator_intervention"

    # 前两条工作流共 5 次动作调用，其中 4 次注入故障（explode/guarded/final/flaky）
    stats_value = stats["return_info"]["return_value"]
    assert stats["status"] == "succeeded"
    assert stats_value["attempts"] == 5, stats_value
    assert stats_value["failures"] == 4, stats_value
    assert "transient-failure" in stats_value["last_error"], stats_value


def assert_retry_workflow(proof: dict[str, Any]) -> None:
    """「重试恢复演示」：节点运行的当前结果是重试后的成功，失败 attempt 留在历史里，任务不中断。"""

    assert proof["workflow_name"] == RETRY_WORKFLOW_NAME
    assert proof["task_status"] == "succeeded", f"预期 retry 后任务成功，实际: {proof}"

    decision = proof["decision"]
    assert decision["selected_action"] == "retry"
    assert decision["action_name"].endswith("run_flaky")
    assert "transient-failure" in decision["error_message"], decision
    assert decision["retry_count"] == 0 and decision["max_retries"] >= 1, decision

    runs = proof["node_runs"]
    assert len(runs) == 2, f"应有 2 个节点运行: {runs}"
    flaky, stats = runs
    # 节点运行本身：当前结果 = attempt 2 的成功结果
    assert flaky["status"] == "succeeded", flaky
    assert flaky["attempt_count"] == 2, flaky
    assert flaky["return_info"]["return_value"]["calls"] == 2, flaky
    assert flaky["return_info"]["return_value"]["recovered_after_failures"] == 1, flaky
    assert flaky["error_info"] == [], flaky
    # 历史：attempt 1 失败且记录 retry 决策，attempt 2 指回 attempt 1
    first, second = flaky["attempts"]
    assert first["attempt_no"] == 1 and first["status"] == "failed", first
    assert first["error_resolution"]["selected_action"] == "retry", first
    assert first["error_info"], first
    assert second["attempt_no"] == 2 and second["status"] == "succeeded", second
    assert second["retry_of_job_uuid"] == first["uuid"], second
    assert second["trigger"] == "retry_decision", second
    assert decision["node_run_uuid"] == flaky["uuid"], decision

    # 三条工作流累计 7 次调用、5 次故障（explode/guarded/final/flaky/flaky-retry#1）
    stats_value = stats["return_info"]["return_value"]
    assert stats["status"] == "succeeded" and stats["attempt_count"] == 1
    assert stats_value["attempts"] == 7, stats_value
    assert stats_value["failures"] == 5, stats_value


# ---------------------------------------------------------------------------
# 进程与 HTTP
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _stop(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _graph_path(repo_root: Path) -> Path:
    """优先读取 wheel 安装的数据文件，editable/source 模式回退到仓库 graph。"""

    installed = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "exception_demo"
        / "graph"
        / "exception_demo.json"
    )
    if installed.is_file():
        return installed
    source = repo_root / "graph" / "exception_demo.json"
    if source.is_file():
        return source
    raise FileNotFoundError("Exception demo graph 未随 distribution 安装")


def _base_command(
    repo_root: Path,
    database_root: Path,
    management_port: int,
    backend: str,
) -> list[str]:
    import unilabos

    config_path = (
        Path(unilabos.__file__).resolve().parent
        / "config"
        / "example_config.py"
    )
    command = [
        sys.executable,
        "-m",
        "unilabos",
        "--backend",
        backend,
        "--skip_env_check",
        "--devices",
        str(repo_root / "exception_demo"),
        "--external_devices_only",
        "--visual",
        "disable",
        "--disable_browser",
        "--port",
        str(management_port),
        "--server_database_root",
        str(database_root),
        "--working_dir",
        str(database_root / "work"),
        "--config",
        str(config_path),
        "-g",
        str(_graph_path(repo_root)),
    ]
    if backend == "ros2":
        command.append("--disable_hostlink")
    return command


def _api_request(
    port: int, path: str, payload: dict[str, Any] | None = None
) -> Any:
    """请求管理 API；workflow 风格 {"code":0,"data":...} 自动解包，诊断路由原样返回。"""

    url = f"http://127.0.0.1:{port}/api/v1{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    # GET 不能带 JSON Content-Type：服务端 Backend 路由会尝试解码空 body 而报错
    headers = {} if payload is None else {"Content-Type": "application/json"}
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        body = json.loads(response.read().decode("utf-8"))
    if isinstance(body, dict) and "code" in body:
        if body["code"] != 0:
            raise RuntimeError(f"管理 API {path} 返回错误: {body}")
        return body.get("data")
    return body


def _wait_management_api(port: int, process: subprocess.Popen[Any], deadline: float) -> None:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("runtime process exited before the management API came up")
        try:
            if _api_request(port, "/health").get("status") == "ok":
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)
    raise RuntimeError("管理 API 未在时限内就绪")


def _find_workflow(port: int, name: str, deadline: float) -> dict[str, Any]:
    while time.monotonic() < deadline:
        listing = _api_request(port, "/workflows?page=1&page_size=50")
        matches = [item for item in listing["items"] if item["name"] == name]
        if matches:
            return matches[0]
        time.sleep(0.3)
    raise RuntimeError(f"未在管理 API 检索到工作流 {name!r}")


def _resolve_decision(
    port: int, task_uuid: str, decision: dict[str, Any], deadline: float
) -> dict[str, Any]:
    """等待失败 attempt 进入错误决策链，按网页的选择放行并返回决策证据。"""

    while time.monotonic() < deadline:
        listing = _api_request(port, "/error-decisions")
        pending = [item for item in listing["items"] if item.get("task_id") == task_uuid]
        if pending:
            report = pending[0]
            resolved = _api_request(
                port,
                f"/error-decisions/{report['decision_id']}",
                {
                    **decision,
                    # 决策必须回带 job/device 完成三元校验
                    "job_id": report["job_id"],
                    "device_id": report["device_id"],
                },
            )
            assert resolved["status"] == "resolved", f"决策未被接受: {resolved}"
            return {
                "decision_id": report["decision_id"],
                "device_id": report.get("device_id", ""),
                "action_name": report.get("action_name", ""),
                "exception_type": report.get("exception_type", ""),
                "error_message": report.get("error_message", ""),
                "options": [str(option.get("action")) for option in report.get("options", [])],
                "retry_count": int(report.get("retry_count") or 0),
                "max_retries": int(report.get("max_retries") or 0),
                "node_run_uuid": str(report.get("node_run_uuid") or ""),
                "selected_action": decision["action"],
            }
        time.sleep(0.3)
    raise RuntimeError(f"任务 {task_uuid} 的错误决策未在时限内出现")


def run_workflow(
    port: int, name: str, decision: dict[str, Any], deadline: float
) -> dict[str, Any]:
    """检索工作流 -> 创建任务 -> 决策链放行 -> 等待终态 -> 汇总节点结果。"""

    workflow = _find_workflow(port, name, deadline)
    task = _api_request(
        port, "/workflow-tasks", {"workflow_uuid": workflow["uuid"], "run_mode": "normal"}
    )
    task_uuid = task["uuid"]
    resolved = _resolve_decision(port, task_uuid, decision, deadline)

    status = str(task.get("status") or "")
    while time.monotonic() < deadline and status not in TERMINAL:
        time.sleep(0.3)
        status = str(_api_request(port, f"/workflow-tasks/{task_uuid}").get("status") or "")
    if status not in TERMINAL:
        raise RuntimeError(f"工作流任务 {task_uuid} 未在时限内结束: {status}")

    node_runs = _api_request(port, f"/workflow-tasks/{task_uuid}/node-runs")
    return {
        "workflow_uuid": workflow["uuid"],
        "workflow_name": name,
        "task_uuid": task_uuid,
        "task_status": status,
        "decision": resolved,
        # 节点结果一律取节点运行（当前 attempt 的投影）；attempts 是该节点的执行历史
        "node_runs": [
            {
                "uuid": run["uuid"],
                "workflow_node_uuid": run["workflow_node_uuid"],
                "status": run["status"],
                "attempt_count": int(run.get("attempt_count") or 0),
                "return_info": dict(run.get("return_info") or {}),
                "error_info": list(run.get("error_info") or []),
                "attempts": [
                    {
                        "uuid": attempt["uuid"],
                        "attempt_no": int(attempt["attempt_no"]),
                        "trigger": attempt.get("trigger", ""),
                        "retry_of_job_uuid": attempt.get("retry_of_job_uuid"),
                        "status": attempt["status"],
                        "return_info": dict(attempt.get("return_info") or {}),
                        "error_info": list(attempt.get("error_info") or []),
                        "error_resolution": dict(attempt.get("error_resolution") or {}),
                    }
                    for attempt in run.get("attempts", [])
                ],
            }
            for run in node_runs
        ],
    }


def run_smoke(backend: str = "hostlink", timeout: float = 30.0) -> dict[str, Any]:
    """启动真实图，经管理 API 依次提交两条工作流并做决策，返回可机读证据。"""

    if backend not in {"hostlink", "ros2"}:
        raise ValueError("backend must be hostlink or ros2")
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix=f"exception-demo-{backend}-") as directory:
        root = Path(directory)
        log_path = root / "runtime.log"
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        management_port = _free_port()
        command = _base_command(repo_root, root / "db", management_port, backend)
        if backend == "hostlink":
            command += ["--hostlink_bind", "127.0.0.1", "--hostlink_port", str(_free_port())]
        else:
            domain_id = str(10 + management_port % 190)
            environment["ROS_DOMAIN_ID"] = domain_id
            command += ["--ros_domain_id", domain_id]

        with log_path.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                command,
                cwd=repo_root,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                deadline = time.monotonic() + timeout
                _wait_management_api(management_port, process, deadline)
                failure = run_workflow(
                    management_port,
                    FAILURE_WORKFLOW_NAME,
                    {"action": "abort", "reason": "exception-demo smoke 放行失败结果"},
                    deadline,
                )
                assert_failure_workflow(failure)
                recovery = run_workflow(
                    management_port,
                    RECOVERY_WORKFLOW_NAME,
                    {
                        "action": "operator_intervention",
                        "reason": "exception-demo smoke 人工替换结果",
                        "result": OPERATOR_RESULT,
                    },
                    deadline,
                )
                assert_recovery_workflow(recovery)
                retry = run_workflow(
                    management_port,
                    RETRY_WORKFLOW_NAME,
                    {"action": "retry", "reason": "exception-demo smoke 重试瞬时故障"},
                    deadline,
                )
                assert_retry_workflow(retry)
                return {
                    "success": True,
                    "backend": backend,
                    "failure_workflow": failure,
                    "recovery_workflow": recovery,
                    "retry_workflow": retry,
                }
            except Exception:
                sys.stderr.write(
                    "SMOKE FAILED\n"
                    + log_path.read_text(encoding="utf-8", errors="replace")
                    + "\n"
                )
                raise
            finally:
                _stop(process)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("hostlink", "ros2"), default="hostlink")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    print(json.dumps(run_smoke(args.backend, args.timeout), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
