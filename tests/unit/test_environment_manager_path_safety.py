"""The manager must consume actual prefix evidence, not only lexical ownership."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import make_home

from rcf.core.config import Config
from rcf.core.errors import PermissionDenied
from rcf.core.models import EnvironmentPlan, EnvironmentSpec, EnvironmentStep, NodeSpec
from rcf.core.runner import SubprocessRunner
from rcf.environments.manager import EnvironmentManager


def manager(tmp_path: Path):
    root = tmp_path / "managed"
    root.mkdir()
    home = make_home(tmp_path, settings="[environments]\nmanaged_roots=" + json.dumps([root.as_posix()]))
    return EnvironmentManager(Config.load(home), SubprocessRunner()), NodeSpec(id="local", kind="local"), root


def test_existing_prefix_cannot_be_claimed_when_old_exists_probe_failed(tmp_path, monkeypatch):
    mgr, node, root = manager(tmp_path)
    prefix = root / "manual"
    prefix.mkdir()
    from rcf.core.models import ProbeStatus

    monkeypatch.setattr(mgr.ops, "dir_exists", lambda *args: ProbeStatus.UNKNOWN)
    env = EnvironmentSpec(id="demo", path={"local": prefix.as_posix()})
    assert mgr._ownership(env, node, prefix.as_posix()).blocked


def test_apply_refuses_real_linked_ancestor_without_running_command(tmp_path):
    mgr, node, root = manager(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = root / "linked"
    runner = SubprocessRunner()
    if os.name == "nt":
        result = runner.run(["cmd.exe", "/c", "mklink", "/J", str(linked), str(outside)])
        assert result.ok, result.stderr
    else:
        linked.symlink_to(outside, target_is_directory=True)
    try:
        path = (linked / "new-env").as_posix()
        env = EnvironmentSpec(id="demo", path={"local": path})
        plan = EnvironmentPlan(env_id="demo", node_id="local", action="create",
                               steps=[EnvironmentStep(description="must not run", command=["never-execute"])])
        assert mgr._ownership(env, node, path).blocked
        with pytest.raises(PermissionDenied):
            mgr.apply(plan, env, node, confirmed=True)
        assert not (outside / "new-env").exists()
    finally:
        if os.name == "nt":
            linked.rmdir()
        else:
            linked.unlink()


def test_unlinked_new_prefix_with_real_ancestor_is_allowed(tmp_path):
    mgr, node, root = manager(tmp_path)
    prefix = root / "new-env"
    env = EnvironmentSpec(id="demo", path={"local": prefix.as_posix()})
    assert mgr._ownership(env, node, prefix.as_posix()).allowed
