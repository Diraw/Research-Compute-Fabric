from __future__ import annotations

import pytest
from conftest import FIXTURES, PYTHON, Sandbox

from rcf.core.models import NodeSpec, SshOptions
from rcf.core.runner import SubprocessRunner
from rcf.storage.fs import StorageFS

pytestmark = pytest.mark.e2e


def sandbox_node() -> NodeSpec:
    return NodeSpec(
        id="lab-hpc", kind="ssh", ssh_alias="lab-hpc",
        ssh=SshOptions(program=[PYTHON, str(FIXTURES / "fake_ssh.py")]),
    )


def test_fake_ssh_batch_inventory_and_exact_deletion(ssh_sandbox: Sandbox) -> None:
    root = ssh_sandbox.host / "storage test's ;$()"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "sub" / "a ;'$.txt").write_bytes(b"remote data")
    (root / "keep").write_bytes(b"unmanaged")
    fs = StorageFS(SubprocessRunner())
    node = sandbox_node()
    scan = fs.scan(node, str(root))
    assert scan["complete"] is True
    assert scan["logical_bytes"] == 20
    assert {e["path"] for e in scan["entries"]} == {"sub", "sub/a ;'$.txt", "keep"}
    inspect = fs.inspect(node, str(root), ["sub/a ;'$.txt"])
    assert inspect["complete"] is True
    result = fs.delete(node, str(root), inspect["entries"])
    assert result["deleted"] == ["sub/a ;'$.txt"]
    assert result["logical_deleted_bytes"] == 11
    assert not result["blocked"]
    assert (root / "keep").read_bytes() == b"unmanaged"
    assert fs.delete(node, str(root), inspect["entries"])["missing"] == ["sub/a ;'$.txt"]


def test_fake_ssh_changed_and_missing(ssh_sandbox: Sandbox) -> None:
    fs = StorageFS(SubprocessRunner())
    node = sandbox_node()
    root = ssh_sandbox.host / "storage"
    root.mkdir()
    (root / "a").write_bytes(b"first")
    entries = fs.inspect(node, str(root), ["a"])["entries"]
    (root / "a").write_bytes(b"second")
    result = fs.delete(node, str(root), entries)
    assert result["blocked"][0]["path"] == "a"
    assert not result["deleted"]
    assert fs.inspect(node, str(root), ["missing"])["missing"] == ["missing"]
    limited = fs.scan(node, str(root), max_entries=0)
    assert limited["complete"] is False
    assert limited["entries"] == []


def test_fake_ssh_replaced_ancestor_and_root(ssh_sandbox: Sandbox) -> None:
    fs = StorageFS(SubprocessRunner())
    node = sandbox_node()
    root = ssh_sandbox.host / "storage"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a").write_bytes(b"data")
    entries = fs.inspect(node, str(root), ["sub/a"])["entries"]
    (root / "sub").rename(root / "old")
    (root / "sub").mkdir()
    (root / "sub" / "a").write_bytes(b"data")
    entries[0].update(fs.inspect(node, str(root), ["sub/a"])["entries"][0], ancestors=entries[0]["ancestors"])
    assert fs.delete(node, str(root), entries)["blocked"]
    root.rename(root.with_name("old-root"))
    root.mkdir()
    (root / "a").write_bytes(b"data")
    assert fs.delete(node, str(root), entries)["blocked"]
    assert (root / "a").exists()


def test_fake_ssh_links_not_followed(ssh_sandbox: Sandbox) -> None:
    fs = StorageFS(SubprocessRunner())
    node = sandbox_node()
    root = ssh_sandbox.host / "storage"
    outside = ssh_sandbox.host / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "precious").write_bytes(b"keep")
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    result = fs.scan(node, str(root))
    assert [e["path"] for e in result["entries"]] == ["link"]
    assert result["entries"][0]["kind"] == "symlink"
    assert fs.inspect(node, str(root), ["link/precious"])["complete"] is False
    assert (outside / "precious").read_bytes() == b"keep"


@pytest.mark.skipif(__import__("os").name == "nt", reason="Win32 disallows tab/newline filenames")
def test_fake_ssh_newline_tab_filenames(ssh_sandbox: Sandbox) -> None:
    root = ssh_sandbox.host / "storage"
    root.mkdir()
    name = "line\nwith\ttab"
    (root / name).write_bytes(b"data")
    fs = StorageFS(SubprocessRunner())
    node = sandbox_node()
    result = fs.inspect(node, str(root), [name])
    assert result["complete"] is True
    assert fs.delete(node, str(root), result["entries"])["deleted"] == [name]
