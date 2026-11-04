# Integration tests (opt-in)

These tests touch real, already-configured nodes. They are skipped by default
and are never part of the offline acceptance run.

## Read-only smoke

```bash
RCF_IT=1 RCF_IT_NODE=<node-a>,<node-b> \
  uv run --frozen --offline pytest tests/integration/test_readonly_smoke.py -q -s
```

`inspect` and `doctor` only read state. No job is submitted and nothing is
created on the node.

## Isolated local full path

```bash
RCF_IT=1 RCF_IT_JOB_SECONDS=5 \
  uv run --frozen --offline pytest tests/integration/test_local_submit.py -q -s
```

This builds a temporary RCF home, a temporary project and a real local
subprocess, then checks plan → submit → observe → results → fetch. It does not
read or write the user's `~/.rcf` registry. Set `RCF_IT_JOB_SECONDS=60` for the
longer variant; the default is deliberately short.

## Real remote submit / cancel (explicit authorization)

```bash
RCF_IT=1 RCF_IT_ALLOW_SUBMIT=1 \
RCF_IT_SUBMIT_NODE=<node-id> RCF_IT_PROJECT=<project-id> RCF_IT_JOB_SECONDS=60 \
  uv run --frozen --offline pytest tests/integration/test_remote_submit.py -q -s
```

Requirements:

- `RCF_IT_ALLOW_SUBMIT=1` is a separate gate; `RCF_IT=1` alone never writes.
- `RCF_IT_SUBMIT_NODE` and `RCF_IT_PROJECT` are mandatory; there is no
  "all nodes" default for writes.
- The job is `sleep` via `command_override`, so it does not run the project's
  entrypoint and does not write into the project directory. It does occupy the
  requested CPU/GPU allocation for the configured duration.
- The cancel test submits `sleep 300`, then cancels and checks that the lease is
  released. Use a node where a 5-minute cancellation is acceptable.

## Boundaries

- Never run these in CI; CI runs `-m "not integration"`.
- Do not add credentials to the environment or the tests.
- A failure here is evidence about a real node, not a reason to weaken the
  assertion. Record the node, version and date in `docs/status.md`.
- The tests read the RCF home selected by `RCF_IT_HOME` (default `~/.rcf`).
