"""异常演示默认子工作流：全部经网页/管理 API 的工作流提交路径运行。

host 启动时由主仓 AST 扫描发现本模块（@workflow），import 后按稳定 uuid
幂等上报到本机 Workflow Authority；网页（或 ``POST /api/v1/workflow-tasks``）
创建任务后，失败的 attempt 进入错误决策链（``GET /api/v1/error-decisions``），
由决策放行——「任务失败」是显式决策结果。

三条工作流覆盖三种异常形态与三种决策：

1. 「异常传播演示」（预期终态 failed）：
   预热成功 -> 监督器点对点调用并在调用侧捕获远端异常（job 成功，异常在返回值）
   -> 驱动内部业务级捕获（job 成功，错误在返回值）-> 异常穿出动作边界（job failed，
   决策 ``abort`` 放行，任务 failed）。
2. 「人工替换恢复演示」（预期终态 succeeded）：
   注入失败 -> 决策 ``operator_intervention`` 提供替代结果（job 以
   ``suc_type=operator_intervention`` 成功）-> 统计仍可服务 -> 任务 succeeded。
3. 「重试恢复演示」（预期终态 succeeded）：
   瞬时故障 -> 决策 ``retry``：失败 attempt 如实落表为 failed，调度器为同一节点追加
   attempt 2 重新下发（任务不中断）-> 第二次调用成功 -> 统计 -> 任务 succeeded。
"""

from unilabos.registry.workflows import WorkflowBuildContext, WorkflowGuide, workflow

#: smoke/测试按显示名检索上报结果，保持单一出处。
FAILURE_WORKFLOW_NAME = "异常传播演示"
RECOVERY_WORKFLOW_NAME = "人工替换恢复演示"
RETRY_WORKFLOW_NAME = "重试恢复演示"

_DECISION_NOTE = "失败的 attempt 不会自动结束任务：它停在「异常审批」页等你决策，决策前任务一直是运行中。"


@workflow(
    display_name=FAILURE_WORKFLOW_NAME,
    description="预热成功 -> 调用侧捕获远端异常 -> 业务级捕获 -> 注入失败终止任务（预期终态 failed）",
    tags=["exception-demo", "error-propagation"],
    guide=WorkflowGuide(
        preparation=[
            "「设备」页确认故障注入器 fault_injector 与监督器 exception_supervisor 在线。",
            "无需准备物料。第二步的角色是设备类 exception_supervisor_demo：图里只有一台监督器时插入模板会自动选中。",
            "运行后打开「异常审批」页等待第四步的失败出现。",
        ],
        expected=[
            "前三步 succeeded：远端异常被调用侧捕获、业务级失败被驱动内部守卫——两者都体现在返回值里，job 本身是成功的。",
            "第四步 failed 后出现在「异常审批」：选「终止（abort）」放行，任务终态 failed。",
            "「任务运行时」里能看到三种形态的区别：成功 / 返回值里带错误 / attempt failed。",
        ],
        notes=[_DECISION_NOTE, "这是一条预期以 failed 收尾的模板。"],
    ),
)
def failure_propagation(ctx: WorkflowBuildContext) -> None:
    """四步串行：成功、点对点捕获、受护捕获、抛异常导致任务失败。"""

    ctx.run(
        "fault_injector/run_step",
        {"step_name": "warmup", "fail": False},
        name="预热成功",
        description="一次正常的动作调用，作为对照。",
    )
    # 监督器类在图中只有一个实例：run_template 按类名自动填充 device_id。
    ctx.run_template(
        "exception_supervisor_demo/probe_remote_failure",
        {"step_name": "explode", "message": "injected-failure"},
        name="调用侧捕获远端异常",
        description="监督器点对点 call_device_action 调注入器并让它抛异常；异常在调用侧被捕获，作为返回值带回，job 成功。",
    )
    ctx.run(
        "fault_injector/run_guarded",
        {"fail": True, "message": "guarded-failure"},
        name="业务级捕获",
        description="驱动内部的业务级守卫捕获失败并返回错误信息，job 成功。",
    )
    ctx.run(
        "fault_injector/run_step",
        {"step_name": "final", "fail": True, "message": "injected-failure"},
        name="注入失败",
        description="异常穿出动作边界：attempt failed，进入「异常审批」等待决策；选 abort 让任务以 failed 结束。",
    )


@workflow(
    display_name=RECOVERY_WORKFLOW_NAME,
    description="注入失败 -> 决策链人工替换结果放行 -> 统计仍可服务（预期终态 succeeded）",
    tags=["exception-demo", "operator-intervention"],
    guide=WorkflowGuide(
        preparation=[
            "「设备」页确认 fault_injector 在线；无需准备物料。",
            "运行后打开「异常审批」页：第一步注入的失败会在那里等待决策。",
        ],
        expected=[
            "第一步 attempt failed → 在「异常审批」选「人工替换结果（operator_intervention）」并填一个替代结果放行。",
            "该 job 以 suc_type=operator_intervention 成功，第二步「故障后统计」照常执行，任务终态 succeeded。",
        ],
        notes=[_DECISION_NOTE],
    ),
)
def operator_recovery(ctx: WorkflowBuildContext) -> None:
    """失败 attempt 由人工替换结果放行后，任务继续并成功结束。"""

    ctx.run(
        "fault_injector/run_step",
        {"step_name": "flaky", "fail": True, "message": "transient-failure"},
        name="注入失败待人工处理",
        description="注入一次失败；在「异常审批」里用人工替换结果放行。",
    )
    ctx.run(
        "fault_injector/stats",
        {},
        name="故障后统计",
        description="读注入器的调用统计，证明决策后设备仍可服务。",
    )


@workflow(
    display_name=RETRY_WORKFLOW_NAME,
    description="瞬时故障 -> 决策链 retry 为同一节点追加新 attempt -> 重跑成功 -> 统计（预期终态 succeeded）",
    tags=["exception-demo", "retry"],
    guide=WorkflowGuide(
        preparation=[
            "「设备」页确认 fault_injector 在线；无需准备物料。",
            "运行后打开「异常审批」页：第一步的瞬时故障会在那里等待决策。",
        ],
        expected=[
            "第一步 attempt 1 failed → 在「异常审批」选「重试（retry）」。",
            "调度器为同一节点追加 attempt 2 重新下发，第二次调用成功；「任务运行时」的节点运行里能看到两个 attempt。",
            "「重试后统计」执行，任务终态 succeeded。",
        ],
        notes=[_DECISION_NOTE],
    ),
)
def retry_recovery(ctx: WorkflowBuildContext) -> None:
    """第一次调用注入故障、retry 后第二次成功；两个 attempt 都留在 job 表里。"""

    ctx.run(
        "fault_injector/run_flaky",
        {"step_name": "flaky-retry", "failures_before_success": 1, "message": "transient-failure"},
        name="瞬时故障后重试",
        description="第一次调用注入故障（failures_before_success=1），retry 后第二次调用成功。",
    )
    ctx.run(
        "fault_injector/stats",
        {},
        name="重试后统计",
        description="读注入器的调用统计（应包含失败一次、成功一次）。",
    )
