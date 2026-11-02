"""Run the bundled CPU example locally, using an isolated temporary RCF home.

Run after installing RCF into the current Python environment. No network,
remote nodes, dependency installation, Git changes or deletion are performed.
The temporary home and results are deliberately retained for inspection.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main() -> None:
    home = Path(tempfile.mkdtemp(prefix="rcf-example-"))
    project = Path(__file__).resolve().parent / "demo"
    # JSON quoted strings are valid TOML basic strings, including Windows paths.
    quote = lambda value: json.dumps(str(value))  # noqa: E731
    (home / "rcf.toml").write_text(
        '[security]\nallow_execute = true\nrequire_confirmation = true\n', encoding="utf-8",
    )
    (home / "nodes.toml").write_text('[nodes.local]\nkind = "local"\n', encoding="utf-8")
    (home / "environments.toml").write_text(
        '[environments.system-py]\nkind = "system"\npython = ' + quote(sys.executable) + '\n',
        encoding="utf-8",
    )
    (home / "projects.toml").write_text(
        '[projects.demo]\nscript = "compute.py"\nenv_id = "system-py"\n'
        'entrypoint = "{python} {script} --steps {steps} --seed {seed} --out {output_dir}"\n'
        'outputs = "outputs"\npaths = { local = ' + quote(project) + ' }\n', encoding="utf-8",
    )

    def cli(*args: str):
        command = [sys.executable, "-m", "rcf.cli.main", "--home", str(home), "--json", *args]
        result = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8")
        return json.loads(result.stdout)

    print(f"Isolated RCF home retained at: {home}")
    plan = cli("run", "plan", "--project", "demo", "--node", "local", "--param", "steps=1000",
               "--param", "seed=1", "--minutes", "1")
    if any(check["status"] == "fail" for check in plan["checks"]):
        raise RuntimeError("Local plan blocked: " + json.dumps(plan["checks"]))
    print(f"Plan {plan['plan_id']}: {plan['command']['preview']}")
    run = cli("run", "submit", plan["plan_id"], "--yes")
    run_id = run["run_id"]
    deadline = time.monotonic() + 30
    while True:
        run = cli("run", "status", run_id)
        if run["state"] in {"succeeded", "failed", "cancelled", "lost"}:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Local example not terminal yet; inspect {run_id} using --home {home}")
        time.sleep(0.2)
    if run["state"] != "succeeded" or run["exit_code"] != 0:
        raise RuntimeError(f"Local example failed; inspect {run_id} using --home {home}")
    print(f"Run {run_id}: succeeded")
    print(cli("run", "logs", run_id)["log"])
    result = cli("run", "results", run_id)
    output = Path(result["result_dir"]) / "result.json"
    payload = json.loads(output.read_text(encoding="utf-8"))
    if payload != {"steps": 1000, "seed": 1, "result": 47025}:
        raise RuntimeError("Unexpected deterministic example result")
    print(f"Verified result: {output}")


if __name__ == "__main__":
    main()
