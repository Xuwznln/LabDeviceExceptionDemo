# UniLabOS Exception Handling Demo

**English** | [中文](README_zh.md)

This external device package demonstrates the two exception propagation paths
in Uni-Lab-OS and where each one is caught:

- **Point-to-point calls** (`DeviceNode.call_device_action`): an exception
  raised by the remote action propagates straight back to the caller, who
  catches it with `try/except` — `supervisor` catches the `RuntimeError`
  injected by `fault_injector.run_step` and records the exception type and
  error text;
- **Scheduled jobs** (workflow nodes): a failed attempt does not immediately
  terminate the task. It is held in the Backend error-decision chain
  (retry / mark failed / operator replacement) and the task only reaches the
  `failed` terminal state after the decision releases it;
- **Business-level guard**: the driver catches internally and returns a
  structured error (`run_guarded`); both the action and the job count as
  succeeded, the error lives in the return value only;
- **Availability after faults**: injected exceptions do not break the device;
  the `stats` action keeps serving and reports honest counters.

## Install from GitHub

```bash
unilab package install https://github.com/Xuwznln/LabDeviceExceptionDemo --ref <commit-sha>
```

For local development:

```bash
git clone https://github.com/Xuwznln/LabDeviceExceptionDemo.git
cd LabDeviceExceptionDemo
python -m pip install -e .
```

No AK/SK and no cloud lab required.

## Terminating dual-runtime smoke

```bash
python -m exception_demo.smoke --backend hostlink --timeout 30
python -m exception_demo.smoke --backend ros2 --timeout 60
```

Stage one (closed-loop proof): `supervisor` remotely invokes the four
`fault_injector` actions and writes `proof.json` — `warmup` succeeds,
`explode` raises and the caller catches it (the error text faithfully carries
`injected-failure`), `run_guarded` returns a structured error, and `stats`
proves the device is still serving (3 attempts, 2 failures).

Stage two (workflow): the smoke runs the "异常传播演示" workflow through the
management HTTP API. After the third step injects a failure:

- `GET /api/v1/error-decisions` exposes the pending decision report
  (exception type, error text, options `retry` / `abort` /
  `operator_intervention`);
- `POST /api/v1/error-decisions/{decision_id}` selects `abort` to release the
  failed result;
- the task ends `failed`, the first two node jobs are `succeeded` (the guarded
  step's error lives in `return_info.return_value`), and the third job is
  `failed` with a populated `error_info`.

## Manual start

```bash
python -m unilabos --backend hostlink --skip_env_check \
  --devices ./exception_demo --external_devices_only \
  --visual disable --disable_browser \
  -g ./graph/exception_demo.json

python -m unilabos --backend ros2 --disable_hostlink --skip_env_check \
  --devices ./exception_demo --external_devices_only \
  --visual disable --disable_browser \
  -g ./graph/exception_demo.json
```

## Default sub-workflow and the error-decision chain

`exception_demo/workflows.py` declares the "异常传播演示" workflow with the
core repo's `@workflow` decorator — three serial steps on the same device:

1. `run_step(fail=False)` — success baseline;
2. `run_guarded(fail=True)` — the exception is caught inside the driver, the
   job succeeds, the error lives in the return value;
3. `run_step(fail=True)` — the exception escapes the action boundary and the
   job fails.

At host startup the AST scan discovers the module and idempotently upserts it
into the local Workflow Authority under a stable uuid derived from the
function's relative path. Failures of actions without an `error_policy` enter
the unified Backend decision chain and wait for an `abort` / `retry` /
`operator_intervention` decision — so in this system "task failed" is an
explicit decision outcome, not an implicit timeout.

## Layout

```text
graph/exception_demo.json          one graph shared by both backends
exception_demo/
  fault_injector.py                fault-injecting target device (run_step/run_guarded/stats)
  supervisor.py                    cross-device caller that catches exceptions
  workflows.py                     @workflow default sub-workflow (expected to fail)
  smoke.py                         terminating real-runtime proof
tests/test_hostlink_smoke.py       HostLink integration assertions
```
