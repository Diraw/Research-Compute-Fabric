"""Configuration loading, spec validation and command rendering."""

from __future__ import annotations

import pytest
from conftest import make_home
from pydantic import ValidationError

from rcf.core.config import Config, write_starter_home
from rcf.core.errors import ConfigError, SpecError
from rcf.core.models import NodeSpec, ProjectSpec, RunRequest
from rcf.core.versions import satisfies
from rcf.runs.planner import build_command, command_preview

BASE_NODE = """
[nodes.ws]
kind = "ssh"
ssh_alias = "ws"
work_root = "/home/me/rcf"
"""


def test_load_home_with_registries(tmp_path):
    home = make_home(
        tmp_path,
        nodes=BASE_NODE,
        projects="""
[projects.p1]
entrypoint = "python {script}"
script = "run.py"
paths = { "ws" = "/home/me/p1" }
""",
        environments="""
[environments.e1]
kind = "system"
python = "python3"
""",
    )
    config = Config.load(home)
    assert set(config.nodes) == {"ws"}
    assert set(config.projects) == {"p1"}
    assert set(config.environments) == {"e1"}
    assert config.projects["p1"].path_for("ws") == "/home/me/p1"
    assert config.db_path == home / "state" / "rcf.db"


def test_missing_home_is_tolerated_with_warning(tmp_path):
    config = Config.load(tmp_path / "nope")
    assert config.nodes == {}
    assert config.warnings


def test_duplicate_id_across_registry_files_is_rejected(tmp_path):
    home = make_home(tmp_path, nodes=BASE_NODE)
    (home / "nodes.d").mkdir()
    (home / "nodes.d" / "extra.toml").write_text(BASE_NODE, encoding="utf-8")
    with pytest.raises(SpecError, match="duplicate"):
        Config.load(home)


def test_unknown_top_level_key_is_rejected(tmp_path):
    home = make_home(tmp_path, nodes="[nods.ws]\nkind = 'local'\n")
    with pytest.raises(SpecError, match="unknown top-level"):
        Config.load(home)


def test_entry_key_must_match_id(tmp_path):
    home = make_home(
        tmp_path,
        nodes="""
[nodes.ws]
id = "other"
kind = "local"
""",
    )
    with pytest.raises(SpecError, match="does not match its id"):
        Config.load(home)


def test_project_referencing_unknown_node_is_rejected(tmp_path):
    home = make_home(
        tmp_path,
        nodes=BASE_NODE,
        projects="""
[projects.p1]
entrypoint = "true"
paths = { "ghost" = "/x" }
""",
    )
    with pytest.raises(SpecError, match="unknown node"):
        Config.load(home)


def test_project_referencing_unknown_environment_is_rejected(tmp_path):
    home = make_home(
        tmp_path,
        nodes=BASE_NODE,
        projects="""
[projects.p1]
entrypoint = "true"
env_id = "ghost"
paths = { "ws" = "/x" }
""",
    )
    with pytest.raises(SpecError, match="unknown environment"):
        Config.load(home)


def test_local_node_must_not_have_ssh_alias():
    with pytest.raises(ValidationError):
        NodeSpec(id="x", kind="local", ssh_alias="host")


def test_remote_node_requires_ssh_alias():
    with pytest.raises(ValidationError):
        NodeSpec(id="x", kind="ssh")


def test_work_root_must_be_absolute_for_remote():
    with pytest.raises(ValidationError):
        NodeSpec(id="x", kind="ssh", ssh_alias="h", work_root="rcf/runs")


def test_work_root_accepts_tilde():
    node = NodeSpec(id="x", kind="ssh", ssh_alias="h", work_root="~/rcf/")
    assert node.work_root == "~/rcf"


def test_host_key_weakening_ssh_option_is_refused(tmp_path):
    home = make_home(
        tmp_path,
        nodes="""
[nodes.ws]
kind = "ssh"
ssh_alias = "ws"
work_root = "/home/me/rcf"

[nodes.ws.ssh]
extra_options = ["-o", "StrictHostKeyChecking=no"]
""",
    )
    with pytest.raises(ConfigError, match="host key"):
        Config.load(home)


def test_start_starter_home_is_idempotent(tmp_path):
    created = write_starter_home(tmp_path)
    assert {p.name for p in created} == {"rcf.toml", "nodes.toml", "projects.toml", "environments.toml"}
    again = write_starter_home(tmp_path)
    assert again == []


@pytest.mark.parametrize(
    "version,spec,expected",
    [
        ("3.12.3", ">=3.10", True),
        ("3.9.1", ">=3.10", False),
        ("1.26.4", ">=1.26,<3", True),
        ("3.0.0", ">=1.26,<3", False),
        ("2.0.1", "==2.0.1", True),
        ("2.0.0", "!=2.0.0", False),
        (None, ">=1", False),
        ("1.2.3", None, True),
    ],
)
def test_version_satisfies(version, spec, expected):
    assert satisfies(version, spec) is expected


def test_unsupported_version_spec_raises():
    with pytest.raises(ValueError):
        satisfies("1.0", "~1.0")


def test_build_command_keeps_parameter_with_spaces_as_one_argument():
    command = build_command(
        template="{python} -m train --name {name} --seed {seed}",
        values={"python": "python3", "name": "my run", "seed": "7"},
        workdir="/work",
        env={},
        preamble=[],
        where="test",
    )
    assert command.argv == ["python3", "-m", "train", "--name", "my run", "--seed", "7"]


def test_build_command_reports_unknown_placeholder():
    with pytest.raises(SpecError, match="unknown placeholder"):
        build_command(
            template="python {script} --missing {not_defined}",
            values={"script": "a.py"},
            workdir="/w",
            env={},
            preamble=[],
            where="project.entrypoint",
        )


def test_build_command_detects_shell_features():
    command = build_command(
        template="python train.py | tee log.txt",
        values={},
        workdir="/w",
        env={},
        preamble=[],
        where="override",
    )
    assert command.shell is True
    assert command.argv == ["python train.py | tee log.txt"]


def test_command_preview_is_reviewable():
    command = build_command(
        template="python {script}",
        values={"script": "a.py"},
        workdir="/w",
        env={"X": "1"},
        preamble=["module load cuda"],
        where="t",
    )
    command.preview = command_preview(command)
    assert "# setup" in command.preview
    assert "module load cuda" in command.preview
    assert "export X=1" in command.preview
    assert "python a.py" in command.preview


def test_run_request_rejects_structured_parameters():
    with pytest.raises(ValidationError):
        RunRequest(project_id="p", params={"nested": {"a": 1}})


def test_project_path_fallback_wildcard():
    project = ProjectSpec(id="p", entrypoint="true", paths={"*": "/srv/p"})
    assert project.path_for("any-node") == "/srv/p"
