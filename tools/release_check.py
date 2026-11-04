"""Offline release acceptance without changing the Git index or user RCF state.

Requires current runtime dependencies and uv; hatchling must be installed or
cached by uv. Each invocation owns a fresh output directory. No downloads occur.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import site
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
import venv
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

DELIVERY_ROOTS = {"src", "docs", "examples", "tests", "tools", ".github"}
DELIVERY_FILES = {"pyproject.toml", "uv.lock", "README.md", "LICENSE", "LICENSE.md", ".gitignore"}
EXCLUDED_PARTS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".tmp", "dist", "build", ".rcf", ".env",
}


class ReleaseCheckError(RuntimeError):
    """An actionable failure of offline release acceptance."""


def run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    """Build tooling is separate from runtime business subprocess policy."""
    result = subprocess.run(
        argv, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
        capture_output=True, timeout=300,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise ReleaseCheckError(
            f"command failed ({result.returncode}): {argv!r}\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def delivery_path(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ReleaseCheckError(f"unsafe source path: {name!r}")
    if any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in path.parts):
        return False
    if path.suffix in {".pyc", ".pyo", ".db"} or name.endswith((".db-wal", ".db-shm")):
        return False
    return name in DELIVERY_FILES or path.parts[0] in DELIVERY_ROOTS


def source_files(source: Path) -> list[str]:
    git = shutil.which("git")
    if not git:
        raise ReleaseCheckError("git is required to enumerate tracked and nonignored untracked delivery files")
    names = run(
        [git, "-C", str(source), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=source,
    ).split("\0")
    selected = sorted({name for name in names if name and delivery_path(name)})
    for name in selected:
        path = source / name
        for parent in (path, *path.parents):
            if parent == source:
                break
            attributes = getattr(parent.lstat(), "st_file_attributes", 0) if parent.exists() else 0
            if parent.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise ReleaseCheckError(f"delivery symlink/junction is not allowed: {name}")
        if not path.is_file():
            raise ReleaseCheckError(f"delivery file missing from working tree: {name}")
    required = {"pyproject.toml", "uv.lock", "README.md", "src/rcf/__init__.py"}
    missing = required - set(selected)
    if missing:
        raise ReleaseCheckError(f"missing required delivery files: {sorted(missing)}")
    return selected


def snapshot(source: Path, destination: Path) -> dict[str, str]:
    names = source_files(source)
    destination.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, str] = {}
    for name in names:
        data = (source / name).read_bytes()
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod((source / name).stat().st_mode & 0o777)
        manifest[name] = sha256(data)
    return manifest


def runtime_sources(source: Path) -> dict[str, bytes]:
    return {
        path.relative_to(source / "src").as_posix(): path.read_bytes()
        for path in sorted((source / "src" / "rcf").rglob("*.py"))
    }


def verify_artifacts(source: Path, wheel: Path, sdist: Path) -> dict[str, Any]:
    expected = runtime_sources(source)
    if not expected:
        raise ReleaseCheckError("snapshot contains no runtime Python sources")
    with zipfile.ZipFile(wheel) as archive:
        members = [name for name in archive.namelist() if name.endswith(".py")]
        if len(members) != len(set(members)):
            raise ReleaseCheckError("wheel has duplicate Python members")
        actual = {name: archive.read(name) for name in members}
    _compare_runtime("wheel", expected, actual)
    with tarfile.open(sdist, "r:gz") as archive:
        actual = {}
        roots: set[str] = set()
        for member in archive.getmembers():
            parts = PurePosixPath(member.name).parts
            if not parts or ".." in parts or PurePosixPath(member.name).is_absolute():
                raise ReleaseCheckError(f"unsafe sdist member: {member.name}")
            roots.add(parts[0])
            if not member.name.endswith(".py"):
                continue
            name = PurePosixPath(*parts[1:]).as_posix()
            if not name.startswith("src/rcf/"):
                continue
            name = name.removeprefix("src/")
            if not member.isfile() or name in actual:
                raise ReleaseCheckError(f"sdist has nonregular/duplicate runtime member: {member.name}")
            handle = archive.extractfile(member)
            if handle is None:
                raise ReleaseCheckError(f"unreadable sdist member: {member.name}")
            actual[name] = handle.read()
        if len(roots) != 1:
            raise ReleaseCheckError("sdist must have exactly one top-level directory")
    _compare_runtime("sdist", expected, actual)
    return {"runtime_python_files": len(expected), "runtime_sha256": {k: sha256(v) for k, v in expected.items()}}


def _compare_runtime(label: str, expected: dict[str, bytes], actual: dict[str, bytes]) -> None:
    if expected.keys() != actual.keys():
        raise ReleaseCheckError(
            f"{label} runtime members differ: missing={sorted(expected.keys() - actual.keys())}, "
            f"extra={sorted(actual.keys() - expected.keys())}"
        )
    changed = [name for name in expected if expected[name] != actual[name]]
    if changed:
        raise ReleaseCheckError(f"{label} runtime bytes differ: {changed}")


def build(source: Path, artifacts: Path, uv: str) -> str:
    artifacts.mkdir()
    if importlib.util.find_spec("hatchling") is not None:
        script = (
            "import hatchling.build, sys; "
            "print(hatchling.build.build_sdist(sys.argv[1])); "
            "print(hatchling.build.build_wheel(sys.argv[1]))"
        )
        run([sys.executable, "-I", "-c", script, str(artifacts)], cwd=source)
        return "current-python-hatchling"
    run(
        [uv, "build", "--offline", "--no-python-downloads", "--python", sys.executable,
         "--sdist", "--wheel", "--out-dir", str(artifacts), str(source)],
        cwd=source,
    )
    return "uv-offline"


# -I ignores PYTHONPATH/current-directory injection but still processes editable
# .pth files inherited from the base environment. Assert every module's origin.
SMOKE_SCRIPT = r'''
import asyncio, importlib, importlib.metadata, json, pathlib, sys
venv = pathlib.Path(sys.prefix).resolve()
checkout = pathlib.Path(sys.argv[1]).resolve()
assert venv != pathlib.Path(sys.base_prefix).resolve(), 'not running in a venv'
import rcf
origin = pathlib.Path(rcf.__file__).resolve()
assert origin.is_relative_to(venv), f'rcf not from wheel venv: {origin}'
assert not origin.is_relative_to(checkout), f'rcf from editable checkout: {origin}'
for name in json.loads(sys.argv[2]):
    module = importlib.import_module(name)
    location = pathlib.Path(module.__file__).resolve()
    assert location.is_relative_to(venv), f'{name} not from wheel: {location}'
from rcf.mcp.server import build_server, TOOL_SPECS
names = sorted(tool.name for tool in asyncio.run(build_server().list_tools()))
assert len(set(names)) == len(names) and set(names) == {spec.name for spec in TOOL_SPECS}, names
entry_points = importlib.metadata.distribution('research-compute-fabric').entry_points
assert {'rcf', 'rcf-mcp'} <= {entry.name for entry in entry_points if entry.group == 'console_scripts'}
print(json.dumps({'rcf_file': str(origin), 'venv': str(venv), 'tool_count': len(names), 'tools': names}))
'''


def smoke(wheel: Path, source: Path, checkout: Path, uv: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="rcf-wheel-check-") as temporary:
        work = Path(temporary).resolve()
        if work.is_relative_to(checkout):
            raise ReleaseCheckError("temporary wheel validation directory must be outside checkout")
        environment = work / "venv"
        venv.EnvBuilder(system_site_packages=True, with_pip=False).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        # A venv made by a venv inherits its *base* interpreter's global sites,
        # not the invoking venv's dependencies. Reuse existing dependency sites
        # explicitly, but keep the wheel venv site first and verify every origin.
        purelib = Path(run(
            [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], cwd=work,
        ).strip())
        dependency_sites = [str(Path(path).resolve()) for path in site.getsitepackages() if Path(path).is_dir()]
        (purelib / "release_dependencies.pth").write_text(
            "import site; " + "; ".join(f"site.addsitedir({path!r})" for path in dependency_sites) + "\n",
            encoding="utf-8",
        )
        home = work / "rcf-home"
        home.mkdir()
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("RCF_") or key in {"PYTHONPATH", "PYTHONHOME"}:
                env.pop(key)
        env.update({"RCF_HOME": str(home), "HOME": str(work), "USERPROFILE": str(work),
                    "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never", "NO_COLOR": "1"})
        run(
            [uv, "pip", "install", "--offline", "--no-deps", "--python", str(python),
             "--reinstall", str(wheel)], cwd=work, env=env,
        )
        modules = []
        for name in runtime_sources(source):
            dotted = name.removesuffix(".py").replace("/", ".")
            if dotted.endswith(".__init__"):
                dotted = dotted.removesuffix(".__init__")
            # __main__ files execute CLI on import; CLI is checked below instead.
            if not dotted.endswith(".__main__"):
                modules.append(dotted)
        result = json.loads(run(
            [str(python), "-I", "-c", SMOKE_SCRIPT, str(checkout), json.dumps(modules)], cwd=work, env=env,
        ))
        cli = run(
            [str(python), "-I", "-m", "rcf.cli.main", "--home", str(home), "--json", "node", "list"],
            cwd=work, env=env,
        )
        if json.loads(cli) != []:
            raise ReleaseCheckError(f"empty-home CLI returned unexpected nodes: {cli}")
        result.update({"cli_nodes": [], "isolated": True, "offline_install": True,
                       "imported_runtime_modules": len(modules), "temporary_home": str(home),
                       "dependency_sites": dependency_sites})
        return result


def release_check(source: Path, output: Path) -> dict[str, Any]:
    source, output = source.resolve(), output.absolute()
    if output.exists() or output.is_symlink():
        raise ReleaseCheckError(f"output already exists; refusing overwrite: {output}")
    output = output.resolve()
    if output.is_relative_to(source) and output.relative_to(source).parts[0] in DELIVERY_ROOTS:
        raise ReleaseCheckError("output cannot be inside a delivery source directory")
    uv = shutil.which("uv")
    if not uv:
        raise ReleaseCheckError("uv is required for offline --no-deps wheel installation")
    output.mkdir(parents=True, exist_ok=False)
    evidence: dict[str, Any] = {
        "ok": False, "source": str(source), "output": str(output),
        "python": sys.version, "platform": platform.platform(),
        "started_at": datetime.now(UTC).isoformat(),
    }
    try:
        delivered = output / "source"
        manifest = snapshot(source, delivered)
        evidence["source_manifest_sha256"] = manifest
        artifacts = output / "artifacts"
        evidence["build_backend"] = build(delivered, artifacts, uv)
        wheels, sdists = sorted(artifacts.glob("*.whl")), sorted(artifacts.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ReleaseCheckError("build must produce exactly one wheel and one sdist")
        _compare_runtime("snapshot", runtime_sources(source), runtime_sources(delivered))
        evidence["artifact_checks"] = verify_artifacts(delivered, wheels[0], sdists[0])
        evidence["artifacts"] = [
            {"path": str(path), "sha256": sha256(path.read_bytes()), "bytes": path.stat().st_size}
            for path in [wheels[0], sdists[0]]
        ]
        evidence["smoke"] = smoke(wheels[0], delivered, source, uv)
        for name, digest in manifest.items():
            if sha256((delivered / name).read_bytes()) != digest or sha256((source / name).read_bytes()) != digest:
                raise ReleaseCheckError(f"source changed during release acceptance: {name}")
        if evidence["smoke"]["tool_count"] != 31:
            raise ReleaseCheckError(
                f"MCP tool count must be exactly 31, got {evidence['smoke']['tool_count']}: "
                f"{evidence['smoke']['tools']}"
            )
        evidence["ok"] = True
    except Exception as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        evidence["finished_at"] = datetime.now(UTC).isoformat()
        (output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, help="new directory only (default: source/.tmp/release-<uuid>)")
    args = parser.parse_args(argv)
    output = args.output or args.source / ".tmp" / f"release-{uuid.uuid4().hex}"
    try:
        evidence = release_check(args.source, output)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "output": str(output.absolute())}))
        return 1
    print(json.dumps(evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
