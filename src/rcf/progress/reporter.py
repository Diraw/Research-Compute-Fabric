"""Dependency-free progress writer, safe to copy to a Python 3.6 worker.

One writer per file is required. Recognized MPI/launcher rank environments disable
nonzero ranks; callers with other launchers must select their writer themselves.
No directory creation, installation, run-state mutation, or implicit completion.
"""

# These modernizations would break the standalone Python 3.6 worker contract.
# ruff: noqa: UP007, UP017, UP045

import datetime
import json
import math
import os
import threading
import time
import uuid
from typing import Optional, Union

Number = Union[int, float]
_RANK_KEYS = (
    "RANK", "OMPI_COMM_WORLD_RANK", "PMI_RANK", "PMIX_RANK", "SLURM_PROCID",
    "MV2_COMM_WORLD_RANK",
)


def _number(value: Number, name: str, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError(f"{name} is outside the finite numeric range") from None
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ValueError("{} must be finite{}".format(name, " and nonnegative" if nonnegative else ""))
    return result


class Reporter:
    """Append progress records; updates suppressed by throttling remain local.

    Missing output path is a deliberate no-op, useful outside managed runs.
    ``finished`` describes only the reported phase, never successful run exit.
    ``close`` does not fabricate a final sample.
    """

    def __init__(
        self, run_id: Optional[str] = None, path: Optional[str] = None,
        total: Optional[Number] = None, phase: str = "compute",
        min_interval_seconds: Number = 1.0,
    ) -> None:
        self.run_id = run_id if run_id is not None else os.environ.get("RCF_RUN_ID") or str(uuid.uuid4())
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id must be a nonempty string")
        self.path = path if path is not None else os.environ.get("RCF_PROGRESS_FILE")
        if total is None and os.environ.get("RCF_PROGRESS_TOTAL"):
            total = float(os.environ["RCF_PROGRESS_TOTAL"])
        self.total = None if total is None else _number(total, "total", True)
        if not isinstance(phase, str) or not phase.strip():
            raise ValueError("phase must be a nonempty string")
        self.phase = phase
        self.min_interval_seconds = _number(min_interval_seconds, "min_interval_seconds", True)
        self.session_id = str(uuid.uuid4())
        self._enabled = bool(self.path)
        for key in _RANK_KEYS:
            if key in os.environ and int(os.environ[key]) != 0:
                self._enabled = False
        self._start = time.monotonic()
        self._last_write = None  # type: Optional[float]
        self._written_phase = None  # type: Optional[str]
        self._seq = 0
        self._completed = 0.0
        self._metric = None  # type: Optional[float]
        self._eta_reason = None  # type: Optional[str]
        self._finished = False
        self._closed = False
        self._lock = threading.Lock()

    def update(
        self, completed: Number, total: Optional[Number] = None,
        phase: Optional[str] = None, metric: Optional[Number] = None,
        eta_reason: Optional[str] = None, force: bool = False, finished: bool = False,
    ) -> bool:
        with self._lock:
            if self._closed:
                raise ValueError("reporter is closed")
            value = _number(completed, "completed", True)
            target_phase = self.phase if phase is None else phase
            if not isinstance(target_phase, str) or not target_phase.strip():
                raise ValueError("phase must be a nonempty string")
            target_total = self.total if target_phase == self.phase else None
            if total is not None:
                target_total = _number(total, "total", True)
            target_metric = None if metric is None else _number(metric, "metric")
            if eta_reason is not None and not isinstance(eta_reason, str):
                raise ValueError("eta_reason must be a string or None")
            if not isinstance(finished, bool) or not isinstance(force, bool):
                raise ValueError("force and finished must be bool")
            self._completed, self.total, self.phase = value, target_total, target_phase
            self._metric, self._eta_reason, self._finished = target_metric, eta_reason, finished
            return self._emit(force)

    def _emit(self, force: bool = False) -> bool:
        if not self._enabled:
            return False
        current = time.monotonic()
        if (not force and not self._finished and self._written_phase == self.phase
                and self._last_write is not None
                and current - self._last_write < self.min_interval_seconds):
            return False
        sample = {
            "run_id": self.run_id, "session_id": self.session_id, "seq": self._seq + 1,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "elapsed_seconds": float(current - self._start), "phase": self.phase,
            "completed": self._completed, "total": self.total, "metric": self._metric,
            "eta_reason": self._eta_reason, "finished": self._finished,
        }
        payload = (json.dumps(sample, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        # A single append write avoids interleaving a record with another write.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
        try:
            if os.write(fd, payload) != len(payload):
                raise OSError("incomplete progress record append")
        finally:
            os.close(fd)
        self._seq += 1
        self._last_write, self._written_phase = current, self.phase
        return True

    def heartbeat(self) -> bool:
        with self._lock:
            if self._closed:
                raise ValueError("reporter is closed")
            return self._emit()

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def __enter__(self) -> "Reporter":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
