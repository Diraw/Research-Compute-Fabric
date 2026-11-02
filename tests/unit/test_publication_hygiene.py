"""Public-delivery privacy regressions; never read private homes or runtime data."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from rcf.core.config import Config
from rcf.core.runner import SubprocessRunner

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_MARKERS = tuple(
    left + right
    for left, right in [
        ("ding", "rui"), ("cra", "ft"), ("gran", "ular"),
        ("csh", "ape"), ("ws-", "kb"), ("ws-", "ubuntu"),
    ]
)
PUBLIC_DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md")),
               *sorted((ROOT / "examples").rglob("*.toml"))]


@pytest.mark.parametrize("path", PUBLIC_DOCS, ids=lambda path: path.name)
def test_public_material_does_not_expose_private_context(path: Path) -> None:
    text = path.read_text(encoding="utf-8").casefold()
    # Do not include the matched text in pytest output: it may be sensitive.
    assert not any(marker in text for marker in PRIVATE_MARKERS), f"private context in {path.name}"


def checker():
    spec = importlib.util.spec_from_file_location("publication_check", ROOT / "tools/publication_check.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_checker_reports_location_and_category_without_echoing_sensitive_text(tmp_path: Path) -> None:
    secret = "ghp_" + "A" * 36
    account = "internal-research-account"
    home = "/home/" + account + "/work"
    address = ".".join(("10", "23", "45", "67"))  # Deliberately non-documentation synthetic address.
    (tmp_path / "README.md").write_text(f"{secret}\n{home}\nssh {address}\n", encoding="utf-8")
    findings = checker().scan(tmp_path)
    assert {item.category for item in findings} >= {"credential", "personal-path", "network-address"}
    result = SubprocessRunner().run(
        [sys.executable, str(ROOT / "tools/publication_check.py"), "--source", str(tmp_path), "--json"],
        timeout=10,
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert all({"path", "line", "category"} == set(item) for item in payload["findings"])
    assert secret not in result.stdout and account not in result.stdout


def test_checker_scans_only_public_allowlist_not_runtime_or_private_data(tmp_path: Path) -> None:
    marker = PRIVATE_MARKERS[0]
    for name in [".context/note.md", ".venv/package.py", "runs/run/log.txt",
                 ".tmprelease-old/evidence.json", "private/nodes.toml", "nodes.toml"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(marker, encoding="utf-8")
    assert checker().scan(tmp_path) == []
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text(marker, encoding="utf-8")
    findings = checker().scan(tmp_path)
    assert [(item.path, item.category) for item in findings] == [("docs/guide.md", "private-context")]


def test_checker_accepts_documentation_placeholders_and_reserved_ip_ranges(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "/home/researcher/work /Users/example/work C:/Users/you/work\n"
        "192.0.2.1 198.51.100.2 203.0.113.3 127.0.0.1 ::1\n"
        "https://github.com/dstackai/dstack\n", encoding="utf-8",
    )
    assert checker().scan(tmp_path) == []


def test_checker_does_not_follow_links_outside_public_tree(tmp_path: Path) -> None:
    target = tmp_path / "private.txt"
    target.write_text(PRIVATE_MARKERS[0], encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    try:
        (docs / "external.md").symlink_to(target)
    except OSError:
        pytest.skip("creating symlinks requires platform permission")
    findings = checker().scan(tmp_path)
    assert [(item.path, item.category) for item in findings] == [("docs/external.md", "unsafe-link")]


def test_public_examples_load_and_refer_to_a_shipped_runnable_project(tmp_path: Path) -> None:
    for name in ("rcf.toml", "nodes.toml", "projects.toml", "environments.toml"):
        shutil.copyfile(ROOT / "examples" / name, tmp_path / name)
    config = Config.load(tmp_path)
    assert "demo" in config.projects
    project = config.projects["demo"]
    assert project.script == "compute.py"
    assert (ROOT / "examples" / "demo" / project.script).is_file()
    assert project.output_contract == "argument"
    assert project.env_id in config.environments
    assert "local" in project.paths


def test_shipped_quickstart_executes_locally_and_verifies_deterministic_result() -> None:
    result = SubprocessRunner().run(
        [sys.executable, str(ROOT / "examples/quickstart.py")], cwd=str(ROOT), timeout=45,
    )
    assert result.ok, "bundled local quickstart failed (inspect locally, do not publish private paths)"
    assert ": succeeded" in result.stdout
    assert "Verified result:" in result.stdout


def test_private_and_release_scratch_are_ignored_without_hiding_source(tmp_path: Path) -> None:
    git = shutil.which("git")
    if not git:
        pytest.skip("git is required for independent ignore verification")
    runner = SubprocessRunner()
    initialized = runner.run([git, "init", "--quiet", str(tmp_path)])
    assert initialized.ok, initialized.stderr
    shutil.copyfile(ROOT / ".gitignore", tmp_path / ".gitignore")
    exclude = tmp_path / "empty-excludes"
    exclude.write_text("", encoding="utf-8")
    paths = [".context/private.md", ".tmprelease-private/evidence.json", "private/nodes.toml",
             "nodes.toml", "projects.toml", "environments.toml", "rcf.toml", ".env", ".env.local"]
    command = [git, "-c", f"core.excludesFile={exclude}", "check-ignore", "--no-index", "--stdin"]
    result = runner.run(command, cwd=str(tmp_path), stdin="\n".join(paths) + "\n")
    assert result.ok, result.stderr
    assert set(result.stdout.splitlines()) == set(paths)
    public = ["src/rcf/runs/store.py", "examples/nodes.toml", "examples/rcf.toml",
              "tools/publication_check.py", "tests/unit/test_publication_hygiene.py"]
    result = runner.run(command, cwd=str(tmp_path), stdin="\n".join(public) + "\n")
    assert result.returncode == 1, result.stdout


def test_public_tree_passes_publication_check() -> None:
    findings = checker().scan(ROOT, include_code=True)
    assert not findings, "public-tree privacy findings: " + "; ".join(
        f"{item.path}:{item.line} ({item.category})" for item in findings
    )
