"""Shared helpers for opt-in integration tests.

This module is deliberately *not* named ``conftest``: a second conftest.py in a
subdirectory shadows the top-level ``tests/conftest.py`` under pytest's default
import mode and breaks unrelated unit tests that do ``from conftest import ...``.

Nothing here runs at import time except reading environment variables.
"""

from __future__ import annotations

import os

import pytest

from rcf.core.config import Config, default_home
from rcf.service import RcfService

_TRUE = {"1", "true", "yes", "on"}


def enabled() -> bool:
    return os.environ.get("RCF_IT", "").strip().lower() in _TRUE


def allow_submit() -> bool:
    return os.environ.get("RCF_IT_ALLOW_SUBMIT", "").strip().lower() in _TRUE


def integration_home() -> str:
    return os.environ.get("RCF_IT_HOME", "").strip() or str(default_home())


def make_service(home: str | None = None) -> RcfService:
    return RcfService(Config.load(home or integration_home()), actor="integration")


def selected_nodes(service: RcfService) -> list[str]:
    raw = os.environ.get("RCF_IT_NODE", "").strip()
    nodes = [part.strip() for part in raw.split(",") if part.strip()]
    if not nodes:
        nodes = sorted(service.config.nodes)
    missing = [node_id for node_id in nodes if node_id not in service.config.nodes]
    if missing:
        pytest.fail(f"RCF_IT_NODE names undeclared node(s): {', '.join(missing)}")
    return nodes


def job_seconds() -> float:
    raw = os.environ.get("RCF_IT_JOB_SECONDS", "5").strip()
    try:
        seconds = float(raw)
    except ValueError:
        pytest.fail(f"RCF_IT_JOB_SECONDS must be a number, got {raw!r}")
    if seconds <= 0 or seconds > 300:
        pytest.fail("RCF_IT_JOB_SECONDS must be in (0, 300]")
    return seconds


def submit_context() -> tuple[str, str, float]:
    """Return (node, project, seconds) or skip with an actionable message."""

    if not allow_submit():
        pytest.skip("set RCF_IT_ALLOW_SUBMIT=1 to allow a real remote job/cancel")
    node = os.environ.get("RCF_IT_SUBMIT_NODE", "").strip()
    project = os.environ.get("RCF_IT_PROJECT", "").strip()
    if not node or not project:
        pytest.skip("set RCF_IT_SUBMIT_NODE and RCF_IT_PROJECT for a real remote job")
    return node, project, job_seconds()
