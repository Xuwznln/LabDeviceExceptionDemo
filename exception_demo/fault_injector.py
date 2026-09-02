"""故障注入器 — 演示设备动作异常的三种形态。

职责单一：按参数决定成功、抛异常或业务级捕获，并累计统计，
供监督设备与工作流验证异常传播链路。

- ``run_step(fail=True)``：直接 ``raise RuntimeError``——异常穿出动作边界，
  由运行时标记该动作/job 失败，远端调用方会收到错误；
- ``run_guarded(fail=True)``：驱动内部 try/except 捕获——动作本身成功返回，
  错误以结构化字段交还调用方（业务级兜底）；
- ``stats()``：故障后设备仍在服务，可随时查询累计计数。
"""

import logging
import time
from typing import Any, Dict, Optional

from typing_extensions import TypedDict

from unilabos.registry.decorators import action, device, not_action, topic_config


class GuardedResult(TypedDict):
    """run_guarded 返回类型：异常被驱动捕获后的结构化描述。"""

    success: bool
    caught: str
    error: str


@device(
    id="fault_injector_demo",
    display_name="故障注入器",
    category=["virtual_device"],
    description="按参数成功/抛异常/业务级捕获的演示设备，统计动作与故障次数",
    supported_backends=["hostlink", "ros2"],
)
class FaultInjectorDemo:
    """按 fail 参数注入 RuntimeError 的演示设备。"""

    run_in_test_mode = True

    def __init__(self, device_id: Optional[str] = None, **kwargs: Any) -> None:
        """初始化故障注入器。

        Args:
            device_id[设备ID]: 设备实例 ID，默认 fault_injector_demo。
        """
        self.device_id = device_id or "fault_injector_demo"
        self.logger = logging.getLogger(f"FaultInjector.{self.device_id}")
        self._start_time = time.time()
        self._attempts: int = 0
        self._failures: int = 0
        self._last_error: str = ""
        # step_name -> 已被调用次数（run_flaky 用它决定"前 N 次失败、之后成功"）
        self._flaky_calls: Dict[str, int] = {}

    @not_action
    def post_init(self, node: Any) -> None:
        self._device_node = node

    @not_action
    def _inject(self, step_name: str, message: str) -> None:
        """统一的故障注入点：记录后抛 RuntimeError。"""

        self._failures += 1
        self._last_error = f"{step_name}: {message}"
        raise RuntimeError(self._last_error)

    # ============ 周期上报的状态 ============

    @property
    @topic_config(period=1.0)
    def heartbeat(self) -> int:
        """自启动以来的心跳秒数。"""
        return int(time.time() - self._start_time)

    @property
    @topic_config()
    def attempt_count(self) -> int:
        """已执行的 run_step/run_guarded 总次数。"""
        return self._attempts

    @property
    @topic_config()
    def failure_count(self) -> int:
        """已注入的故障总次数。"""
        return self._failures

    @property
    @topic_config()
    def last_error(self) -> str:
        """最近一次注入的错误文本。"""
        return self._last_error

    # ============ 动作 ============

    @action(
        display_name="执行步骤",
        description="fail=True 时抛 RuntimeError（异常穿出动作边界，动作/job 失败）",
        always_free=True,
        feedback_interval=1.0,
    )
    def run_step(
        self, step_name: str = "step", fail: bool = False, message: str = "injected-failure"
    ) -> Dict[str, Any]:
        """执行一个可注入故障的步骤。

        Args:
            step_name[步骤名]: 步骤标识，将回显在返回值/错误文本中。
            fail[注入故障]: 为 True 时抛出 RuntimeError。
            message[错误文本]: 注入故障时的错误消息。
        """
        self._attempts += 1
        if fail:
            self.logger.warning(f"[FaultInjector] {step_name} 注入故障: {message}")
            self._inject(step_name, message)
        self.logger.info(f"[FaultInjector] {step_name} 成功")
        return {"success": True, "step_name": step_name, "attempt": self._attempts}

    @action(
        display_name="受护执行",
        description="驱动内部 try/except 捕获注入的异常，动作总是成功返回结构化错误",
        always_free=True,
        feedback_interval=1.0,
    )
    def run_guarded(
        self, fail: bool = False, message: str = "guarded-failure"
    ) -> GuardedResult:
        """业务级兜底：异常在驱动内捕获，调用方拿到结构化错误而非失败。

        Args:
            fail[注入故障]: 为 True 时在内部注入并捕获 RuntimeError。
            message[错误文本]: 注入故障时的错误消息。
        """
        self._attempts += 1
        try:
            if fail:
                self._inject("guarded", message)
            return {"success": True, "caught": "", "error": ""}
        except RuntimeError as exc:
            self.logger.info(f"[FaultInjector] 业务级捕获: {exc}")
            return {"success": False, "caught": type(exc).__name__, "error": str(exc)}

    @action(
        display_name="不稳定步骤",
        description="同一 step_name 的前 N 次调用注入故障、之后成功——供 retry 决策演示：失败 attempt 落表，新 attempt 重跑成功",
        always_free=True,
        feedback_interval=1.0,
    )
    def run_flaky(
        self,
        step_name: str = "flaky",
        failures_before_success: int = 1,
        message: str = "transient-failure",
    ) -> Dict[str, Any]:
        """模拟瞬时故障：按 step_name 计数，前 N 次抛 RuntimeError，之后成功。

        Args:
            step_name[步骤名]: 计数键；同一步骤被 retry 重跑时计数累加。
            failures_before_success[失败次数]: 前多少次调用注入故障。
            message[错误文本]: 注入故障时的错误消息。
        """
        self._attempts += 1
        calls = self._flaky_calls.get(step_name, 0) + 1
        self._flaky_calls[step_name] = calls
        if calls <= int(failures_before_success):
            self.logger.warning(
                f"[FaultInjector] {step_name} 第 {calls} 次调用注入瞬时故障: {message}"
            )
            self._inject(step_name, f"{message} (call {calls}/{failures_before_success})")
        self.logger.info(f"[FaultInjector] {step_name} 第 {calls} 次调用成功")
        return {
            "success": True,
            "step_name": step_name,
            "calls": calls,
            "recovered_after_failures": calls - 1,
        }

    @action(
        display_name="查询统计",
        description="返回累计动作/故障计数（验证故障后设备仍在服务）",
        always_free=True,
        feedback_interval=1.0,
    )
    def stats(self) -> Dict[str, Any]:
        """查询累计统计。"""
        return {
            "success": True,
            "attempts": self._attempts,
            "failures": self._failures,
            "last_error": self._last_error,
        }
