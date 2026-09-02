"""异常监督器 — 把"点对点调用侧捕获远端异常"暴露成一个可编排的动作。

演示以网页工作流提交为主路径：工作流节点调用本设备的 ``probe_remote_failure``，
动作内部经共用 ``DeviceNode.call_device_action`` 调用 ``fault_injector.run_step(fail=True)``；
远端抛出的 ``RuntimeError`` 直接传播回本设备，由 ``try/except`` 捕获后作为结构化
返回值交还——因此这个 job 是 **成功** 的，异常只体现在返回值里。

与之对照：工作流直接调用 ``fault_injector.run_step(fail=True)`` 的节点会让异常穿出
动作边界，job 失败并进入错误决策链（见 workflows.py）。
"""

import logging
import time
from typing import Any, Dict, Optional

from unilabos.registry.decorators import action, device, not_action, topic_config

#: 目标设备节点 id（需与 graph 中故障注入器的 node id 一致）。
FAULT_DEVICE_ID = "fault_injector"


@device(
    id="exception_supervisor_demo",
    display_name="异常监督器",
    category=["virtual_device"],
    description="跨设备调用故障注入器并在调用侧捕获远端异常，作为工作流节点演示点对点异常传播",
    supported_backends=["hostlink", "ros2"],
)
class ExceptionSupervisorDemo:
    """在调用侧捕获远端动作异常的监督设备。"""

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
        self._probes: int = 0
        self._last_probe: str = ""

    @not_action
    def post_init(self, node: Any) -> None:
        self._device_node = node

    @property
    @topic_config(period=1.0)
    def heartbeat(self) -> int:
        """自启动以来的心跳秒数。"""
        return int(time.time() - self._start_time)

    @property
    @topic_config()
    def probe_count(self) -> int:
        """已执行的远端探测次数。"""
        return self._probes

    @property
    @topic_config()
    def last_probe(self) -> str:
        """最近一次探测的结果摘要。"""
        return self._last_probe

    @not_action
    def _call(self, action_name: str, arguments: dict) -> Any:
        return self._device_node.call_device_action(
            self._fault_device,
            action_name,
            arguments,
            server_wait_timeout=10.0,
            timeout=10.0,
        )

    @action(
        display_name="探测远端异常",
        description="点对点调用 fault_injector.run_step(fail=True)，远端异常由本设备捕获并作为返回值交还（本 job 成功）",
        always_free=True,
        feedback_interval=1.0,
    )
    def probe_remote_failure(
        self, step_name: str = "explode", message: str = "injected-failure"
    ) -> Dict[str, Any]:
        """跨设备调用一个注定失败的远端动作，并在调用侧捕获异常。

        Args:
            step_name[步骤名]: 传给远端 run_step 的步骤标识。
            message[错误文本]: 远端注入故障时的错误消息，用于核对异常文本保真。
        """
        self._probes += 1
        try:
            self._call(
                "run_step", {"step_name": step_name, "fail": True, "message": message}
            )
        except Exception as exc:  # noqa: BLE001 - 捕获任意远端错误形态
            self._last_probe = f"{type(exc).__name__}: {exc}"
            self.logger.info(f"[Supervisor] 已捕获远端动作异常: {exc}")
            return {
                "success": True,
                "caught": True,
                "target_device": self._fault_device,
                "error_type": type(exc).__name__,
                "error_text": str(exc),
            }
        self._last_probe = "远端未抛出异常"
        return {
            "success": False,
            "caught": False,
            "target_device": self._fault_device,
            "error_type": "",
            "error_text": "",
        }
