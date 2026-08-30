"""异常演示默认子工作流：失败节点如何终止任务并留下错误信息。

host 启动时由主仓 AST 扫描发现本模块（@workflow），import 后按稳定 uuid
幂等上报到本机 Workflow Authority，前端/HTTP 可直接引用运行。

三步全部显式指向 fault_injector（同一设备，调度器保证串行）：
1. 预热成功（job succeeded）；
2. 受护执行注入故障——驱动内部捕获，job 仍 succeeded，错误在返回值里；
3. 执行步骤注入故障——异常穿出动作边界，job failed，任务整体 failed，
   错误进入 job 的 error_info。

这条工作流的预期终态就是 failed；smoke 以此验证失败传播链路。
"""

from unilabos.registry.workflows import WorkflowBuildContext, workflow

#: smoke/测试按显示名检索上报结果，保持单一出处。
FAILURE_WORKFLOW_NAME = "异常传播演示"


@workflow(
    display_name=FAILURE_WORKFLOW_NAME,
    description="预热成功 -> 业务级捕获 -> 注入失败终止任务（预期终态 failed）",
    tags=["exception-demo", "error-propagation"],
)
def failure_propagation(ctx: WorkflowBuildContext) -> None:
    """同一设备三步串行：成功、受护捕获、抛异常导致任务失败。"""

    ctx.run(
        "fault_injector/run_step",
        {"step_name": "warmup", "fail": False},
        name="预热成功",
    )
    ctx.run(
        "fault_injector/run_guarded",
        {"fail": True, "message": "guarded-failure"},
        name="业务级捕获",
    )
    ctx.run(
        "fault_injector/run_step",
        {"step_name": "final", "fail": True, "message": "injected-failure"},
        name="注入失败",
    )
