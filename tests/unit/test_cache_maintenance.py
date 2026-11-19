"""RCF cache ownership: path selection, dry-run, age/size pruning."""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest
from conftest import local_node, make_home, ssh_node
from typer.testing import CliRunner

from rcf.cli.main import app
from rcf.core.errors import RcfError, SpecError
from rcf.core.runner import ScriptedRunner, SubprocessRunner
from rcf.environments.manager import EnvironmentManager
from rcf.resources.cache_maintenance import (
    CacheManager,
    parse_duration,
    parse_size,
)
from rcf.security.policy import SecurityPolicy


class FakeConfig:
    def __init__(self, home):
        self.home = home

    def policy(self):
        return SecurityPolicy(allow_execute=True, allow_environment_mutation=True)


def cache_tree(tmp_path):
    root = tmp_path / "cache"
    (root / "pip").mkdir(parents=True)
    old = root / "pip" / "old.bin"
    old.write_bytes(b"o" * 10)
    os.utime(old, (time.time() - 40 * 86400, time.time() - 40 * 86400))
    new = root / "pip" / "new.bin"
    new.write_bytes(b"n" * 20)
    return root, old, new


def test_parse_size_and_duration():
    assert parse_size("0") == 0
    assert parse_size("5G") == 5 * 1024**3
    assert parse_size("500m") == 500 * 1024**2
    assert parse_duration("30d") == 30 * 86400
    assert parse_duration("0") == 0
    with pytest.raises(SpecError):
        parse_size("lots")
    with pytest.raises(SpecError):
        parse_duration("soon")


def test_prune_is_dry_run_by_default(tmp_path):
    root, old, new = cache_tree(tmp_path)
    manager = CacheManager(FakeConfig(tmp_path), SubprocessRunner())

    payload = manager.prune(local_node("laptop"), older_than_seconds=30 * 86400,
                            max_bytes=0, apply=False)

    assert payload["root"] == str(root)
    assert payload["files"] == 2
    assert payload["selected_files"] == 1
    assert payload["deleted_files"] == 0
    assert old.exists() and new.exists()


def test_prune_apply_deletes_only_selected_files(tmp_path):
    root, old, new = cache_tree(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"keep")
    link = root / "pip" / "link.bin"
    try:
        link.symlink_to(outside)
    except OSError:  # pragma: no cover - windows without symlink privilege
        link = None

    manager = CacheManager(FakeConfig(tmp_path), SubprocessRunner())
    payload = manager.prune(local_node("laptop"), older_than_seconds=30 * 86400,
                            max_bytes=0, apply=True)

    assert payload["deleted_files"] == 1
    assert payload["deleted_bytes"] == 10
    assert not old.exists()
    assert new.exists()
    assert outside.exists()
    if link is not None:
        assert link.is_symlink()


def test_size_rule_removes_oldest_first(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    oldest = root / "a.bin"
    oldest.write_bytes(b"a" * 10)
    os.utime(oldest, (time.time() - 90, time.time() - 90))
    newer = root / "b.bin"
    newer.write_bytes(b"b" * 10)
    os.utime(newer, (time.time() - 60, time.time() - 60))

    manager = CacheManager(FakeConfig(tmp_path), SubprocessRunner())
    payload = manager.prune(local_node("laptop"), older_than_seconds=0,
                            max_bytes=10, apply=True)

    assert payload["selected_files"] == 1
    assert payload["deleted_files"] == 1
    assert not oldest.exists()
    assert newer.exists()


def test_symlinked_cache_root_is_refused(tmp_path):
    real = tmp_path / "real-cache"
    real.mkdir()
    (real / "file.bin").write_bytes(b"x")
    link = tmp_path / "cache"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:  # pragma: no cover - windows without symlink privilege
        pytest.skip("symlinks unavailable")
    manager = CacheManager(FakeConfig(tmp_path), SubprocessRunner())

    with pytest.raises(RcfError, match="symlink"):
        manager.prune(local_node("laptop"), apply=False)


def test_install_cache_env_is_scoped_to_the_node(tmp_path):
    manager = EnvironmentManager(FakeConfig(tmp_path), ScriptedRunner([]))
    local = manager.install_cache_env(local_node("laptop"))
    assert local["PIP_CACHE_DIR"] == str(tmp_path / "cache" / "pip")
    assert local["UV_CACHE_DIR"] == str(tmp_path / "cache" / "uv")

    manager.ops.home_dir = lambda node: "/home/researcher"  # type: ignore[method-assign]
    remote = manager.install_cache_env(ssh_node("workstation"))
    assert remote["PIP_CACHE_DIR"] == "/home/researcher/.rcf-tmp/cache/pip"
    assert remote["CONDA_PKGS_DIRS"] == "/home/researcher/.rcf-tmp/cache/conda-pkgs"


def test_install_steps_get_cache_exports_but_verify_does_not(tmp_path):
    manager = EnvironmentManager(FakeConfig(tmp_path), ScriptedRunner([]))
    manager.ops.home_dir = lambda node: "/home/researcher"  # type: ignore[method-assign]
    runner = ScriptedRunner([])
    manager.runner = runner
    env = SimpleNamespace(env_vars={}, modules=[], kind="venv", id="demo", conda_root=None,
                          path_for=lambda node_id: "/home/researcher/envs/demo")

    manager._run_argv(env, ssh_node("workstation"), ["pip", "install", "x"], timeout=10,
                      extra_env={"PIP_CACHE_DIR": "/home/researcher/.rcf-tmp/cache/pip"})
    assert runner.inputs
    assert "PIP_CACHE_DIR=/home/researcher/.rcf-tmp/cache/pip" in runner.inputs[-1]

    manager._run_argv(env, ssh_node("workstation"), ["python", "-c", "print(1)"], timeout=10)
    assert "PIP_CACHE_DIR" not in runner.inputs[-1]


def test_cache_prune_cli_is_dry_run_until_yes(tmp_path):
    home = make_home(tmp_path, nodes='[nodes.laptop]\nkind = "local"\n')
    cache = home / "cache" / "pip"
    cache.mkdir(parents=True)
    old = cache / "old.bin"
    old.write_bytes(b"o" * 10)
    os.utime(old, (time.time() - 40 * 86400, time.time() - 40 * 86400))
    runner = CliRunner()

    dry = runner.invoke(app, [
        "--home", str(home), "--json", "cache", "prune", "--node", "laptop",
        "--older-than", "30d", "--max-size", "0",
    ])
    assert dry.exit_code == 0, dry.output
    payload = json.loads(dry.output)
    assert payload["selected_files"] == 1
    assert payload["deleted_files"] == 0
    assert old.exists()

    applied = runner.invoke(app, [
        "--home", str(home), "--json", "cache", "prune", "--node", "laptop",
        "--older-than", "30d", "--max-size", "0", "--yes",
    ])
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output)["deleted_files"] == 1
    assert not old.exists()
