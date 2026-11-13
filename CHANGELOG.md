# Changelog

All notable changes to Research Compute Fabric are documented here.
This project follows a pre-1.0 versioning scheme: minor versions may change
behavior, and every entry states the verification boundary.

## [Unreleased]

### Fixed

- Linux project push now pins directory handles with `O_PATH` where supported,
  retaining `O_DIRECTORY`, `O_NOFOLLOW` and explicit directory verification.
  Search-only ancestors (such as shared home roots with mode 0711) no longer
  need directory-list permission for extraction into a user's writable tree.
  Platforms without `O_PATH` keep the strict read-only fallback; Windows is
  unchanged. Permission failures are reported separately from invalid/link
  failures. Archive validation, fd-relative writes and replacement guards remain.
  Verification boundary: scoped Windows tests and non-root Linux extraction
  counterexamples passed; the reloaded MCP completed a verified single-file
  push and the isolated benchmark code/data push through a real search-only HPC
  ancestor. See `docs/status.md` for the unrelated full-suite limitation.

### Added

- The report answers "when" as well as "what": a `When each kind appeared` matrix
  counts failures per kind per day, switching to weeks when the window exceeds two
  weeks. Each column carries how many runs finished in that bucket, so a cell reads
  against its denominator. Beyond twelve buckets the older history folds into one
  `earlier` column instead of disappearing, and the note says what it covers.
- `rcf storage guards` and `rcf storage unguard <guard>... --yes`: a guard has no
  expiry, so an interrupted fetch or cleanup keeps its roots locked forever. These
  show what is holding them and release it deliberately, with the guard's own
  evidence written to the audit log.
- `rcf run manifest <run_id> [--out file.json]`: a stable JSON record of one run —
  command, parameters, git commit, environment fingerprint, input digest, and the
  output files with their sha256. The output listing is read live; when it cannot be
  read the document records the error instead of an empty list, which would read as
  "this run produced nothing". `--no-hashes` skips content hashing.
- `rcf run diff <a> <b> [--artifacts]`: compares two runs by parameter, provenance,
  outcome, command and (opt-in) outputs. The command is compared by argv, not by the
  stored preview, because the preview carries the run id and would make every pair
  look different. Artifact comparison hashes both sides: two files of equal size and
  different content are `changed`, never `same`.
- `rcf run sweep -p PROJECT --grid seed=1,2,3 [--grid mode=fast,slow]`: plan every
  combination of a parameter grid, then submit them as one sweep. The whole grid is
  planned before the first submission, so one impossible point refuses the request
  instead of half-running it. `--param` supplies the values that stay fixed, a grid
  above 200 combinations is refused, and without `--yes` nothing is submitted.
- Runs carry a `sweep_id`, so `rcf run list --sweep <id>` returns exactly the
  combinations of one request, and `run status` shows which sweep a run belongs to.
- `rcf report [--out report.html] [--project P] [--since 7d] [--limit N]` writes
  one self-contained HTML page: what is still open, failures grouped by kind with
  the evidence that produced each verdict, per-project success and duration, and
  the failed runs themselves. No server, no JavaScript, no external asset, so the
  file survives being copied to a machine that cannot reach anything.
  Two rules carry over from the CLI: a state that was not re-observed is labelled
  `unverified <age>` rather than shown as healthy, and a failure with no recorded
  kind is shown as `unknown` rather than left blank.

- `rcf run reconcile --older-than-minutes 30 --limit 20` re-probes runs whose
  state claim has gone stale and reports what changed. Nothing polls in the
  background by design; this is the bounded "someone asks" that was missing, and
  it is CLI-only so the MCP surface stays at 31 tools. `rcf run list --stale`
  filters the same set for inspection.
- A failure with no matching pattern now records a diagnosis instead of nothing:
  the exit code plus the last non-empty log line, labelled `no known failure
  pattern`. Before this, every exit-2 failure in the database had a null
  diagnosis while the answer sat in the last line of its log.
- `partition_time_limit`: the planner now compares the requested walltime with the
  target partition's `MaxTime` (`sinfo %l`) and fails the plan instead of letting
  `sbatch` reject the job after it is queued. `infinite` and `PARTITION_LIMIT` are
  treated as "no bound" and produce no check at all.
- `parse_slurm_time_limit_minutes` in `resources/parsers.py`: pure parser for every
  shape Slurm echoes back (`MM`, `MM:SS`, `HH:MM:SS`, `D-HH`, `D-HH:MM`,
  `D-HH:MM:SS`), returning `None` for the unbounded markers.
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

- Plan-time `cpu_capacity` and `memory_capacity` on a Slurm node now use the
  target partition's per-node `%c` / `%m` instead of the login node's snapshot.
  The old comparison was wrong in both directions on a real cluster: 64 cores on
  a 64-core partition was rejected because the login node had 32, while 300 GB
  was accepted because the login node had 480 GB free and the compute nodes have
  251 GB. When `sinfo` reports no per-node figure, the login-node fallback now
  warns and says so instead of blocking.
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

- A blocked root now says who holds it: `fetch of run_... still holds these roots`
  plus the exact `rcf storage unguard <id> --yes` that frees it. "storage roots
  already guarded" left the user with a dead end and no next step.
- An interrupted transfer is reported as one: a killed scp shows up as exit
  `0xFFFFFFFF`, which was printed as `exit code 4294967295` with the hint "check that
  scp exists locally". It now reads `copying from 'lab-hpc' was interrupted (scp exit
  -1); nothing partial was kept here`, and says to retry.
- A fetch that dies before its cleanup no longer leaves garbage: the next fetch of
  the same run reclaims `.rcf-fetch-*.tar.gz` from the run directory (269 MB in the
  measured case) and removes its own stale temporary download directories.
- A command whose executable is a Windows path is refused at plan time for
  Slurm and SSH nodes. Git Bash rewrites `/public/x` into
  `C:/Program Files/Git/public/x` before RCF sees the argument, and shlex then
  splits that at the space, so the job died with `line 36: C:/Program: No such
  file or directory`. The error now names the rewritten path, recovers the
  intended one, and points at `MSYS_NO_PATHCONV=1`; a rewritten path anywhere
  else in the command becomes a `command_path` warning instead of a failure.
  Local nodes are unaffected: they run argv directly.
- A cancelled Slurm run no longer records `exit code 0`. `sacct` reports `0:0`
  for the allocation row of every cancelled job, so the old code could not tell a
  cancelled run from a clean finish. The exit code now comes from the `.batch`
  step when that row already exists (143 for SIGTERM); when it has not landed yet,
  the run records no exit code at all. Verified on lab-hpc 2026-11-07 with a
  RUNNING cancel and a PENDING cancel.
- The failure diagnosis is written after the state event, so the trail reads
  cause after effect and a diagnosis error can no longer cost the transition
  that was already persisted.
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

### Added

- `failure_kind` on every failed run: `domain` (the program finished and
  rejected the result), `environment`, `input` (a needed file is missing or
  lacks a field), `parameter`, `code`, or `unknown`. The
  scheduler only says "exit 2", which is the same code for a misspelled argument
  and for a solver that finished and said no; the split is derived from the log
  tail and the exit code, and `unknown` is written when nothing matches instead of
  guessing. `run list --kind` filters, `run status` shows a `verdict` line, and the
  diagnosis event carries `kind`.
- `rcf run classify` derives the kind for runs recorded before this existed, from
  the logs still on the nodes. It writes only that column: the state, exit code and
  timestamps are untouched, and the event is marked `retrospective: true` so a
  reader can tell a re-derived verdict from one recorded at failure time.
- `tests/unit/test_failure_classification.py`, including the log shapes that
  justify each rule (pretty-printed verdict JSON, glibc mismatch, argparse
  rejection, a failing pytest summary).

### Tests

- Added `tests/integration/test_long_run.py` and
  `assets/sleep_heartbeat.py`, gated by `RCF_IT_LONG=1` and at least one hour: it
  submits a heartbeat job, observes it once, stays deliberately silent longer than
  the staleness threshold, then asserts that the claim went stale while the job
  kept running, that one refresh recovers it, and that the lease is released only
  at the terminal state. Encodes the 2026-11-07 acceptance (66.3 minutes, job
  267670), but does not run by default.
- Added `tests/integration/assets/gpu_cuda_smoke.py` and `test_gpu_compute.py`: a
  driver-tier check that loads `libcuda.so.1` through `ctypes`, allocates 64 MiB
  and round-trips bytes inside a real Slurm allocation. No toolkit, no wheel, no
  kernel; verified on lab-hpc 2026-11-07 (RTX 3090, 0 mismatches).
- Added `tests/fixtures/slurm/squeue_pending.txt`, the first non-empty `squeue`
  sample: three 8-GPU jobs competing for a two-node partition, with a PENDING row
  and its `(Resources)` reason.
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
