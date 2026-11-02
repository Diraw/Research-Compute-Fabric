"""A real local job must not open a console window (Windows).

The supervisor is spawned by RCF and then starts the job process. If either step
allocates a console, the user sees a terminal flashing in the foreground — which
is exactly what happened before ``CREATE_NO_WINDOW`` was used. This test checks
the grandchild's own view of its console: the handle must be 0.
"""

from __future__ import annotations

import os
import time

import pytest
from conftest import make_home, toml_path

from rcf.core.config import Config
from rcf.core.runner import SubprocessRunner
from rcf.service import RcfService

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.name != "nt", reason="Windows console semantics"),
]

SCRIPT = """import ctypes
import os
import pathlib

out = pathlib.Path(os.environ["RCF_OUTPUT_DIR"])
out.mkdir(parents=True, exist_ok=True)
(out / "hwnd.txt").write_text(str(ctypes.windll.kernel32.GetConsoleWindow()))
print("done")
"""


def test_local_job_has_no_console_window(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "probe.py").write_text(SCRIPT, encoding="utf-8")
    home = make_home(
        tmp_path,
        nodes='[nodes.laptop]\nkind = "local"\n',
        projects=f"""\
[projects.probe]
entrypoint = "{{python}} {{script}}"
output_contract = "env"
script = "probe.py"
outputs = "outputs"
paths = {{ "laptop" = "{toml_path(project)}" }}
""",
    )
    service = RcfService(Config.load(home), runner=SubprocessRunner(), actor="test")
    try:
        plan = service.plan_run({"project_id": "probe"})
        record = service.submit_run(plan["plan_id"], confirm=True)
        deadline = time.time() + 30
        state = record["state"]
        while time.time() < deadline and state not in {"succeeded", "failed", "lost"}:
            time.sleep(0.2)
            state = service.get_run(record["run_id"])["state"]
        assert state == "succeeded", service.get_logs(record["run_id"])["log"]

        result_dir = service.get_run(record["run_id"])["result_dir"]
        hwnd = (type(project)(result_dir) / "hwnd.txt").read_text().strip()
        assert hwnd == "0", f"job process got a console window (hwnd={hwnd})"
    finally:
        service.close()
