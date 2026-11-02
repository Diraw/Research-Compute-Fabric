"""`project push`: copy a checkout to a node without clobbering local edits."""

from __future__ import annotations

import time

import pytest
from conftest import make_home, toml_path

from rcf.core.config import Config
from rcf.core.errors import NotFound
from rcf.core.runner import SubprocessRunner
from rcf.service import RcfService


def build_home(tmp_path, *, extra_projects: str = "") -> tuple[RcfService, object, object]:
    source = tmp_path / "src-proj"
    target = tmp_path / "dst-proj"
    (source / "bench" / "nested").mkdir(parents=True)
    (source / "bench" / "run.py").write_text("print('hi')\n", encoding="utf-8")
    (source / "bench" / "nested" / "util.py").write_text("x = 1\n", encoding="utf-8")
    (source / "README.md").write_text("# demo\n", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "run.cpython-312.pyc").write_bytes(b"\x00binary")
    (source / "outputs").mkdir()
    (source / "outputs" / "old.json").write_text("{}", encoding="utf-8")

    home = make_home(
        tmp_path,
        nodes="""\
[nodes.work]
kind = "local"

[nodes.cluster]
kind = "local"
""",
        projects=f"""\
[projects.demo]
entrypoint = "{{python}} {{script}}"
script = "bench/run.py"
paths = {{ "work" = "{toml_path(source)}", "cluster" = "{toml_path(target)}" }}
{extra_projects}""",
    )
    service = RcfService(Config.load(home), runner=SubprocessRunner(), actor="test")
    return service, source, target


def test_push_sends_everything_but_caches_and_outputs(tmp_path):
    svc, source, target = build_home(tmp_path)
    try:
        plan = svc.push_project_plan("demo", "cluster")
        paths = sorted(f["path"] for f in plan["new"])
        assert paths == ["README.md", "bench/nested/util.py", "bench/run.py"]
        assert plan["source_node"] == "work"
        report = svc.push_project("demo", "cluster", confirm=True)
        assert report["files"] == 3
        assert report["verified"] is True
        assert (target / "bench" / "run.py").read_text(encoding="utf-8") == "print('hi')\n"
        assert not (target / "__pycache__").exists()
        assert not (target / "outputs").exists()
    finally:
        svc.close()


def test_second_push_has_nothing_to_do(tmp_path):
    svc, source, target = build_home(tmp_path)
    try:
        svc.push_project("demo", "cluster", confirm=True)
        plan = svc.push_project_plan("demo", "cluster")
        assert plan["new"] == [] and plan["changed"] == []
        assert plan["unchanged"] == 3
    finally:
        svc.close()


def test_same_size_change_is_detected_by_hash(tmp_path):
    svc, source, target = build_home(tmp_path)
    try:
        svc.push_project("demo", "cluster", confirm=True)
        time.sleep(1.1)  # a new second, so the size is not the only signal
        script = source / "bench" / "run.py"
        original = script.read_text(encoding="utf-8")
        script.write_text(original.replace("hi", "ho"), encoding="utf-8")  # same size

        plan = svc.push_project_plan("demo", "cluster")
        assert [f["path"] for f in plan["changed"]] == ["bench/run.py"]
        svc.push_project("demo", "cluster", confirm=True)
        assert "ho" in (target / "bench" / "run.py").read_text(encoding="utf-8")
    finally:
        svc.close()


def test_touching_a_file_without_editing_it_is_not_a_change(tmp_path):
    svc, source, target = build_home(tmp_path)
    try:
        svc.push_project("demo", "cluster", confirm=True)
        import os

        later = time.time() + 120
        os.utime(source / "bench" / "run.py", (later, later))
        plan = svc.push_project_plan("demo", "cluster")
        assert plan["changed"] == []
        assert plan["unchanged"] == 3
    finally:
        svc.close()


def test_newer_file_on_the_target_is_left_alone(tmp_path):
    svc, source, target = build_home(tmp_path)
    try:
        svc.push_project("demo", "cluster", confirm=True)
        remote_file = target / "bench" / "run.py"
        remote_file.write_text("# edited on the cluster\n", encoding="utf-8")
        future = time.time() + 3600
        import os

        os.utime(remote_file, (future, future))

        plan = svc.push_project_plan("demo", "cluster")
        assert [f["path"] for f in plan["remote_newer"]] == ["bench/run.py"]
        assert plan["changed"] == []
        svc.push_project("demo", "cluster", confirm=True)
        assert remote_file.read_text(encoding="utf-8") == "# edited on the cluster\n"

        forced = svc.push_project_plan("demo", "cluster", force=True)
        assert [f["path"] for f in forced["changed"]] == ["bench/run.py"]
    finally:
        svc.close()


def test_missing_source_path_is_reported(tmp_path):
    svc, source, target = build_home(tmp_path)
    try:
        svc.config.projects["demo"].paths.pop("work")
        with pytest.raises(NotFound):
            svc.push_project_plan("demo", "cluster")
    finally:
        svc.close()


def test_data_scope_copies_the_data_root_not_the_checkout(tmp_path):
    svc, source, target = build_home(tmp_path)
    data_src = tmp_path / "data-src"
    data_dst = tmp_path / "data-dst"
    (data_src / "init").mkdir(parents=True)
    (data_src / "init" / "conf.npy").write_bytes(bytes([1]) * 16)
    try:
        project = svc.config.projects["demo"]
        project.data_paths = {"work": str(data_src), "cluster": str(data_dst)}
        plan = svc.push_project_plan("demo", "cluster", scope="data")
        assert plan["scope"] == "data"
        assert [f["path"] for f in plan["new"]] == ["init/conf.npy"]
        report = svc.push_project("demo", "cluster", confirm=True, scope="data")
        assert report["files"] == 1
        assert (data_dst / "init" / "conf.npy").read_bytes() == bytes([1]) * 16
        # the checkout was not touched by a data push
        assert not (target / "README.md").exists()
    finally:
        svc.close()


def test_data_scope_without_a_data_root_is_reported(tmp_path):
    svc, source, target = build_home(tmp_path)
    try:
        project = svc.config.projects["demo"]
        project.data_paths = {"cluster": str(tmp_path / "nowhere")}
        with pytest.raises(NotFound) as excinfo:
            svc.push_project_plan("demo", "cluster", source_node_id="work", scope="data")
        assert "data_paths" in str(excinfo.value.hint or "")
        assert "data root" in str(excinfo.value)
    finally:
        svc.close()


def test_file_listing_works_on_bsd_userlands():
    """macOS ships BSD find/stat: no -printf, and stat takes -f with real tabs."""

    from rcf.projects.push import list_files_command

    command = list_files_command()
    assert "find . -maxdepth 0 -printf ''" in command  # capability probe
    assert "stat -f '%z" in command  # BSD branch
    assert "\t" in command  # BSD stat does not expand \t itself
