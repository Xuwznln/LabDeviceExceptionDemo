# Uni-Lab-OS Exception Handling Demo

**English** | [中文](README_zh.md)

This external device package demonstrates how exceptions propagate in
Uni-Lab-OS **through the web workflow submission path**: a workflow is
created the way the web UI does it (`POST /api/v1/workflow-tasks`), a failed
attempt shows up in the error-decision chain (`GET /api/v1/error-decisions`),
and the operator's decision (`POST /api/v1/error-decisions/{id}`) determines
the outcome. Nothing runs by itself inside the devices; every path is a
workflow node:

- **Exception escapes the action boundary** (`fault_injector.run_step(fail=True)`):
  the job fails and is held in the error-decision chain until a decision
  (`retry` / `abort` / `operator_intervention`) releases it — "task failed" is
  an explicit decision outcome, not an implicit timeout;
- **Point-to-point call, caught by the caller** (`supervisor.probe_remote_failure`):
  the supervisor device calls `fault_injector.run_step(fail=True)` through
  `DeviceNode.call_device_action`; the remote `RuntimeError` propagates straight
  back and is caught by its `try/except`, so the job *succeeds* and the
  exception lives in the return value;
- **Business-level guard** (`fault_injector.run_guarded`): the driver catches
  internally and returns a structured error; the job succeeds;
- **Operator replacement**: choosing `operator_intervention` with a replacement
  result marks the failed attempt as succeeded (`suc_type=operator_intervention`)
  and the task continues;
- **Availability after faults**: `stats` keeps serving and reports honest counters.

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
python -m exception_demo.smoke --backend hostlink --timeout 40
python -m exception_demo.smoke --backend ros2 --timeout 60
```

The smoke boots the real runtime (`unilab -g graph/exception_demo.json`, which
also reports the `@workflow` templates to the local Workflow Authority) and then
replays exactly what the web UI does through the management HTTP API:

1. **"异常传播演示"** (expected terminal state `failed`, 4 jobs):
   `run_step(warmup)` succeeds → `supervisor.probe_remote_failure` catches the
   remote `RuntimeError` on the caller side (job `succeeded`, `caught: true`
   with the faithful `injected-failure` text) → `run_guarded(fail=True)`
   returns a structured error (job `succeeded`) → `run_step(final, fail=True)`
   escapes; the pending decision report (exception type, error text, options
   `retry` / `abort` / `operator_intervention`) is resolved with `abort`; the
   job ends `failed` with `error_info` and the task ends `failed`.
2. **"人工替换恢复演示"** (expected terminal state `succeeded`, 2 jobs):
   `run_step(flaky, fail=True)` fails → the decision is resolved with
   `operator_intervention` carrying a replacement `result` → the job is released
   as `succeeded` with `suc_type=operator_intervention` and the replacement
   value → `stats` runs and reports `attempts=5, failures=4`.

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

Then open the management UI (or call the API above): run "异常传播演示", watch
the decision appear under error decisions, and pick a decision.

## Default sub-workflows and the error-decision chain

`exception_demo/workflows.py` declares both workflows with the core repo's
`@workflow` decorator. At host startup the AST scan discovers the module and
idempotently upserts them into the local Workflow Authority under stable uuids
derived from the functions' relative paths. `run_template("exception_supervisor_demo/…")`
resolves the single supervisor instance by class; `run("fault_injector/…")`
addresses the fault injector by instance id. Failures of actions without an
`error_policy` enter the unified decision chain; `retry` creates a new attempt
on the Backend side (the local scheduler releases the failed attempt), `abort`
releases the failure, `operator_intervention` replaces the result.

## Layout

```text
graph/exception_demo.json          one graph shared by both backends
exception_demo/
  fault_injector.py                fault-injecting target device (run_step/run_guarded/stats)
  supervisor.py                    probe_remote_failure: point-to-point call that catches the remote exception
  workflows.py                     @workflow "异常传播演示" (failed) and "人工替换恢复演示" (succeeded)
  smoke.py                         terminating real-runtime proof driven through the management API
tests/test_hostlink_smoke.py       HostLink integration assertions
```
