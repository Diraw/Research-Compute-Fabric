from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FIXTURES, PYTHON, Sandbox

from rcf.core.errors import ExecutorError
from rcf.core.models import NodeSpec, SshOptions
from rcf.core.runner import SubprocessRunner
from rcf.storage.fs import StorageFS

pytestmark = pytest.mark.e2e


def test_fake_ssh_persistent_generation_and_idempotent_reply(ssh_sandbox: Sandbox) -> None:
    node = NodeSpec(id="lab-hpc", kind="ssh", ssh_alias="lab-hpc",
                    ssh=SshOptions(program=[PYTHON, str(FIXTURES / "fake_ssh.py")]))
    fs = StorageFS(SubprocessRunner())
    root = ssh_sandbox.host / "fencing storage's ;$()"
    root.mkdir()
    (root / "a").write_bytes(b"old")
    entries = fs.inspect(node, str(root), ["a"])["entries"]
    old = fs.prepare_delete(node, str(root), "plan/quoted';$()")
    current = fs.prepare_delete(node, str(root), "plan/quoted';$()")
    assert current["generation"] == old["generation"] + 1
    with pytest.raises(ExecutorError, match="fenced"):
        fs.delete(node, str(root), entries, operation=old)
    assert (root / "a").read_bytes() == b"old"
    report = fs.delete(node, str(root), entries, operation=current)
    assert report["deleted"] == ["a"]
    (root / "a").write_bytes(b"replacement")
    replacement = fs.inspect(node, str(root), ["a"])["entries"]
    assert fs.delete(node, str(root), replacement, operation=current) == report
    assert (root / "a").read_bytes() == b"replacement"
    assert {e["path"] for e in fs.scan(node, str(root))["entries"]} == {"a"}
    prepared = fs.prepare_delete(node, str(root), "plan/quoted';$()")
    assert prepared["previous_report"] == report


def test_fake_ssh_metadata_symlink_fails_closed(ssh_sandbox: Sandbox) -> None:
    node = NodeSpec(id="lab-hpc", kind="ssh", ssh_alias="lab-hpc",
                    ssh=SshOptions(program=[PYTHON, str(FIXTURES / "fake_ssh.py")]))
    fs = StorageFS(SubprocessRunner())
    root = ssh_sandbox.host / "storage"
    root.mkdir()
    outside = ssh_sandbox.host / "precious"
    outside.mkdir()
    (outside / "keep").write_bytes(b"unchanged")
    try:
        (root / ".rcf-storage").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(ExecutorError):
        fs.prepare_delete(node, str(root), "plan")
    assert (outside / "keep").read_bytes() == b"unchanged"
    assert list(outside.iterdir()) == [Path(outside / "keep")]
