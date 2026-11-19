"""Home audit: read-only, bounded, and focused on unmanaged roots."""

from __future__ import annotations

import os
import time

from conftest import local_node

from rcf.core.models import NodeSpec
from rcf.core.runner import SubprocessRunner
from rcf.resources.home_audit import HomeAuditor, format_bytes


def make_tree(tmp_path):
    home = tmp_path / "home"
    files = {
        "rcf/run.txt": b"r",
        "envs/pkg.bin": b"e",
        "proj/reg/a.py": b"g",
        "proj/loose/b.py": b"l",
        "old/__pycache__/a.pyc": b"p",
        ".cache/pip/wheel.bin": b"c",
        ".local/lib/python3.12/site-packages/pkg/x.py": b"u",
        "note.txt": b"n",
        ".venvs/known/a.py": b"k",
        ".venvs/unknown/b.py": b"q",
        "miniforge3/lib/x.py": b"m",
    }
    for relative, content in files.items():
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    stale = home / ".rcf-tmp" / ".rcf-push-old.tar.gz"
    stale.parent.mkdir()
    stale.write_bytes(b"o")
    os.utime(stale, (time.time() - 2 * 86400, time.time() - 2 * 86400))
    return home


class FakeConfig:
    managed_roots = ["~/envs"]

    class _Project:
        def path_for(self, node_id):
            return "~/proj/reg"

        def data_path_for(self, node_id):
            return "~/data/reg"

    class _Environment:
        def path_for(self, node_id):
            return "~/.venvs/known"

    projects = {"demo": _Project()}
    environments = {"known": _Environment()}


class FakeOps:
    def __init__(self, home):
        self.home = str(home)

    def home_dir(self, node):
        return self.home


def test_audit_prunes_registered_roots_and_reports_unmanaged(tmp_path):
    home = make_tree(tmp_path)
    auditor = HomeAuditor(FakeConfig(), SubprocessRunner(), node_ops=FakeOps(home))
    payload = auditor.audit(local_node("source"))

    assert payload["node_id"] == "source"
    assert payload["node_kind"] == "local"
    assert payload["complete"] is True
    assert payload["unmanaged"]["files"] == 6
    assert payload["rcf_temp"]["files"] == 1
    assert payload["rcf_temp"]["stale_push_archives"] == 1
    assert payload["loose_root_files"]["files"] == 1
    assert payload["caches"] == [{"path": ".cache/pip", "files": 1, "bytes": 1}]
    assert payload["python_build_residue"] == {"files": 1, "bytes": 1}
    paths = {row["path"] for row in payload["unmanaged_trees"]}
    assert {"proj/loose", "old"}.issubset(paths)
    assert "proj/reg" not in paths
    assert any("site-packages" in warning for warning in payload["warnings"])
    assert any("not registered" in warning for warning in payload["warnings"])
    assert any("stale" in warning for warning in payload["warnings"])
    assert any("miniforge3" in path for path in payload["skipped"]["toolchains"])


def test_audit_reports_truncation_instead_of_guessing(tmp_path):
    home = make_tree(tmp_path)
    auditor = HomeAuditor(FakeConfig(), SubprocessRunner(), node_ops=FakeOps(home))
    payload = auditor.audit(local_node("source"), max_entries=2)

    assert payload["complete"] is False
    assert payload["truncated"] == "entries"
    assert any("partial" in warning for warning in payload["warnings"])


def test_roots_expand_tilde_and_remove_nested_known_paths(tmp_path):
    home = str(tmp_path)
    node = NodeSpec(id="work", kind="ssh", ssh_alias="work", work_root="~/rcf")
    auditor = HomeAuditor(FakeConfig(), SubprocessRunner())
    managed, known = auditor.roots(node, home)

    assert managed == [home + "/rcf", home + "/envs"]
    assert home + "/proj/reg" in known
    assert home + "/data/reg" in known
    assert home + "/.venvs/known" in known


def test_format_bytes():
    assert format_bytes(0) == "0 B"
    assert format_bytes(1024) == "1.0 KiB"
    assert format_bytes(1024**3) == "1.0 GiB"
