"""有限时启动真实图，验证跨设备异常捕获，再运行预期失败的默认子工作流。

阶段一（闭环）：supervisor 依次调用 fault_injector 的成功/异常/受护/统计
四个动作，把捕获结果写出 proof.json。

阶段二（工作流）：host 启动时已把 exception_demo/workflows.py 里的 @workflow
幂等上报到本机 Workflow Authority；本脚本通过管理 HTTP API 找到它、创建任务。
第三步注入的异常会把 job 挂入 Backend 错误决策链（GET /error-decisions），
脚本断言决策报文（异常类型/错误文本/可选项）后选择「标记失败」放行，
最终断言任务终态 failed、前两步 succeeded、第三步 failed 且 error_info 非空。

两条异常路径的对照是本演示的核心：
- call_device_action 点对点调用：远端异常直接抛给调用方（阶段一）；
- 调度 job：失败先进入错误决策链，由决策放行/重试/人工替换（阶段二）。
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
WORKFLOW_DISPLAY_NAME = "异常传播演示"


def assert_smoke_proof(proof: dict[str, Any], backend: str) -> None:
    """对 HostLink/ROS2 共用的三类异常路径做同一组断言。"""

    assert proof.get("success") is True, f"smoke 未成功: {proof}"
    assert proof.get("backend") == backend, f"backend 不匹配: {proof}"
    assert proof["fault_device"] == "fault_injector"

    # 1) 正常成功路径
    ok_step = proof["ok_step"]
    assert ok_step["success"] is True
    assert ok_step["step_name"] == "warmup"

    # 2) 远端抛异常 -> 调用方捕获；错误文本保真携带注入的消息
    caught = proof["caught"]
    assert caught["caught"] is True, f"远端异常未被捕获: {caught}"
    assert caught["error_type"], f"缺少异常类型: {caught}"
    assert "injected-failure" in caught["error_text"], f"错误文本丢失: {caught}"

    # 3) 业务级兜底 -> 动作成功返回结构化错误
    guarded = proof["guarded"]
    assert guarded["success"] is False
    assert guarded["caught"] == "RuntimeError"
    assert "guarded-failure" in guarded["error"]

    # 4) 故障后设备仍在服务，计数与三次调用一致
    stats = proof["stats"]
    assert stats["success"] is True
    assert stats["attempts"] == 3
    assert stats["failures"] == 2
    assert "injected-failure" in stats["last_error"] or "guarded-failure" in stats["last_error"]


def assert_workflow_proof(workflow_proof: dict[str, Any]) -> None:
    """断言默认子工作流：决策链捕获失败节点，放行后任务整体 failed。"""

    assert workflow_proof["workflow_name"] == WORKFLOW_DISPLAY_NAME
    assert workflow_proof["task_status"] == "failed", (
        f"预期任务失败传播，实际: {workflow_proof}"
    )

    # 失败 attempt 先进入 Backend 错误决策链，报文携带异常与可选项
    decision = workflow_proof["decision"]
    assert decision["resolved_action"] == "abort"
    assert decision["exception_type"] == "RuntimeError", f"决策报文异常类型: {decision}"
    assert "injected-failure" in decision["error_message"], f"决策报文错误文本: {decision}"
    assert {"retry", "abort", "operator_intervention"} <= set(decision["options"])
    assert decision["action_name"].endswith("run_step")
    assert decision["device_id"] == "fault_injector"

    jobs = workflow_proof["jobs"]
    assert len(jobs) == 3, f"应有 3 个节点 job: {jobs}"

    # jobs 按 topological_index 返回；节点 uuid 序 == 声明序
    warmup_job, guarded_job, final_job = jobs
    assert warmup_job["status"] == "succeeded"
    assert warmup_job["return_info"]["return_value"]["step_name"] == "warmup"

    # 业务级捕获：job 成功，错误在返回值里
    assert guarded_job["status"] == "succeeded"
    guarded_value = guarded_job["return_info"]["return_value"]
    assert guarded_value["success"] is False
    assert guarded_value["caught"] == "RuntimeError"

    # 异常穿出动作边界：job 失败并留下 error_info
    assert final_job["status"] == "failed"
    assert final_job["error_info"], f"失败 job 缺少 error_info: {final_job}"


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


# ---------------------------------------------------------------------------
# 管理 HTTP API（工作流阶段）
# ---------------------------------------------------------------------------


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


def _resolve_failure_decision(
    management_port: int, task_uuid: str, deadline: float
) -> dict[str, Any]:
    """等待失败 attempt 进入错误决策链，选择「标记失败」放行并返回决策证据。"""

    while time.monotonic() < deadline:
        listing = _api_request(management_port, "/error-decisions")
        pending = [
            item
            for item in listing["items"]
            if item.get("task_id") == task_uuid
        ]
        if pending:
            report = pending[0]
            decision_id = report["decision_id"]
            resolved = _api_request(
                management_port,
                f"/error-decisions/{decision_id}",
                {
                    "action": "abort",
                    "reason": "exception-demo smoke 放行失败结果",
                    # 决策必须回带 job/device 完成三元校验
                    "job_id": report["job_id"],
                    "device_id": report["device_id"],
                },
            )
            assert resolved["status"] == "resolved", f"决策未被接受: {resolved}"
            return {
                "decision_id": decision_id,
                "device_id": report.get("device_id", ""),
                "action_name": report.get("action_name", ""),
                "exception_type": report.get("exception_type", ""),
                "error_message": report.get("error_message", ""),
                "options": [
                    str(option.get("action"))
                    for option in report.get("options", [])
                ],
                "resolved_action": "abort",
            }
        time.sleep(0.3)
    raise RuntimeError(f"任务 {task_uuid} 的错误决策未在时限内出现")


def run_workflow_stage(management_port: int, timeout: float) -> dict[str, Any]:
    """检索工作流 -> 创建任务 -> 决策链放行失败 -> 等待终态 -> 汇总节点结果。"""

    deadline = time.monotonic() + timeout

    workflow_uuid = ""
    while time.monotonic() < deadline:
        try:
            listing = _api_request(
                management_port, "/workflows?page=1&page_size=50"
            )
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
            continue
        matches = [
            item
            for item in listing["items"]
            if item["name"] == WORKFLOW_DISPLAY_NAME
        ]
        if matches:
            workflow_uuid = matches[0]["uuid"]
            break
        time.sleep(0.3)
    if not workflow_uuid:
        raise RuntimeError(
            f"{timeout}s 内未在管理 API 检索到工作流 {WORKFLOW_DISPLAY_NAME!r}"
        )

    task = _api_request(
        management_port,
        "/workflow-tasks",
        {"workflow_uuid": workflow_uuid, "run_mode": "normal"},
    )
    task_uuid = task["uuid"]

    # 第三步失败会先挂入错误决策链；选择 abort 放行 failed 结果
    decision = _resolve_failure_decision(management_port, task_uuid, deadline)

    status = str(task.get("status") or "")
    while time.monotonic() < deadline and status not in {"succeeded", "failed"}:
        time.sleep(0.3)
        current = _api_request(management_port, f"/workflow-tasks/{task_uuid}")
        status = str(current.get("status") or "")
    if status not in {"succeeded", "failed"}:
        raise RuntimeError(f"工作流任务 {task_uuid} 未在 {timeout}s 内结束: {status}")

    jobs = _api_request(management_port, f"/workflow-tasks/{task_uuid}/jobs")
    return {
        "workflow_uuid": workflow_uuid,
        "workflow_name": WORKFLOW_DISPLAY_NAME,
        "task_uuid": task_uuid,
        "task_status": status,
        "decision": decision,
        # task 级 output 不进公开 HTTP 契约，节点结果一律取 job.return_info
        "jobs": [
            {
                "uuid": job["uuid"],
                "status": job["status"],
                "return_info": dict(job.get("return_info") or {}),
                "error_info": list(job.get("error_info") or []),
            }
            for job in jobs
        ],
    }


def run_smoke(
    backend: str = "hostlink",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """启动真实图，等待异常闭环 proof，再运行预期失败的工作流。"""

    if backend not in {"hostlink", "ros2"}:
        raise ValueError("backend must be hostlink or ros2")
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(
        prefix=f"exception-demo-{backend}-"
    ) as directory:
        root = Path(directory)
        proof_path = root / "proof.json"
        log_path = root / "runtime.log"
        environment = os.environ.copy()
        environment.update(
            {
                "EXCEPTION_DEMO_PROOF_FILE": str(proof_path),
                "EXCEPTION_DEMO_START_DELAY": (
                    "2.0" if backend == "ros2" else "0.2"
                ),
                "PYTHONUNBUFFERED": "1",
            }
        )
        hostlink_port = _free_port()
        management_port = _free_port()
        command = _base_command(
            repo_root,
            root / "db",
            management_port,
            backend,
        )
        if backend == "hostlink":
            command += [
                "--hostlink_bind",
                "127.0.0.1",
                "--hostlink_port",
                str(hostlink_port),
            ]
        else:
            domain_id = str(10 + hostlink_port % 190)
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
                proof: dict[str, Any] | None = None
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if proof_path.is_file():
                        proof = json.loads(
                            proof_path.read_text(encoding="utf-8")
                        )
                        if proof.get("success") is not True:
                            raise RuntimeError(
                                f"{backend} smoke failed: {proof}\n"
                                + log_path.read_text(
                                    encoding="utf-8", errors="replace"
                                )
                            )
                        assert_smoke_proof(proof, backend)
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                if proof is None:
                    raise RuntimeError(
                        f"{backend} smoke did not complete within {timeout}s\n"
                        + log_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    )

                # 阶段二：上报结果已在启动时完成，这里检索并真实运行工作流
                try:
                    proof["workflow"] = run_workflow_stage(
                        management_port, timeout
                    )
                    assert_workflow_proof(proof["workflow"])
                except Exception:
                    sys.stderr.write(
                        "WORKFLOW STAGE FAILED\n"
                        + log_path.read_text(encoding="utf-8", errors="replace")
                        + "\n"
                    )
                    raise
                return proof
            finally:
                _stop(process)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        choices=("hostlink", "ros2"),
        default="hostlink",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            run_smoke(args.backend, args.timeout),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
