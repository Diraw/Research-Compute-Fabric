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
- `rcf fetch` refuses a destination that overlaps another run's result, work or
  run directory. Runs are compared by physical endpoint (two local aliases are
  one filesystem), and the host's case rules apply (case-insensitive on macOS,
  including UNC paths on Windows).
- Storage cleanup proves a retained copy through the file's registered owner,
  not the run requested for cleanup. A shared output root registered under a
  later run no longer stays permanently blocked.

### Fixed

- SSH exit 255 keeps the SSH client's stderr in the error detail instead of
  collapsing it into a generic transport failure.
- Fetching a run into its own output/work root reports the real overlap before
  guard acquisition, instead of the opaque "storage roots already guarded".
- The destination guard uses the fail-closed run index: above 20,000 runs the
  fetch is refused rather than silently checking only the newest rows.

### Security

- On POSIX, RCF-owned `state/` and `logs/` directories are created 0700 and the
  SQLite database and starter configuration files 0600. Windows ACLs are
  unchanged; a user-supplied `RCF_HOME` directory itself is never chmodded.

### Tests

- Added `tests/unit/test_observation_truthfulness.py` covering freshness,
  error lifecycle, diagnosis surfacing, and SSH stderr preservation.
- Added contract tests for inputs manifest hand-off from plan to run and for
  concurrent GPU lease acquisition across two SQLite connections.
- The mutual replica-cleanup test seeds the legacy copy receipts directly,
  because fetch now refuses to create that layout; the "never delete both
  copies" fencing check is preserved. Added regressions for alias
  destinations, owner-keyed retained copies, macOS case rules, the
  reference-index limit, and POSIX file modes.
- The pilot e2e deadline is 5s instead of 2s so two interpreter startups cannot
  race the watchdog on a loaded machine; the kill, exit-code and preserved
  progress assertions are unchanged.
- CI runs the offline suite with xdist and isolates the wall-clock-sensitive
  runner and pilot deadline tests. On Windows the two steps take 85.56s +
  44.72s versus 335.95s serial (2026-11-06).

## [0.1.0] - 2026-10-31

Initial public candidate. Offline regression and simulated-host coverage only;
real-device results are not claimed by the version itself. See
`docs/status.md` for the exact verification boundary at a given commit.
