# Changelog

All notable changes to Research Compute Fabric are documented here.
This project follows a pre-1.0 versioning scheme: minor versions may change
behavior, and every entry states the verification boundary.

## [Unreleased]

### Added

- `compute_get_run` / `rcf run status` now expose the latest structured
  `diagnosis` at the top level, so a caller can answer "why did it fail?"
  without scanning the event list first.
- Observation truth fields on every run payload: `state_observed_at`,
  `observed_age_seconds`, `stale` and `stale_after_seconds`. A failed probe
  keeps the last known state but no longer makes it look fresh.
- `observe_recovered` event and error clearing when a later probe succeeds.
- MCP `compute_plan_environment` / `compute_apply_environment` for the
  two-stage environment install path.
- `rcf --version` as a global flag (the `rcf version` command remains).

### Changed

- A run submitted with `command_override` is no longer marked
  `contract_violation` when its output directory is empty. The plan already
  warns that an override hides artifact location; the run now records
  `output_contract_unchecked` instead of a false violation.
- `output_contract = "none"` records `output_contract_unmanaged` once, making
  "nothing to check" distinguishable from "checked and empty".
- `docs/roadmap.md` is now ordered by evidence: real-path verification first,
  reproducibility second, and queue/Web/multi-user explicitly deferred.

### Fixed

- SSH exit 255 keeps the SSH client's stderr in the error detail instead of
  collapsing it into a generic transport failure.

### Tests

- Added `tests/unit/test_observation_truthfulness.py` covering freshness,
  error lifecycle, diagnosis surfacing, and SSH stderr preservation.
- Added contract tests for inputs manifest hand-off from plan to run and for
  concurrent GPU lease acquisition across two SQLite connections.

## [0.1.0] - 2026-10-31

Initial public candidate. Offline regression and simulated-host coverage only;
real-device results are not claimed by the version itself. See
`docs/status.md` for the exact verification boundary at a given commit.
