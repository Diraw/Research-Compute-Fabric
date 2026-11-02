"""Offline release utility regressions; all repositories and artifacts are temporary."""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("release_check", ROOT / "tools" / "release_check.py")
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    git = shutil.which("git")
    if not git:
        pytest.skip("git is required for release enumeration")
    repo = tmp_path / "repo"
    repo.mkdir()
    release.run([git, "init", "--quiet", str(repo)], cwd=tmp_path)
    files = {
        "pyproject.toml": "[project]\nname='test'\n",
        "uv.lock": "version = 1\n",
        "README.md": "delivery\n",
        ".gitignore": "ignored.py\n__pycache__/\n.venv/\n.tmp/\n",
        "src/rcf/__init__.py": "__version__ = '0.1.0'\n",
        "src/rcf/runs/untracked.py": "VALUE = 'unstaged runtime'\n",
        "src/rcf/ignored.py": "PRIVATE = True\n",
        "src/rcf/__pycache__/cached.pyc": "cache",
        "docs/guide.md": "guide\n",
        "examples/config.toml": "[example]\n",
        "nodes.toml": "private host config",
        ".rcf/rcf.toml": "private state",
        "runs/private/result.txt": "scientific data",
        "state/private.json": "private runtime",
        ".venv/private.py": "private environment",
        ".tmp/private.py": "scratch",
    }
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return repo


def test_snapshot_includes_untracked_sources_but_no_private_runtime(source: Path, tmp_path: Path) -> None:
    before = release.run([shutil.which("git"), "status", "--porcelain=v1"], cwd=source)
    manifest = release.snapshot(source, tmp_path / "snapshot")
    assert "src/rcf/runs/untracked.py" in manifest
    assert {"docs/guide.md", "examples/config.toml", "pyproject.toml", "uv.lock"} <= manifest.keys()
    assert set(manifest) == {
        ".gitignore", "README.md", "pyproject.toml", "uv.lock", "src/rcf/__init__.py",
        "src/rcf/runs/untracked.py", "docs/guide.md", "examples/config.toml",
    }
    for name, digest in manifest.items():
        assert (tmp_path / "snapshot" / name).read_bytes() == (source / name).read_bytes()
        assert digest == release.sha256((source / name).read_bytes())
    assert release.run([shutil.which("git"), "status", "--porcelain=v1"], cwd=source) == before
    assert not (tmp_path / "snapshot" / ".git").exists()


def test_snapshot_refuses_symlink(source: Path, tmp_path: Path) -> None:
    link = source / "docs" / "linked.md"
    try:
        link.symlink_to(source / "README.md")
    except OSError:
        pytest.skip("symlink creation requires OS privileges")
    with pytest.raises(release.ReleaseCheckError, match="symlink/junction"):
        release.snapshot(source, tmp_path / "snapshot")


@pytest.mark.parametrize("name", ["/absolute.py", "../private.py", "src/../../escape.py"])
def test_unsafe_delivery_paths_rejected(name: str) -> None:
    with pytest.raises(release.ReleaseCheckError, match="unsafe source path"):
        release.delivery_path(name)


def archives(tmp_path: Path, wheel_files: dict[str, bytes], sdist_files: dict[str, bytes]) -> tuple[Path, Path]:
    wheel, sdist = tmp_path / "test.whl", tmp_path / "test.tar.gz"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in wheel_files.items():
            archive.writestr(name, data)
    with tarfile.open(sdist, "w:gz") as archive:
        for name, data in sdist_files.items():
            info = tarfile.TarInfo("test-0.1/" + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return wheel, sdist


def test_artifacts_require_exact_runtime_bytes(source: Path, tmp_path: Path) -> None:
    expected = release.runtime_sources(source)
    wheel, sdist = archives(tmp_path, expected, {"src/" + k: v for k, v in expected.items()})
    result = release.verify_artifacts(source, wheel, sdist)
    assert result["runtime_python_files"] == 3
    assert set(result["runtime_sha256"]) == set(expected)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("defect", ["missing", "extra", "changed"])
def test_artifact_missing_extra_or_modified_runtime_rejected(
    source: Path, tmp_path: Path, kind: str, defect: str,
) -> None:
    expected = release.runtime_sources(source)
    bad = dict(expected)
    if defect == "missing":
        bad.pop("rcf/runs/untracked.py")
    elif defect == "extra":
        bad["rcf/extra.py"] = b"EXTRA = True\n"
    else:
        bad["rcf/runs/untracked.py"] += b"# tampered\n"
    wheel_files = bad if kind == "wheel" else expected
    sdist_files = bad if kind == "sdist" else expected
    wheel, sdist = archives(tmp_path, wheel_files, {"src/" + k: v for k, v in sdist_files.items()})
    with pytest.raises(release.ReleaseCheckError, match=f"{kind} runtime (members|bytes) differ"):
        release.verify_artifacts(source, wheel, sdist)


def test_duplicate_runtime_archive_member_rejected(source: Path, tmp_path: Path) -> None:
    expected = release.runtime_sources(source)
    wheel, sdist = archives(tmp_path, expected, {"src/" + k: v for k, v in expected.items()})
    with zipfile.ZipFile(wheel, "a") as archive, pytest.warns(UserWarning, match="Duplicate name"):
        archive.writestr("rcf/__init__.py", expected["rcf/__init__.py"])
    with pytest.raises(release.ReleaseCheckError, match="duplicate Python members"):
        release.verify_artifacts(source, wheel, sdist)


def test_existing_output_is_never_overwritten(source: Path, tmp_path: Path) -> None:
    output = tmp_path / "release"
    output.mkdir()
    marker = output / "important.whl"
    marker.write_bytes(b"previous release")
    with pytest.raises(release.ReleaseCheckError, match="refusing overwrite"):
        release.release_check(source, output)
    assert marker.read_bytes() == b"previous release"
    assert list(output.iterdir()) == [marker]


def test_failed_build_preserves_json_evidence(source: Path, tmp_path: Path, monkeypatch) -> None:
    def fail_build(*args):
        raise release.ReleaseCheckError("build unavailable offline")

    output = tmp_path / "release"
    if not shutil.which("uv"):
        pytest.skip("uv required")
    monkeypatch.setattr(release, "build", fail_build)
    with pytest.raises(release.ReleaseCheckError, match="unavailable offline"):
        release.release_check(source, output)
    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["ok"] is False
    assert "unavailable offline" in evidence["error"]
    assert "src/rcf/runs/untracked.py" in evidence["source_manifest_sha256"]


def test_uv_build_is_strictly_offline(source: Path, tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(release.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(release, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    assert release.build(source, tmp_path / "artifacts", "uv") == "uv-offline"
    argv, kwargs = calls[0]
    assert argv[:3] == ["uv", "build", "--offline"]
    assert "--no-python-downloads" in argv
    assert argv[argv.index("--python") + 1] == sys.executable
    assert "--sdist" in argv and "--wheel" in argv
    assert kwargs["cwd"] == source


@pytest.mark.parametrize("count, accepted", [(29, True), (28, False)])
def test_release_requires_exactly_29_tools_and_records_evidence(
    source: Path, tmp_path: Path, monkeypatch, count: int, accepted: bool,
) -> None:
    if not shutil.which("uv"):
        pytest.skip("uv required")

    (source / "src/rcf/ignored.py").unlink()

    def fake_build(delivered, artifacts, uv):
        artifacts.mkdir()
        expected = release.runtime_sources(delivered)
        archives(artifacts, expected, {"src/" + k: v for k, v in expected.items()})
        return "test-build"

    monkeypatch.setattr(release, "build", fake_build)
    monkeypatch.setattr(release, "smoke", lambda *args: {"tool_count": count, "tools": ["fake"] * count})
    output = tmp_path / "release"
    if accepted:
        assert release.release_check(source, output)["ok"] is True
    else:
        with pytest.raises(release.ReleaseCheckError, match="must be exactly 29, got 28"):
            release.release_check(source, output)
    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["ok"] is accepted
    assert evidence["smoke"]["tool_count"] == count
    assert len(evidence["artifacts"]) == 2


def test_ignored_runtime_python_is_a_release_blocker(source: Path, tmp_path: Path, monkeypatch) -> None:
    if not shutil.which("uv"):
        pytest.skip("uv required")

    def fake_build(delivered, artifacts, uv):
        artifacts.mkdir()
        expected = release.runtime_sources(delivered)
        archives(artifacts, expected, {"src/" + k: v for k, v in expected.items()})
        return "test-build"

    monkeypatch.setattr(release, "build", fake_build)
    with pytest.raises(release.ReleaseCheckError, match="snapshot runtime members differ.*ignored.py"):
        release.release_check(source, tmp_path / "release")


def test_smoke_uses_external_venv_isolated_imports_and_offline_install(
    source: Path, tmp_path: Path, monkeypatch,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    monkeypatch.setenv("RCF_HOME", "must-not-use-real-home")
    monkeypatch.setenv("RCF_JSON", "must-not-inherit")
    monkeypatch.setenv("PYTHONPATH", "must-not-inherit")
    monkeypatch.setattr(release.tempfile, "TemporaryDirectory", lambda **kwargs: _Temporary(external))
    created = []

    class Builder:
        def __init__(self, **kwargs):
            assert kwargs == {"system_site_packages": True, "with_pip": False}

        def create(self, path):
            created.append(path)
            (path / "lib").mkdir(parents=True)

    monkeypatch.setattr(release.venv, "EnvBuilder", Builder)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "import sysconfig; print(sysconfig.get_path('purelib'))" in argv:
            return str(external / "venv" / "lib") + "\n"
        if release.SMOKE_SCRIPT in argv:
            return json.dumps({"tool_count": 29, "tools": []})
        if "node" in argv:
            return "[]\n"
        return ""

    monkeypatch.setattr(release, "run", fake_run)
    result = release.smoke(tmp_path / "test.whl", source, source, "uv")
    assert created == [external / "venv"]
    install = next(argv for argv, _ in calls if argv[:3] == ["uv", "pip", "install"])
    assert "--offline" in install and "--no-deps" in install and "--reinstall" in install
    for argv, kwargs in calls:
        assert kwargs["cwd"] == external
        if "env" in kwargs:
            env = kwargs["env"]
            assert env["RCF_HOME"] == str(external / "rcf-home")
            assert "RCF_JSON" not in env and "PYTHONPATH" not in env
            assert env["UV_OFFLINE"] == "1" and env["UV_PYTHON_DOWNLOADS"] == "never"
        if release.SMOKE_SCRIPT in argv or "node" in argv:
            assert argv[1] == "-I"
    assert result["cli_nodes"] == [] and result["isolated"] is True


class _Temporary:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        return str(self.path)

    def __exit__(self, *args):
        return False
