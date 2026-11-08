"""Why a run failed, decided from evidence the run itself left behind.

The scheduler can only report "exited with 2".  For a simulation that is not
enough: the same code is 2 when an argument is misspelled and when the solver
finishes and reports that the configuration does not pass its own check.  The
difference decides whether the next action is "fix the environment", "fix the
parameters" or "try another configuration", so it is worth deriving rather than
leaving to whoever opens the log.

Nothing here calls a model or guesses.  Every classification points at a concrete
line or field, and ``UNKNOWN`` is the honest answer when there is none — the
historical runs classified before this existed stay ``UNKNOWN`` on purpose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..core.models import FailureKind, RunState

#: Checked in order; the first marker present in the log decides.  A broken
#: environment wins over everything else because the program never really ran.
_ENVIRONMENT_MARKERS: tuple[tuple[str, str], ...] = (
    # "No module named" first: ``ModuleNotFoundError: No module named 'x'``
    # contains both, and the shorter form is the one worth quoting.
    ("No module named", "a Python package is missing from this environment"),
    ("ModuleNotFoundError", "a Python package is missing from this environment"),
    ("cannot open shared object file", "a compiled library is missing from the environment"),
    ("command not found", "the command is not on PATH in the job environment"),
    ("Permission denied", "the job cannot read/write the path it needs"),
    ("No space left on device", "the working filesystem is full"),
    ("MemoryError", "the job ran out of memory"),
    # A binary built for a newer glibc than the node has.  Seen on lab-hpc with a
    # wheel-linked library: the traceback says OSError, the cause is the node.
    ("GLIBC_", "the node's system libraries are older than this binary requires"),
)

#: Exceptions that mean "the data was not what the code needs".  A missing file
#: is an input problem even when the cause is a stale upstream stage, because the
#: fix is the same: produce the input, do not patch the program.
_INPUT_EXCEPTIONS: tuple[tuple[str, str], ...] = (
    ("FileNotFoundError", "a file the program needs is missing"),
    ("KeyError", "the input data lacks a field the program expects"),
)

#: Arguments that the program rejected.  These usually arrive as a traceback too,
#: so they are matched before the generic traceback rule.
_PARAMETER_MARKERS: tuple[tuple[str, str], ...] = (
    ("invalid int value", "an argument has the wrong type"),
    ("invalid float value", "an argument has the wrong type"),
    ("invalid choice", "an argument is outside the accepted set"),
    ("unrecognized arguments", "the command received arguments it does not define"),
    ("the following arguments are required", "a required argument is missing"),
    ("must be <=", "an argument is outside the range the model accepts"),
    ("must be >=", "an argument is outside the range the model accepts"),
)

#: A pretty-printed report can easily exceed 40 lines, and a tail that starts
#: mid-document cannot be parsed.  Classification therefore asks for more of the
#: log than a human-facing ``logs --tail`` does.
CLASSIFY_TAIL_LINES = 400

#: Keys a program uses when it reports a verdict about the work it just did.
_VERDICT_KEYS = ("accepted", "status", "ok", "passed", "converged", "verdict")

_TRACEBACK = "Traceback (most recent call last):"

#: ``1 failed, 69 passed in 24.96s``: the project's own suite reported failures.
_TEST_SUMMARY = re.compile(r"^\d+ (?:failed|error)", re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    """One classification plus the evidence that produced it."""

    kind: FailureKind
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


def classify_failure(
    *, state: RunState, exit_code: int | None, tail: str
) -> Verdict:
    """Classify a terminal failure from the log tail and the exit code."""

    lines = tail.splitlines()
    label = f"exit {exit_code}" if exit_code is not None else "no exit code"
    last_line = next((line.strip() for line in reversed(lines) if line.strip()), "")

    if state is RunState.LOST:
        return Verdict(
            FailureKind.UNKNOWN,
            "state lost: the scheduler no longer knows this job",
            {"lost": True},
        )

    # A missing interpreter or package means the payload never ran, so it is
    # decided before any verdict the log happens to contain.
    if exit_code == 137:
        return Verdict(
            FailureKind.ENVIRONMENT,
            "exit 137: killed (out of memory, or walltime limit reached)",
            {"exit_code": exit_code},
        )
    # The log names the missing thing ("command not found: python3.9"), so it is
    # quoted before falling back to the exit code, which only says "something was
    # missing".
    for marker, hint in _ENVIRONMENT_MARKERS:
        if _contains(tail, marker):
            return Verdict(
                FailureKind.ENVIRONMENT,
                f"{marker}: {hint}",
                {"exit_code": exit_code, "pattern": marker},
            )
    if exit_code == 127:
        return Verdict(
            FailureKind.ENVIRONMENT,
            "exit 127: interpreter or command not found in the job environment",
            {"exit_code": exit_code},
        )
    if exit_code == 126:
        return Verdict(
            FailureKind.ENVIRONMENT,
            "exit 126: the command exists but cannot be executed",
            {"exit_code": exit_code},
        )

    documents = list(_json_objects(lines))
    verdict = next((doc for doc in documents if any(key in doc for key in _VERDICT_KEYS)), None)
    if verdict is not None:
        key, value = _summary(verdict)
        return Verdict(
            FailureKind.DOMAIN,
            f"{label}: the program finished and reported {key}={value}",
            {"exit_code": exit_code, "verdict": {key: value}},
        )
    if exit_code == 2 and documents:
        # The pipelines on this profile exit 2 after printing their report, and not
        # every report carries a status field.  A complete JSON document at the end
        # of a run that exited 2 is the program finishing, not crashing; the detail
        # names the evidence so a reader can disagree with the inference.
        keys = ", ".join(sorted(documents[0])[:4])
        return Verdict(
            FailureKind.DOMAIN,
            f"{label}: the program printed a report and exited 2 (no verdict field; keys: {keys})",
            {"exit_code": exit_code, "document": True},
        )

    for marker, hint in _PARAMETER_MARKERS:
        if _contains(tail, marker):
            return Verdict(
                FailureKind.PARAMETER,
                f"{marker}: {hint}; last log line: {last_line[:200]}",
                {"exit_code": exit_code, "pattern": marker},
            )

    for marker, hint in _INPUT_EXCEPTIONS:
        if _raises(tail, marker):
            return Verdict(
                FailureKind.INPUT,
                f"{label}: {hint} ({last_line[:160]})",
                {"exit_code": exit_code, "exception": marker},
            )

    if _TEST_SUMMARY.match(last_line):
        return Verdict(
            FailureKind.CODE,
            f"{label}: the project's own tests reported failures ({last_line[:120]})",
            {"exit_code": exit_code, "test_summary": last_line[:120]},
        )

    if _TRACEBACK in tail:
        raised = last_line or "an exception"
        return Verdict(
            FailureKind.CODE,
            f"{label}: the program raised {raised[:200]}",
            {"exit_code": exit_code, "raised": raised[:200]},
        )

    # Nothing matched.  Returning silently leaves the reader with an exit code and
    # a multi-kilobyte log to grep; the last non-empty line is the cheapest
    # evidence actually available.  Labelling it ``no known failure pattern``
    # keeps a hint from being mistaken for a conclusion.
    detail = f"{label}: no known failure pattern"
    if last_line:
        detail = f"{detail}; last log line: {last_line[:200]}"
    return Verdict(
        FailureKind.UNKNOWN,
        detail,
        {"exit_code": exit_code, "unmatched": True},
    )


def _contains(tail: str, marker: str) -> bool:
    return marker.lower() in tail.lower()


def _raises(tail: str, exception: str) -> bool:
    """True when the log shows this exception being raised.

    The trailing colon keeps ``KeyError`` from matching a word in a report, and it
    is how both a traceback's last line and a plain ``print`` of the error look.
    """

    return f"{exception}:" in tail or f"{exception} :" in tail


def _json_objects(lines: list[str]) -> list[dict[str, Any]]:
    """Yield the JSON objects in the tail, newest first.

    A program that reports a verdict prints it as JSON and then exits nonzero, so
    the document is the evidence.  It may be one line or pretty-printed across
    many, and the last object wins because a run can print progress documents
    earlier.
    """

    found: list[dict[str, Any]] = []
    for end in range(len(lines) - 1, -1, -1):
        text = lines[end].strip()
        if not text.endswith("}"):
            continue
        if text.startswith("{"):
            candidates = [text]
        else:
            start = next(
                (i for i in range(end, -1, -1) if lines[i].strip().startswith("{")),
                None,
            )
            if start is None:
                continue
            candidates = ["".join(line + "\n" for line in lines[start : end + 1])]
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(payload, dict):
                found.append(payload)
                break
    return found


def _summary(payload: dict[str, Any]) -> tuple[str, Any]:
    for key in _VERDICT_KEYS:
        if key in payload:
            return key, payload[key]
    return "verdict", "unknown"
