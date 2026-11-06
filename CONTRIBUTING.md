# Contributing

Thanks for helping. RCF is deliberately conservative: a change that makes a
claim easier to state but harder to verify is usually the wrong change.

## Setup

```bash
uv sync
uv run rcf --help
```

Python 3.11+ is required. `uv.lock` is the source of truth for dependency
versions; do not hand-edit it.

## Tests

```bash
uv run pytest -m "not integration"        # offline / simulated hosts
uv run ruff check src tests tools
```

- The default suite never connects to a real node or the network.
- `tests/integration/` is opt-in (`RCF_IT=1`) and needs a node you own. Never
  point it at a shared production host, and never weaken an assertion just to
  make it pass.
- A test that needs a missing platform capability must `skip` with the concrete
  reason, not silently succeed.
- Prefer counterexamples: a test that reproduces the unsafe state before the
  fix is worth more than a happy-path round trip.

## Design invariants

- Planning has no side effects; submission needs explicit confirmation and a
  plan without blockers.
- A persisted state is not evidence. Re-read the executor and keep `unknown`
  when the outcome cannot be observed.
- Destructive operations select exact files, re-fingerprint them at apply time,
  and require a separate confirmation.
- No passwords, private keys or tokens in RCF configuration. Do not add an
  option that weakens SSH host-key verification.
- Fail closed: when an index, scan or fingerprint is incomplete, refuse rather
  than treat the missing part as safe.

## Documentation and changelog

- Every user-visible behavior change gets a `CHANGELOG.md` entry under
  `[Unreleased]`.
- Verification claims belong in `docs/status.md` and must name the command, the
  platform and the result. "CI is configured" is not "CI passed".
- Update `README.md` when the tool surface (CLI commands, MCP tools) changes.
