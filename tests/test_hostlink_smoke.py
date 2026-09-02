from __future__ import annotations

from exception_demo.smoke import (
    assert_failure_workflow,
    assert_recovery_workflow,
    assert_retry_workflow,
    run_smoke,
)


def test_real_exception_hostlink_smoke() -> None:
    proof = run_smoke("hostlink", timeout=60.0)
    # 网页工作流提交路径：失败传播（abort 放行 -> failed）
    assert_failure_workflow(proof["failure_workflow"])
    # 网页工作流提交路径：人工替换结果放行 -> 任务继续并 succeeded
    assert_recovery_workflow(proof["recovery_workflow"])
    # 网页工作流提交路径：retry -> 失败 attempt 落表，同节点新 attempt 成功
    assert_retry_workflow(proof["retry_workflow"])
