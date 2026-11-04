"""A real local job through the full plan → submit → observe → fetch path.

This uses an isolated RCF home and a temporary project, so it never touches the
user's registry, existing runs or output directories.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from _support import enabled, job_seconds

from rcf.core.config import Config
from rcf.service import RcfService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not enabled(), reason="set RCF_IT=1 to run integration tests"),
]

SETTINGS = """\
[security]
allow_execute = true
allow_environment_mutation = false
allow_system_admin = false
require_confirmation = true

[defaults]
inspect_cache_seconds = 0
probe_timeout = 30
"""

WORK_PY = """\
import argparse
import pathlib
import time

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=float, required=True)
parser.add_argument("--out", required=True)
args = parser.parse_args()

time.sleep(args.seconds)

out = pathlib.Path(args.out)
out.mkdir(parents=True, exist_ok=True)
(out / "result.txt").write_text("ok", encoding="utf-8")
"""

TERMINAL = {"succeeded", "failed", "cancelled", "lost"}


def test_local_plan_submit_observe_and_fetch(tmp_path: Path):
    seconds = job_seconds()
    project = tmp_path / "proj"
    project.mkdir()
    (project / "work.py").write_text(WORK_PY, encoding="utf-8")

    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    (home / "rcf.toml").write_text(SETTINGS, encoding="utf-8")
    (home / "nodes.toml").write_text('[nodes.laptop]\nkind = "local"\n', encoding="utf-8")
    project_posix = str(project).replace("\\", "/")
    (home / "projects.toml").write_text(
        f"""\
[projects.demo]
entrypoint = "{{python}} {{script}} --seconds {{seconds}} --out {{output_dir}}"
script = "work.py"
outputs = "outputs"
output_contract = "argument"
paths = {{ "laptop" = "{project_posix}" }}
""",
        encoding="utf-8",
    )
    service = RcfService(Config.load(home), actor="integration")
    try:
        plan = service.plan_run(
            {
                "project_id": "demo",
                "params": {"seconds": seconds},
                "requirements": {"cpus": 1, "time_minutes": 2},
            }
        )
        assert not [check for check in plan["checks"] if check["status"] == "fail"]

        record = service.submit_run(plan["plan_id"], confirm=True)
        run_id = record["run_id"]

        deadline = time.monotonic() + seconds + 30
        payload = service.get_run(run_id)
        while payload["state"] not in TERMINAL and time.monotonic() < deadline:
            time.sleep(0.5)
            payload = service.get_run(run_id)

        assert payload["state"] == "succeeded", payload
        assert payload["observed_age_seconds"] is not None
        assert payload["stale"] is False

        manifest = service.collect_results(run_id)
        assert [entry["path"] for entry in manifest["entries"]] == ["result.txt"]

        dest = tmp_path / "fetched"
        fetched = service.fetch_results(run_id, dest=str(dest), confirm=True)
        assert fetched["verified"] is True
        assert (dest / "result.txt").read_text(encoding="utf-8") == "ok"
    finally:
        service.close()
