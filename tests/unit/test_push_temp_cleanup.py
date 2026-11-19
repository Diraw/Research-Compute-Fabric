"""Push staging cleanup: stale sweeps are bounded, best-effort removal is closed."""

from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

import pytest
from conftest import local_node, ssh_node

from rcf.core.errors import RcfError
from rcf.core.models import PushPlan, SyncFile
from rcf.core.runner import CommandResult, ScriptedRunner
from rcf.projects.push import ProjectPusher, _sweep_push_archives


def test_sweep_only_removes_old_rcf_archives() -> None:
    calls: list[str] = []

    def run(command, **kwargs):
        calls.append(command)
        return CommandResult((), 0, stdout="2\n")

    assert _sweep_push_archives(SimpleNamespace(run=run), "/srv/rcf-staging") == 2
    assert len(calls) == 1
    assert "find /srv/rcf-staging -maxdepth 1 -type f" in calls[0]
    assert "-name '.rcf-push-*.tar.gz'" in calls[0]
    assert "-mmin +1440" in calls[0]
    assert "-delete -print" in calls[0]


@pytest.mark.parametrize("result", [
    CommandResult((), 1, stderr="find: permission denied"),
    CommandResult((), 0, stdout="not-a-number\n"),
    CommandResult((), 0, stdout="", timed_out=True),
])
def test_sweep_failure_is_not_fatal(result) -> None:
    assert _sweep_push_archives(SimpleNamespace(run=lambda *args, **kwargs: result),
                                "/srv/rcf-staging") == 0


def _plan() -> PushPlan:
    return PushPlan(
        project_id="demo",
        source_node="source",
        target_node="target",
        source_root="/source",
        target_root="/target",
        new=[SyncFile(path="value", size=4, mtime=time.time())],
        bytes=4,
    )


def _pusher(tmp_path) -> ProjectPusher:
    pusher = ProjectPusher(SimpleNamespace(home=tmp_path), ScriptedRunner([]))
    pusher._remote_tmp = lambda node: "/srv/rcf-staging"  # type: ignore[method-assign]
    return pusher


def _seed_archive(pusher: ProjectPusher) -> str:
    digest = hashlib.sha256(b"data").hexdigest()

    def pack(source, root, files, archive):
        archive.write_bytes(b"data")
        return {"value": digest}

    pusher._pack_file = pack  # type: ignore[method-assign]
    return digest


def test_extract_failure_still_removes_remote_archive(tmp_path, monkeypatch) -> None:
    from rcf.executors.ssh import SshTransport

    pusher = _pusher(tmp_path)
    _seed_archive(pusher)
    calls: list[str] = []

    def run(self, command, **kwargs):
        calls.append(command)
        if "RCF_PUSH_EXTRACT" in command:
            return CommandResult((), 1, stderr="extract failed")
        return CommandResult((), 0)

    monkeypatch.setattr(SshTransport, "run", run)
    monkeypatch.setattr(SshTransport, "push_file", lambda *args, **kwargs: None)

    with pytest.raises(RcfError, match="extracting"):
        pusher.apply(_plan(), ssh_node("target"), _plan(), source=local_node("source"))

    assert any(call.startswith("find /srv/rcf-staging") and "-mmin +1440" in call
               for call in calls)
    assert any(call.startswith("rm -f /srv/rcf-staging/.rcf-push-")
               for call in calls)


def test_transfer_failure_still_attempts_remote_cleanup(tmp_path, monkeypatch) -> None:
    from rcf.executors.ssh import SshTransport

    pusher = _pusher(tmp_path)
    _seed_archive(pusher)
    calls: list[str] = []

    def run(self, command, **kwargs):
        calls.append(command)
        return CommandResult((), 0)

    def push_file(self, local, remote, **kwargs):
        raise RcfError("connection dropped")

    monkeypatch.setattr(SshTransport, "run", run)
    monkeypatch.setattr(SshTransport, "push_file", push_file)

    with pytest.raises(RcfError, match="connection dropped"):
        pusher.apply(_plan(), ssh_node("target"), _plan(), source=local_node("source"))

    assert any(call.startswith("rm -f /srv/rcf-staging/.rcf-push-")
               for call in calls)
