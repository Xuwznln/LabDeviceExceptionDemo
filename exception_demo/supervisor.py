"""异常监督器 — 演示跨设备调用侧如何捕获远端动作异常。

三段闭环（全部经共用 DeviceNode.call_device_action，不直接触碰驱动实例）：

1. 调 fault_injector 的 run_step(fail=False)：正常拿到返回值；
2. 调 run_step(fail=True)：远端抛 RuntimeError，动作失败——本设备 try/except
   捕获调用异常并记录错误文本（跨设备异常捕获）；
3. 调 run_guarded(fail=True)：远端驱动内部已捕获，动作成功返回结构化错误
   （业务级兜底，调用方无需 try/except）；
4. 调 stats()：验证故障后目标设备仍在服务。

结果写入 EXCEPTION_DEMO_PROOF_FILE 指定的终态 JSON，供有限时 smoke 断言。
"""

import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Optional

from unilabos.registry.decorators import device, not_action, topic_config

#: 目标设备节点 id（需与 graph 中故障注入器的 node id 一致）。
FAULT_DEVICE_ID = "fault_injector"


@device(
    id="exception_supervisor_demo",
    display_name="异常监督器",
    category=["virtual_device"],
    description="跨设备调用故障注入器，演示远端异常捕获与业务级兜底两条路径",
    supported_backends=["hostlink", "ros2"],
)
class ExceptionSupervisorDemo:
    """依次驱动成功/异常/受护三类远端动作并写出终态证明。"""

    run_in_test_mode = True

    def __init__(
        self,
        device_id: Optional[str] = None,
        fault_device: str = FAULT_DEVICE_ID,
        **kwargs: Any,
    ) -> None:
        """初始化异常监督器。

        Args:
            device_id[设备ID]: 设备实例 ID，默认 exception_supervisor_demo。
            fault_device[目标设备ID]: 故障注入器的节点 ID，默认 fault_injector。
        """
        self.device_id = device_id or "exception_supervisor_demo"
        self._fault_device = (fault_device or FAULT_DEVICE_ID).strip()
        self.logger = logging.getLogger(f"ExceptionSupervisor.{self.device_id}")
        self._start_time = time.time()
        self._phase: str = "idle"

    @not_action
    def post_init(self, node: Any) -> None:
        self._device_node = node
        proof_file = os.environ.get("EXCEPTION_DEMO_PROOF_FILE", "").strip()
        if proof_file:
            threading.Thread(
                target=self._run_proof,
                args=(Path(proof_file),),
                name="exception-demo-proof",
                daemon=True,
            ).start()

    @property
    @topic_config(period=1.0)
    def heartbeat(self) -> int:
        """自启动以来的心跳秒数。"""
        return int(time.time() - self._start_time)

    @property
    @topic_config()
    def phase(self) -> str:
        """当前演示阶段：idle / running / done / failed。"""
        return self._phase

    @not_action
    def _call(self, action_name: str, arguments: dict) -> Any:
        return self._device_node.call_device_action(
            self._fault_device,
            action_name,
            arguments,
            server_wait_timeout=10.0,
            timeout=10.0,
        )

    @not_action
    def _run_proof(self, proof_file: Path) -> None:
        """在真实运行时中按序执行三类调用，并原子写出可机读终态。"""

        delay = float(os.environ.get("EXCEPTION_DEMO_START_DELAY", "1.0"))
        time.sleep(max(0.0, delay))
        self._phase = "running"
        try:
            # 1) 正常成功
            ok_step = self._call("run_step", {"step_name": "warmup", "fail": False})

            # 2) 远端抛异常 -> 动作失败 -> 调用方捕获（本演示的核心）
            caught: dict[str, Any] = {"caught": False, "error_type": "", "error_text": ""}
            try:
                self._call(
                    "run_step",
                    {"step_name": "explode", "fail": True, "message": "injected-failure"},
                )
            except Exception as exc:  # noqa: BLE001 - 捕获任意远端错误形态
                caught = {
                    "caught": True,
                    "error_type": type(exc).__name__,
                    "error_text": str(exc),
                }
                self.logger.info(f"[Supervisor] 已捕获远端动作异常: {exc}")

            # 3) 业务级兜底：远端已捕获，动作成功返回结构化错误
            guarded = self._call(
                "run_guarded", {"fail": True, "message": "guarded-failure"}
            )

            # 4) 故障后目标设备仍在服务
            stats = self._call("stats", {})

            proof = {
                "success": True,
                "backend": str(getattr(self._device_node, "backend_name", "unknown")),
                "fault_device": self._fault_device,
                "ok_step": ok_step,
                "caught": caught,
                "guarded": guarded,
                "stats": stats,
            }
            self._phase = "done"
        except Exception as exc:  # noqa: BLE001 - 演示用，报告任何失败
            self.logger.exception("异常演示闭环失败")
            proof = {
                "success": False,
                "backend": str(getattr(self._device_node, "backend_name", "unknown")),
                "error": f"{type(exc).__name__}: {exc}",
            }
            self._phase = "failed"
        proof_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = proof_file.with_suffix(proof_file.suffix + ".tmp")
        temporary.write_text(
            json.dumps(proof, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(proof_file)
