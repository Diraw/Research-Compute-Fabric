"""Delivery regressions independent of the current Git index and user RCF home."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import bash_support
import pytest
from conftest import make_home

from rcf.core.runner import CommandResult, SubprocessRunner

ROOT = Path(__file__).resolve().parents[2]
RUN_MODULES = {"__init__", "fetch", "manager", "planner", "store"}


def test_source_files_are_not_ignored_even_before_staging(tmp_path: Path) -> None:
    git = shutil.which("git")
    if not git:
        pytest.skip("git is required to check source delivery ignore rules")
    runner = SubprocessRunner()
    # An empty temporary index ensures a tracked file cannot mask a bad rule.
    result = runner.run([git, "init", "--quiet", str(tmp_path)])
    assert result.ok, result.stderr
    shutil.copyfile(ROOT / ".gitignore", tmp_path / ".gitignore")
    exclude = tmp_path / "empty-excludes"
    exclude.write_text("", encoding="utf-8")
    command = [git, "-c", f"core.excludesFile={exclude}", "check-ignore", "--no-index", "--stdin"]
    sources = sorted((ROOT / "src").rglob("*.py"))
    assert RUN_MODULES <= {path.stem for path in ROOT.joinpath("src/rcf/runs").glob("*.py")}
    result = runner.run(
        command,
        cwd=str(tmp_path),
        stdin="".join(path.relative_to(ROOT).as_posix() + "\n" for path in sources),
    )
    assert result.returncode == 1, f"source files excluded from delivery:\n{result.stdout}\n{result.stderr}"
    runtime = [".rcf/config.toml", "state/cache.json", "runs/run_1/run.log", "src/rcf/__pycache__/x.pyc"]
    result = runner.run(command, cwd=str(tmp_path), stdin="\n".join(runtime) + "\n")
    assert result.ok, result.stderr
    assert set(result.stdout.splitlines()) == set(runtime)


def test_copied_source_starts_cli_and_imports_mcp_without_user_home(tmp_path: Path) -> None:
    """Exercise a source-only delivery, not the editable install's import path."""

    delivered = tmp_path / "delivery"
    shutil.copytree(ROOT / "src" / "rcf", delivered / "rcf", ignore=shutil.ignore_patterns("__pycache__"))
    home = make_home(tmp_path)
    modules = [f"rcf.runs.{name}" for name in sorted(RUN_MODULES - {"__init__"})]
    script = (
        "import importlib, pathlib, runpy, sys; "
        "sys.path.insert(0, sys.argv.pop(1)); "
        "import rcf; "
        "assert pathlib.Path(rcf.__file__).is_relative_to(pathlib.Path(sys.path[0])); "
        f"[importlib.import_module(name) for name in {modules!r}]; "
        "from rcf.mcp.server import build_server; "
        "assert build_server() is not None; "
        "sys.argv[0] = 'rcf'; runpy.run_module('rcf.cli.main', run_name='__main__')"
    )
    result = SubprocessRunner().run(
        [sys.executable, "-I", "-c", script, str(delivered), "--home", str(home), "--json", "node", "list"],
        cwd=str(tmp_path),
        env={
            "RCF_HOME": str(home),
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "RCF_JSON": "0",
        },
        timeout=30,
    )
    assert result.ok, result.stderr
    assert json.loads(result.stdout) == []
    assert not (tmp_path / ".rcf").exists()


def test_bash_override_is_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RCF_TEST_BASH", "custom-bash")
    monkeypatch.setattr(bash_support.shutil, "which", lambda name: f"/custom/{name}")
    assert bash_support._candidates() == ["/custom/custom-bash"]


def test_windows_bash_prefers_git_install_over_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("RCF_TEST_BASH", raising=False)
    monkeypatch.setattr(bash_support, "WINDOWS", True)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    launcher = str(tmp_path / "System32" / "bash.exe")
    monkeypatch.setattr(bash_support.shutil, "which", lambda name: launcher if name == "bash" else None)
    candidates = bash_support._candidates()
    assert candidates[0] == str(tmp_path / "Git" / "bin" / "bash.exe")
    assert candidates.index(launcher) > 0


def test_windows_bash_never_launches_wsl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bash_support, "WINDOWS", True)
    launcher = tmp_path / "System32" / "bash.exe"
    launcher.parent.mkdir()
    launcher.touch()
    monkeypatch.setattr(bash_support, "_candidates", lambda: [str(launcher)])

    def unexpected_run(*args, **kwargs):
        pytest.fail("the WSL launcher must never be started")

    monkeypatch.setattr(bash_support.SubprocessRunner, "run", unexpected_run)
    assert bash_support.find_test_bash() is None


@pytest.mark.parametrize("returncode, expected", [(0, True), (1, False)])
def test_windows_bash_validates_msys_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int, expected: bool
) -> None:
    monkeypatch.setattr(bash_support, "WINDOWS", True)
    bash = tmp_path / "bash.exe"
    bash.touch()
    monkeypatch.setattr(bash_support, "_candidates", lambda: [str(bash)])

    def fake_run(self, argv, **kwargs):
        assert "--noprofile" in argv and "--norc" in argv
        assert "MINGW*|MSYS*" in argv[-1]
        assert "cygpath" in argv[-1]
        return CommandResult(argv=tuple(argv), returncode=returncode)

    monkeypatch.setattr(bash_support.SubprocessRunner, "run", fake_run)
    assert (bash_support.find_test_bash() == str(bash)) is expected


def test_missing_bash_override_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RCF_TEST_BASH", str(tmp_path / "missing-bash"))
    assert bash_support.find_test_bash() is None
