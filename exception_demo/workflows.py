"""异常演示默认子工作流：全部经网页/管理 API 的工作流提交路径运行。

host 启动时由主仓 AST 扫描发现本模块（@workflow），import 后按稳定 uuid
幂等上报到本机 Workflow Authority；网页（或 ``POST /api/v1/workflow-tasks``）
创建任务后，失败的 attempt 进入错误决策链（``GET /api/v1/error-decisions``），
由决策放行——「任务失败」是显式决策结果。

两条工作流覆盖三种异常形态与两种决策：

1. 「异常传播演示」（预期终态 failed）：
   预热成功 -> 监督器点对点调用并在调用侧捕获远端异常（job 成功，异常在返回值）
   -> 驱动内部业务级捕获（job 成功，错误在返回值）-> 异常穿出动作边界（job failed，
   决策 ``abort`` 放行，任务 failed）。
2. 「人工替换恢复演示」（预期终态 succeeded）：
   注入失败 -> 决策 ``operator_intervention`` 提供替代结果（job 以
   ``suc_type=operator_intervention`` 成功）-> 统计仍可服务 -> 任务 succeeded。
"""

from unilabos.registry.workflows import WorkflowBuildContext, workflow

#: smoke/测试按显示名检索上报结果，保持单一出处。
FAILURE_WORKFLOW_NAME = "异常传播演示"
RECOVERY_WORKFLOW_NAME = "人工替换恢复演示"


@workflow(
    display_name=FAILURE_WORKFLOW_NAME,
    description="预热成功 -> 调用侧捕获远端异常 -> 业务级捕获 -> 注入失败终止任务（预期终态 failed）",
    tags=["exception-demo", "error-propagation"],
)
def failure_propagation(ctx: WorkflowBuildContext) -> None:
    """四步串行：成功、点对点捕获、受护捕获、抛异常导致任务失败。"""

    ctx.run(
        "fault_injector/run_step",
        {"step_name": "warmup", "fail": False},
        name="预热成功",
    )
    # 监督器类在图中只有一个实例：run_template 按类名自动填充 device_id。
    ctx.run_template(
        "exception_supervisor_demo/probe_remote_failure",
        {"step_name": "explode", "message": "injected-failure"},
        name="调用侧捕获远端异常",
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


@workflow(
    display_name=RECOVERY_WORKFLOW_NAME,
    description="注入失败 -> 决策链人工替换结果放行 -> 统计仍可服务（预期终态 succeeded）",
    tags=["exception-demo", "operator-intervention"],
)
def operator_recovery(ctx: WorkflowBuildContext) -> None:
    """失败 attempt 由人工替换结果放行后，任务继续并成功结束。"""

    ctx.run(
        "fault_injector/run_step",
        {"step_name": "flaky", "fail": True, "message": "transient-failure"},
        name="注入失败待人工处理",
    )
    ctx.run("fault_injector/stats", {}, name="故障后统计")
