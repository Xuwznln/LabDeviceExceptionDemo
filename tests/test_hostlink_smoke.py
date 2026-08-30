from __future__ import annotations

from exception_demo.smoke import (
    assert_smoke_proof,
    assert_workflow_proof,
    run_smoke,
)


def test_real_exception_hostlink_smoke() -> None:
    proof = run_smoke("hostlink", timeout=40.0)
    # 阶段一：跨设备异常捕获 + 业务级兜底 + 故障后服务可用
    assert_smoke_proof(proof, "hostlink")
    # 阶段二：默认子工作流预期失败传播（前两步成功，第三步 failed + error_info）
    assert_workflow_proof(proof["workflow"])
