"""``rcf`` command line interface.

The CLI is a thin adapter over :class:`rcf.service.RcfService` — exactly the
same object the MCP server uses.  Every command supports ``--json`` so scripts
and Agents never have to parse human output.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer

from .. import __version__
from ..core.config import Config, default_home, write_starter_home
from ..core.errors import NotFound, RcfError
from ..core.ids import parse_iso
from ..core.models import RunRequest
from ..service import RcfService
from . import render


@dataclass
class CliState:
    home: Path | None
    as_json: bool


app = typer.Typer(
    no_args_is_help=True,
    invoke_without_command=True,
    add_completion=False,
    help="Research Compute Fabric — nodes, environments and runs for AI Agents.",
)
node_app = typer.Typer(no_args_is_help=True, help="Inspect compute nodes.")
project_app = typer.Typer(no_args_is_help=True, help="Inspect and sync projects.")
env_app = typer.Typer(no_args_is_help=True, help="Inspect environments and plan installs.")
run_app = typer.Typer(no_args_is_help=True, help="Plan, submit and observe runs.")
storage_app = typer.Typer(no_args_is_help=True, help="Inspect storage and review exact-file cleanup/adoption plans.")
db_app = typer.Typer(no_args_is_help=True, help="Local state database.")
mcp_app = typer.Typer(no_args_is_help=True, help="MCP server for Agents.")

app.add_typer(node_app, name="node")
app.add_typer(project_app, name="project")
app.add_typer(env_app, name="env")
app.add_typer(run_app, name="run")
app.add_typer(storage_app, name="storage")
app.add_typer(db_app, name="db")
app.add_typer(mcp_app, name="mcp")


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #


def _configure_stdio() -> None:
    """Force UTF-8 output so non-ASCII project names survive a GBK console."""

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover - exotic stream
            pass


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", is_eager=True, help="Print the RCF version and exit."
    ),
    home: Path | None = typer.Option(None, "--home", envvar="RCF_HOME", help="RCF home directory."),
    as_json: bool = typer.Option(
        False, "--json", help="Machine readable output (global; also via RCF_JSON=1)."
    ),
) -> None:
    _configure_stdio()
    if version:
        typer.echo(f"rcf {__version__}")
        raise typer.Exit()
    env_json = os.environ.get("RCF_JSON", "").strip().lower() in {"1", "true", "yes"}
    ctx.obj = CliState(home=home, as_json=as_json or env_json)


def _state(ctx: typer.Context) -> CliState:
    assert isinstance(ctx.obj, CliState)
    return ctx.obj


def _service(state: CliState, *, actor: str = "cli") -> RcfService:
    return RcfService(Config.load(state.home or default_home()), actor=actor)


@contextmanager
def _errors(state: CliState) -> Iterator[None]:
    try:
        yield
    except RcfError as exc:
        if state.as_json:
            typer.echo(json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False, indent=2))
        else:
            typer.secho(f"error: {exc.message}", fg="red", err=True)
            if exc.hint:
                typer.secho(f"hint:  {exc.hint}", fg="yellow", err=True)
        raise typer.Exit(exc.exit_code) from exc


def _emit(state: CliState, payload: Any, human: callable | None = None) -> None:
    if state.as_json or human is None:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    human(payload)


def _parse_param(token: str) -> tuple[str, Any]:
    key, sep, raw = token.partition("=")
    if not sep or not key.strip():
        raise typer.BadParameter(f"--param expects key=value, got {token!r}")
    value: Any = raw
    lowered = raw.strip().lower()
    if lowered in {"true", "false"}:
        value = lowered == "true"
    elif lowered in {"null", "none"}:
        value = None
    else:
        try:
            value = int(raw)
        except ValueError:
            try:
                value = float(raw)
            except ValueError:
                value = raw
    return key.strip(), value


# --------------------------------------------------------------------------- #
# top level
# --------------------------------------------------------------------------- #


@app.command()
def version() -> None:
    """Print the RCF version."""
    typer.echo(f"rcf {__version__}")


@app.command()
def init(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """Create a starter home directory (settings and registries)."""
    state = _state(ctx)
    home = state.home or default_home()
    created = write_starter_home(home, overwrite=force)
    _emit(
        state,
        {"home": str(home), "created": [str(p) for p in created]},
        lambda payload: (
            typer.echo(f"home: {payload['home']}"),
            typer.echo("created: " + (", ".join(Path(p).name for p in payload["created"]) or "nothing (already present)")),
            typer.echo("next: edit nodes.toml and projects.toml, then `rcf doctor`"),
        ),
    )


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check configuration, dependencies and security posture."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        typer.echo(("OK" if payload["ok"] else "PROBLEMS FOUND") + f"  home={payload['home']}")
        render.checks(payload["checks"])
        for warning in payload["warnings"]:
            typer.secho(f"  note: {warning}", fg="yellow")
        typer.echo("")
        typer.echo("security: " + json.dumps(payload["security"]))
        typer.echo("config files: " + (", ".join(payload["config_files"]) or "<none>"))

    with _errors(state):
        _emit(state, _service(state).doctor(), human)


# --------------------------------------------------------------------------- #
# node
# --------------------------------------------------------------------------- #


@node_app.command("list")
def node_list(ctx: typer.Context) -> None:
    """List declared nodes (no network access)."""
    state = _state(ctx)

    def human(rows: list[dict[str, Any]]) -> None:
        render.table(
            ["id", "kind", "ssh alias", "labels", "work root"],
            [
                [r["id"], r["kind"], r["ssh_alias"] or "-", r["labels"], r["work_root"]]
                for r in rows
            ],
        )

    with _errors(state):
        _emit(state, _service(state).list_nodes(), human)


@node_app.command("show")
def node_show(ctx: typer.Context, node_id: str = typer.Argument(...)) -> None:
    """Show the declared configuration of a node."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        render.kv_block(
            f"node {payload['id']}",
            [
                ("kind", payload["kind"]),
                ("name", payload["name"]),
                ("ssh alias", payload["ssh_alias"]),
                ("work root", payload["work_root"]),
                ("labels", payload["labels"]),
                ("partitions", payload["partitions"]),
                ("default partition", payload["default_partition"]),
                ("gpus expected", payload["gpus_expected"]),
            ],
        )

    with _errors(state):
        service = _service(state)
        matching = [n for n in service.list_nodes() if n["id"] == node_id]
        if not matching:
            raise NotFound(f"unknown node {node_id!r}", hint="run `rcf node list`")
        _emit(state, matching[0], human)


@node_app.command("inspect")
def node_inspect(
    ctx: typer.Context,
    node_id: str = typer.Argument(...),
    refresh: bool = typer.Option(True, "--refresh/--no-refresh", help="Bypass the probe cache."),
) -> None:
    """Probe a node (read-only) and report resources and GPU schedulability."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        summary = payload["summary"]
        render.kv_block(
            f"node {node_id} — {summary['adapter']} probe",
            [
                ("observed", summary["observed_at"]),
                ("cpus", summary["cpus"]),
                ("load 1m", summary["load_1m"]),
                ("memory", f"{summary['memory_available_mb']} / {summary['memory_total_mb']} MB available"),
                ("gpus", summary["gpus"]),
                ("partitions", summary["partitions"]),
            ],
        )
        render.checks([c for c in payload["snapshot"]["checks"]])
        for gpu in payload["snapshot"].get("gpus", []):
            typer.echo(
                f"  gpu{gpu['index']}: {gpu.get('name')} "
                f"{gpu.get('memory_used_mb')}/{gpu.get('memory_total_mb')} MB, "
                f"procs={len(gpu.get('processes') or [])}"
            )
        if payload["gpu_schedulability"]:
            typer.echo("\n  GPU schedulability (free VRAM ≠ available):")
            for item in payload["gpu_schedulability"]:
                typer.echo(f"    gpu{item['gpu_index']}: {item['status']} — {'; '.join(item['reasons'])}")

    with _errors(state):
        _emit(state, _service(state).inspect_node(node_id, refresh=refresh), human)


@node_app.command("selftest")
def node_selftest(ctx: typer.Context, node_id: str = typer.Argument(...)) -> None:
    """Prove the whole control path to a node works (takes ~10s)."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        typer.echo(f"node {payload['node_id']} — self-test ({payload['duration_s']}s)")
        render.checks(payload["checks"])
        typer.echo("")
        if payload["ok"]:
            typer.secho("link OK: shell, work_root, interpreter checked", fg="green")
        else:
            failed = [c["name"] for c in payload["checks"] if c["status"] == "fail"]
            typer.secho(f"link FAILED: {', '.join(failed)}", fg="red")
            typer.echo("see `rcf node inspect` and docs/security.md for the checklist")

    with _errors(state):
        _emit(state, _service(state).selftest_node(node_id), human)


# --------------------------------------------------------------------------- #
# project
# --------------------------------------------------------------------------- #


@project_app.command("list")
def project_list(ctx: typer.Context) -> None:
    """List declared projects."""
    state = _state(ctx)

    def human(rows: list[dict[str, Any]]) -> None:
        render.table(
            ["id", "env", "entrypoint", "paths"],
            [[r["id"], r["env_id"] or "-", r["entrypoint"], r["paths"]] for r in rows],
        )

    with _errors(state):
        _emit(state, _service(state).list_projects(), human)


@project_app.command("show")
def project_show(
    ctx: typer.Context,
    project_id: str = typer.Argument(...),
    node_id: str = typer.Option(..., "--node", help="Node to inspect the checkout on."),
    sync_plan: bool = typer.Option(False, "--sync-plan", help="Also show the sync plan."),
) -> None:
    """Inspect a project checkout on a node."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        view = payload["project"]
        render.kv_block(
            f"project {view['project_id']} on {view['node_id']}",
            [
                ("path", view["path"]),
                ("exists", view["exists"]),
                ("commit", view["git_commit"]),
                ("branch", view["git_branch"]),
                ("dirty", view["git_dirty"]),
            ],
        )
        render.checks(view["checks"])
        if "sync_plan" in payload:
            plan = payload["sync_plan"]
            typer.echo("\n  sync plan:")
            for step in plan["steps"]:
                typer.echo(f"    - {step['description']}: {' '.join(step['command'])}")
            for blocker in plan["blockers"]:
                typer.secho(f"    ! {blocker}", fg="yellow")

    with _errors(state):
        _emit(state, _service(state).inspect_project(project_id, node_id, sync_plan=sync_plan), human)


@project_app.command("sync")
def project_sync(
    ctx: typer.Context,
    project_id: str = typer.Argument(...),
    node_id: str = typer.Option(..., "--node"),
    apply: bool = typer.Option(False, "--apply", help="Actually run git fetch/checkout."),
    yes: bool = typer.Option(False, "--yes", help="Confirm the mutating action."),
) -> None:
    """Plan (default) or apply a git sync for a project checkout."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        plan = payload["plan"]
        render.kv_block(
            f"sync {plan['project_id']} on {plan['node_id']}",
            [
                ("current commit", plan["current_commit"]),
                ("target", plan["target_commit"]),
                ("applied", payload["applied"]),
            ],
        )
        for step in plan["steps"]:
            typer.echo(f"  - {step['description']}: {' '.join(step['command'])}")
        for blocker in plan["blockers"]:
            typer.secho(f"  ! {blocker}", fg="red")
        for result in payload.get("results", []):
            typer.echo(f"  [{result['returncode']}] {result['description']}")

    with _errors(state):
        _emit(state, _service(state).sync_project(project_id, node_id, apply=apply, confirmed=yes), human)


# --------------------------------------------------------------------------- #
# environment
# --------------------------------------------------------------------------- #


@env_app.command("list")
def env_list(
    ctx: typer.Context,
    node_id: str | None = typer.Option(None, "--node", help="Limit to one node."),
) -> None:
    """Inspect every declared environment (read-only)."""
    state = _state(ctx)

    def human(rows: list[dict[str, Any]]) -> None:
        render.table(
            ["env", "node", "state", "python", "path", "missing"],
            [
                [
                    r["env_id"],
                    r["node_id"],
                    r["state"],
                    r["python_version"] or "-",
                    r["path"] or "-",
                    r["missing_packages"],
                ]
                for r in rows
            ],
        )

    with _errors(state):
        _emit(state, _service(state).list_environments(node_id=node_id), human)


@env_app.command("show")
def env_show(
    ctx: typer.Context,
    env_id: str = typer.Argument(...),
    node_id: str = typer.Option(..., "--node"),
    verify: bool = typer.Option(False, "--verify", help="Run verification (requires --yes; prefer env verify)."),
    yes: bool = typer.Option(False, "--yes", help="Confirm execution of the verification command."),
) -> None:
    """Inspect one environment on one node."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        render.kv_block(
            f"environment {payload['env_id']} on {payload['node_id']}",
            [
                ("state", payload["state"]),
                ("path", payload["path"]),
                ("python", payload["python_version"]),
                ("fingerprint", payload["fingerprint"]),
                ("installed", payload.get("dist_count")),
                ("env digest", payload.get("env_digest")),
                ("verified at", payload.get("verified_at")),
                ("missing", payload["missing_packages"]),
            ],
        )
        render.checks(payload["checks"])

    with _errors(state):
        _emit(state, _service(state).inspect_environment(env_id, node_id, verify=verify, confirm=yes), human)


@env_app.command("verify")
def env_verify(
    ctx: typer.Context,
    env_id: str = typer.Argument(...),
    node_id: str = typer.Option(..., "--node"),
    yes: bool = typer.Option(False, "--yes", help="Confirm the configured verification command."),
) -> None:
    """Execute an environment verification command (not a read-only probe)."""
    state = _state(ctx)
    with _errors(state):
        _emit(state, _service(state).verify_environment(env_id, node_id, confirm=yes),
              lambda payload: render.kv_block("environment verification", [
                  ("environment", payload["env_id"]), ("node", payload["node_id"]),
                  ("state", payload["state"]),
              ]))


@env_app.command("plan")
def env_plan(
    ctx: typer.Context,
    env_id: str = typer.Argument(...),
    node_id: str = typer.Option(..., "--node"),
) -> None:
    """Show what installing this environment would do (no changes)."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        typer.echo(f"action: {payload['action']}  (requires authorization: {payload['requires_authorization']})")
        for step in payload["steps"]:
            typer.echo(f"  - {step['description']}\n      {' '.join(step['command'])}")
        for blocker in payload["blockers"]:
            typer.secho(f"  ! {blocker}", fg="red")
        render.checks(payload["checks"])

    with _errors(state):
        _emit(state, _service(state).plan_environment(env_id, node_id), human)


@env_app.command("lock")
def env_lock(
    ctx: typer.Context,
    env_id: str = typer.Argument(...),
    node_id: str = typer.Option(..., "--node"),
    out: Path | None = typer.Option(None, "--out", help="Write the manifest to this file instead of stdout."),
) -> None:
    """Print a rebuildable package list for an environment (conda --explicit / pip freeze)."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        text = payload["lock"]
        if out is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            typer.echo(f"wrote {out}  ({len(text.splitlines())} lines)")
        else:
            typer.echo(text, nl=False)

    with _errors(state):
        _emit(state, _service(state).lock_environment(env_id, node_id), human)


@env_app.command("apply")
def env_apply(
    ctx: typer.Context,
    env_id: str = typer.Argument(...),
    node_id: str = typer.Option(..., "--node"),
    yes: bool = typer.Option(False, "--yes", help="Confirm the mutation."),
) -> None:
    """Execute a previously reviewed environment plan (needs opt-in in config)."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        for result in payload["results"]:
            typer.echo(f"  [{result['returncode']}] {result['description']}")
            if result["output"]:
                typer.echo(f"      {result['output'][:300]}")

    with _errors(state):
        _emit(state, _service(state).apply_environment(env_id, node_id, confirmed=yes), human)


@project_app.command("push")
def project_push(
    ctx: typer.Context,
    project_id: str = typer.Argument(...),
    node_id: str = typer.Option(..., "--node", help="Target node to copy the checkout to."),
    source_node: str | None = typer.Option(
        None, "--from", help="Node to copy from (default: the local checkout)."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite files that are newer on the target."),
    data: bool = typer.Option(
        False, "--data", help="Copy the data root (data_paths) instead of the checkout."
    ),
    yes: bool = typer.Option(False, "--yes", help="Actually copy (default is a dry-run plan)."),
) -> None:
    """Copy a project checkout (or its data root) to a node, sending only what differs."""
    scope = "data" if data else "code"
    state = _state(ctx)
    service = _service(state)

    def human(payload: dict[str, Any]) -> None:
        if "target_root" in payload and "files" in payload:  # report
            render.kv_block(
                f"pushed {payload['project_id']} to {payload['target_node']}",
                [
                    ("target", payload["target_root"]),
                    ("files", payload["files"]),
                    ("bytes", payload["bytes"]),
                    ("unchanged", payload["unchanged"]),
                    ("verified", payload["verified"]),
                ],
            )
            return
        render.kv_block(
            f"push {payload['project_id']} ({payload['scope']}): "
            f"{payload['source_node']} -> {payload['target_node']}",
            [
                ("from", payload["source_root"]),
                ("to", payload["target_root"]),
                ("new", len(payload["new"])),
                ("changed", len(payload["changed"])),
                ("unchanged", payload["unchanged"]),
                ("bytes", payload["bytes"]),
            ],
        )
        for entry in payload["new"][:10]:
            typer.echo(f"  + {entry['path']}  ({entry['size']} B)")
        if len(payload["new"]) > 10:
            typer.echo(f"  + ... {len(payload['new']) - 10} more new")
        for entry in payload["changed"][:10]:
            typer.echo(f"  ~ {entry['path']}  ({entry['size']} B)")
        if len(payload["changed"]) > 10:
            typer.echo(f"  ~ ... {len(payload['changed']) - 10} more changed")
        for entry in payload["remote_newer"][:10]:
            typer.secho(f"  ! {entry['path']} is newer on the target; left alone", fg="yellow")
        render.checks(payload["checks"])
        if not payload["new"] and not payload["changed"]:
            typer.secho("Nothing to copy; the target already matches.", fg="green")
            return
        typer.echo("")
        typer.secho("Review the list, then copy:", fg="green")
        typer.echo(
            f"  rcf project push {payload['project_id']} --node {payload['target_node']}"
            f" --from {payload['source_node']} --yes"
        )

    with _errors(state):
        if yes:
            _emit(
                state,
                service.push_project(
                    project_id,
                    node_id,
                    source_node_id=source_node,
                    force=force,
                    confirm=yes,
                    scope=scope,
                ),
                human,
            )
        else:
            _emit(
                state,
                service.push_project_plan(
                    project_id, node_id, source_node_id=source_node, force=force, scope=scope
                ),
                human,
            )


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #


class CleanupKind(StrEnum):
    stale_code = "stale-code"
    disposable = "disposable"
    replica = "replica"
    cache = "cache"


class AdoptionKind(StrEnum):
    code = "code"
    input = "input"
    output = "output"
    disposable = "disposable"
    cache = "cache"


def _storage_human(title: str, payload: dict[str, Any], *, plan: bool = False) -> None:
    """Keep all evidence visible without assuming a particular report shape."""
    typer.secho(title, bold=True)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if plan and payload.get("plan_id"):
        typer.echo("No files changed. Review the exact paths and blockers before applying:")
        typer.echo(f"  rcf storage apply {payload['plan_id']} --yes")


@storage_app.command("scan")
def storage_scan(
    ctx: typer.Context,
    project_id: str = typer.Argument(...),
    node_id: str = typer.Option(..., "--node"),
) -> None:
    """Scan declared storage roots without changing research files."""
    state = _state(ctx)
    with _errors(state):
        _emit(state, _service(state).scan_storage(project_id, node_id),
              lambda payload: _storage_human("Storage scan", payload))


@storage_app.command("report")
def storage_report(
    ctx: typer.Context,
    project_id: str = typer.Argument(...),
    node_id: str | None = typer.Option(None, "--node"),
    largest: int = typer.Option(20, "--largest", min=0, help="Number of largest files to show."),
) -> None:
    """Report stored file statistics and largest files."""
    state = _state(ctx)
    with _errors(state):
        _emit(state, _service(state).report_storage(project_id, node_id, largest=largest),
              lambda payload: _storage_human("Storage report", payload))


@storage_app.command("check")
def storage_check(
    ctx: typer.Context,
    project_id: str = typer.Argument(...),
    node_id: str | None = typer.Option(None, "--node"),
) -> None:
    """Check storage evidence and safety issues without deleting files."""
    state = _state(ctx)
    with _errors(state):
        _emit(state, _service(state).check_storage(project_id, node_id),
              lambda payload: _storage_human("Storage checks", payload))


@storage_app.command("plan")
def storage_plan(
    ctx: typer.Context,
    project_id: str = typer.Argument(...),
    node_id: str = typer.Option(..., "--node"),
    kind: CleanupKind = typer.Option(CleanupKind.disposable, "--kind"),
    root_id: str | None = typer.Option(None, "--root", "--root-id"),
    source_node_id: str | None = typer.Option(None, "--from", "--source-node"),
    run_id: str | None = typer.Option(None, "--run"),
) -> None:
    """Plan exact-file cleanup only; nothing is deleted."""
    state = _state(ctx)
    with _errors(state):
        _emit(
            state,
            _service(state).plan_storage_cleanup(
                project_id, node_id, kind=kind.value, root_id=root_id,
                source_node_id=source_node_id, run_id=run_id,
            ),
            lambda payload: _storage_human("Storage cleanup plan (not applied)", payload, plan=True),
        )


@storage_app.command("adopt")
def storage_adopt(
    ctx: typer.Context,
    project_id: str = typer.Argument(...),
    node_id: str = typer.Option(..., "--node"),
    root_id: str = typer.Option(..., "--root", "--root-id"),
    paths: list[str] = typer.Option(..., "--path", help="Explicit relative file path; repeat for each file."),
    kind: AdoptionKind = typer.Option(AdoptionKind.output, "--kind"),
    run_id: str | None = typer.Option(None, "--run"),
    source_node_id: str | None = typer.Option(
        None, "--from", help="Source node; required for code adoption and verified against it.",
    ),
) -> None:
    """Plan adoption of explicitly selected files; never defaults to a whole root."""
    state = _state(ctx)
    with _errors(state):
        _emit(
            state,
            _service(state).plan_storage_adoption(
                project_id, node_id, root_id=root_id, paths=paths, kind=kind.value,
                run_id=run_id, source_node_id=source_node_id,
            ),
            lambda payload: _storage_human("Storage adoption plan (not applied)", payload, plan=True),
        )


@storage_app.command("show")
def storage_show(
    ctx: typer.Context,
    plan_id: str = typer.Argument(...),
) -> None:
    """Show stored plan, owner/stage, journal and protection evidence without applying it."""
    state = _state(ctx)
    with _errors(state):
        _emit(state, _service(state).get_storage_plan(plan_id),
              lambda payload: _storage_human("Storage plan evidence (read-only)", payload))


@storage_app.command("reconcile")
def storage_reconcile(
    ctx: typer.Context,
    plan_id: str = typer.Argument(...),
    yes: bool = typer.Option(
        False, "--yes", help="Confirm closing this old plan without deleting any more files.",
    ),
) -> None:
    """Observe and close an old plan; never resume deletion. Requires explicit --yes."""
    state = _state(ctx)
    with _errors(state):
        _emit(state, _service(state).reconcile_storage_plan(plan_id, confirm=yes),
              lambda payload: _storage_human("Storage plan reconciliation (no deletion)", payload))


@storage_app.command("apply")
def storage_apply(
    ctx: typer.Context,
    plan_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", help="Confirm this reviewed cleanup or adoption plan."),
    resume: bool = typer.Option(
        False, "--resume",
        help="Continue this exact interrupted plan after reviewing its journal; never reselect files.",
    ),
) -> None:
    """Apply one reviewed storage plan; requires a fresh explicit --yes."""
    state = _state(ctx)
    with _errors(state):
        _emit(state, _service(state).apply_storage_plan(plan_id, confirm=yes, resume=resume),
              lambda payload: _storage_human("Storage plan result", payload))


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


@run_app.command("pin")
def run_pin(
    ctx: typer.Context,
    run_id: str = typer.Argument(...),
    reason: str = typer.Option("", "--reason", help="Required explanation for retaining this run."),
    yes: bool = typer.Option(False, "--yes", help="Confirm changing run retention."),
) -> None:
    """Pin a run against cleanup (service validates the required reason)."""
    state = _state(ctx)
    with _errors(state):
        _emit(state, _service(state).pin_run(run_id, pinned=True, reason=reason, confirm=yes),
              lambda payload: _storage_human("Run retention: pin", payload))


@run_app.command("unpin")
def run_unpin(
    ctx: typer.Context,
    run_id: str = typer.Argument(...),
    reason: str = typer.Option("", "--reason", help="Explanation for changing retention."),
    yes: bool = typer.Option(False, "--yes", help="Confirm changing run retention."),
) -> None:
    """Remove a run's cleanup pin; does not delete files."""
    state = _state(ctx)
    with _errors(state):
        _emit(state, _service(state).pin_run(run_id, pinned=False, reason=reason, confirm=yes),
              lambda payload: _storage_human("Run retention: unpin", payload))


def _inputs_summary(manifest: dict[str, Any] | None) -> str:
    if not manifest:
        return "-"
    return f"{len(manifest['files'])} files, {manifest['total_bytes']} bytes, sha256 {manifest['digest'][:12]}"


def _expiry_text(created_at: str, ttl_minutes: int) -> str:
    if ttl_minutes <= 0:
        return "never"
    created = parse_iso(created_at)
    if created is None:
        return "unknown"
    deadline = created + timedelta(minutes=ttl_minutes)
    return f"{deadline.isoformat(timespec='minutes')} ({ttl_minutes} min after plan)"


class WaitUntil(StrEnum):
    change = "change"
    terminal = "terminal"
    phase = "phase"
    alert = "alert"


def _positive_seconds(value: float | None) -> float | None:
    if value is not None and (not math.isfinite(value) or value <= 0):
        raise typer.BadParameter("must be a finite number greater than zero")
    return value


def _wait_seconds(value: float) -> float:
    if not math.isfinite(value) or not 0 <= value <= 60:
        raise typer.BadParameter("must be a finite number between 0 and 60")
    return value


def _observed_label(payload: dict[str, Any]) -> str:
    """Compact freshness marker: an old claim must not read like a live one."""

    seconds = payload.get("observed_age_seconds")
    if seconds is None:
        return "-"
    if seconds < 90:
        label = f"{seconds:.0f}s"
    elif seconds < 5400:
        label = f"{seconds / 60:.0f}m"
    elif seconds < 172800:
        label = f"{seconds / 3600:.0f}h"
    else:
        label = f"{seconds / 86400:.0f}d"
    return f"{label} STALE" if payload.get("stale") else label


def _progress_human(payload: dict[str, Any]) -> None:
    """Keep progress evidence readable without dumping potentially long sample tails."""
    progress = payload.get("progress")
    if not isinstance(progress, dict):
        progress = payload
    eta = progress.get("eta")
    if not isinstance(eta, dict):
        eta = {}

    def value(key: str, *aliases: str, default: Any = "unknown") -> Any:
        for name in (key, *aliases):
            result = progress.get(name)
            if result is not None:
                return result
        return default

    lower = eta.get("lower_seconds", progress.get("eta_lower_seconds"))
    upper = eta.get("upper_seconds", progress.get("eta_upper_seconds"))
    bounds = progress.get("eta_range_seconds")
    if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
        lower, upper = bounds
    interval = f"{lower}..{upper} seconds" if lower is not None and upper is not None else "unknown (不可估)"
    render.kv_block(
        f"progress {payload.get('run_id', '')}",
        [
            ("status", value("status")),
            ("phase", value("phase")),
            ("completed / total", f"{value('completed')} / {value('total')}"),
            ("fraction", value("fraction")),
            ("rate", value("rate", "rate_per_second")),
            ("elapsed seconds", value("elapsed_seconds")),
            ("ETA interval", interval),
            ("ETA scope", eta.get("scope") or value("eta_scope", "scope")),
            ("confidence", eta.get("confidence") or value("confidence")),
            ("reason", eta.get("reason") or value("reason", "eta_reason")),
            ("alerts", value("alerts", default=[]) or "none"),
            ("last report", value("last_report_at", "last_report")),
            ("last advance", value("last_advance_at", "last_advance")),
            ("last report age seconds", value("last_report_age_seconds")),
            ("last advance age seconds", value("last_advance_age_seconds")),
            ("revision", payload.get("revision", progress.get("revision", "unknown"))),
        ],
    )


@run_app.command("progress")
def run_progress(ctx: typer.Context, run_id: str = typer.Argument(...)) -> None:
    """Read progress and evidence-based ETA; never submit or cancel a run."""
    state = _state(ctx)
    with _errors(state):
        _emit(state, _service(state).get_run_progress(run_id, refresh_state=True), _progress_human)


@run_app.command("wait")
def run_wait(
    ctx: typer.Context,
    run_id: str = typer.Argument(...),
    timeout: float = typer.Option(
        30, "--timeout", min=0, max=60, callback=_wait_seconds,
        help="Bounded long-poll seconds (0..60, capped by [progress].max_wait_seconds); zero reads cache, not a background notification.",
    ),
    after: str | None = typer.Option(None, "--after", help="Previously observed revision."),
    until: WaitUntil = typer.Option(WaitUntil.change, "--until"),
) -> None:
    """Wait read-only for change, terminal state, phase or alert; never submit/cancel."""
    state = _state(ctx)
    with _errors(state):
        _emit(
            state,
            _service(state).wait_run(
                run_id, timeout_seconds=timeout, after_revision=after, until=until.value,
            ),
            _progress_human,
        )


@run_app.command("plan")
def run_plan(
    ctx: typer.Context,
    project_id: str = typer.Option(..., "--project", "-p"),
    node_id: str | None = typer.Option(None, "--node"),
    env_id: str | None = typer.Option(None, "--env"),
    gpus: int = typer.Option(0, "--gpus"),
    cpus: int | None = typer.Option(None, "--cpus"),
    tasks: int = typer.Option(1, "--tasks", help="MPI ranks (srun/mpirun)."),
    memory_gb: int | None = typer.Option(None, "--mem", help="System memory in GB."),
    gpu_memory_gb: int | None = typer.Option(
        None, "--gpu-mem", help="Minimum VRAM per GPU in GB (allows sharing a busy device that fits)."
    ),
    minutes: int | None = typer.Option(None, "--minutes", help="Walltime limit."),
    partition: str | None = typer.Option(None, "--partition"),
    command: str | None = typer.Option(None, "--command", help="Override the project entrypoint."),
    param: list[str] = typer.Option([], "--param", help="key=value (repeatable)."),
    allow_shared_gpu: bool = typer.Option(False, "--allow-shared-gpu"),
    pilot_seconds: int | None = typer.Option(
        None, "--pilot-seconds", min=1, max=300, help="Calibration pilot with a hard watchdog (1..300 seconds).",
    ),
    runtime_budget_seconds: float | None = typer.Option(
        None, "--runtime-budget-seconds", callback=_positive_seconds,
        help="Runtime planning/alert budget; does not cancel a normal run and is not an ETA.",
    ),
) -> None:
    """Build a reviewable execution plan (no side effects)."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        typer.echo(f"plan {payload['plan_id']}  run {payload['run_id']}")
        render.kv_block(
            "target",
            [
                ("node", payload["node_id"]),
                ("executor", payload["executor"]),
                ("workdir", payload["workdir"]),
                ("run dir", payload["run_dir"]),
                ("outputs", payload["result_dir"]),
                ("environment", payload.get("env_id")),
                ("inputs", _inputs_summary(payload.get("inputs"))),
                ("expires", _expiry_text(payload["created_at"], _service(state).config.settings.runs.plan_ttl_minutes)),
                ("fingerprint", payload["fingerprint"]),
            ],
        )
        typer.echo(typer.style("command", bold=True))
        typer.echo(payload["command"]["preview"])
        typer.echo("")
        render.checks(payload["checks"])
        for warning in payload["warnings"]:
            typer.secho(f"  ! {warning}", fg="yellow")
        blockers = [c for c in payload["checks"] if c["status"] == "fail"]
        typer.echo("")
        if blockers:
            typer.secho(f"BLOCKED by {len(blockers)} issue(s); submit will be refused.", fg="red")
        else:
            typer.secho("Plan is executable. Review it, then:", fg="green")
            typer.echo(f"  rcf run submit {payload['plan_id']} --yes")

    with _errors(state):
        params = dict(_parse_param(token) for token in param)
        request = RunRequest(
            project_id=project_id,
            node_id=node_id,
            env_id=env_id,
            params=params,
            command_override=command,
            pilot_seconds=pilot_seconds,
            runtime_budget_seconds=runtime_budget_seconds,
            requirements={
                "gpus": gpus,
                "cpus": cpus,
                "tasks": tasks,
                "memory_gb": memory_gb,
                "gpu_memory_gb": gpu_memory_gb,
                "time_minutes": minutes,
                "partition": partition,
                "allow_shared_gpu": allow_shared_gpu,
            },
        )
        _emit(state, _service(state).plan_run(request), human)


@run_app.command("submit")
def run_submit(
    ctx: typer.Context,
    plan_id: str = typer.Argument(...),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    yes: bool = typer.Option(False, "--yes", help="Confirm submission."),
) -> None:
    """Submit a plan to its executor."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        render.kv_block(
            f"run {payload['run_id']}",
            [
                ("state", payload["state"]),
                ("node", payload["node_id"]),
                ("executor", payload["executor"]),
                ("job id", (payload.get("handle") or {}).get("job_id")),
                ("workdir", payload["workdir"]),
                ("log", payload["log_path"]),
            ],
        )
        typer.echo(f"next: rcf run status {payload['run_id']}")

    with _errors(state):
        _emit(state, _service(state).submit_run(plan_id, idempotency_key=idempotency_key, confirm=yes), human)


@run_app.command("status")
def run_status(
    ctx: typer.Context,
    run_id: str = typer.Argument(...),
    refresh: bool = typer.Option(True, "--refresh/--no-refresh"),
) -> None:
    """Show a run, refreshing from the executor unless --no-refresh."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        render.kv_block(
            f"run {payload['run_id']}",
            [
                ("state", payload["state"]),
                ("node", payload["node_id"]),
                ("executor", payload["executor"]),
                ("job id", (payload.get("handle") or {}).get("job_id")),
                ("commit", (payload.get("git_commit") or "")[:12] or None),
                ("dirty", payload.get("git_dirty")),
                ("exit code", payload["exit_code"]),
                ("created", payload["created_at"]),
                ("submitted", payload["submitted_at"]),
                ("finished", payload["finished_at"]),
                ("observed", _observed_label(payload)),
                ("log", payload["log_path"]),
                ("error", payload["error"]),
                ("diagnosis", (payload.get("diagnosis") or {}).get("message")),
            ],
        )
        if isinstance(payload.get("progress"), dict):
            _progress_human(payload)
        if payload.get("contract_violation"):
            typer.secho(
                "  ! the run succeeded but its output directory is empty "
                "(artifact contract not honoured)",
                fg="yellow",
            )
        if payload.get("events"):
            typer.echo("  events:")
            for event in payload["events"][-10:]:
                typer.echo(f"    {event['at']}  {event['kind']:<14} {event['message'][:100]}")

    with _errors(state):
        _emit(state, _service(state).get_run(run_id, refresh=refresh), human)


@run_app.command("list")
def run_list(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit"),
    state_filter: str | None = typer.Option(None, "--state"),
) -> None:
    """List recent runs from the local database."""
    state = _state(ctx)

    def human(rows: list[dict[str, Any]]) -> None:
        render.table(
            ["run id", "state", "observed", "node", "project", "created", "exit"],
            [
                [
                    r["run_id"],
                    r["state"],
                    _observed_label(r),
                    r["node_id"],
                    r["project_id"] or "-",
                    r["created_at"],
                    r["exit_code"],
                ]
                for r in rows
            ],
        )

    with _errors(state):
        _emit(state, _service(state).list_runs(limit=limit, state=state_filter), human)


@run_app.command("logs")
def run_logs(
    ctx: typer.Context,
    run_id: str = typer.Argument(...),
    tail: int = typer.Option(200, "--tail"),
) -> None:
    """Read the tail of a run log."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        typer.secho(f"# {payload['run_id']} [{payload['state']}] last {payload['tail']} lines", fg="bright_black")
        typer.echo(payload["log"])

    with _errors(state):
        _emit(state, _service(state).get_logs(run_id, tail=tail), human)


@run_app.command("cancel")
def run_cancel(
    ctx: typer.Context,
    run_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Cancel a run (scancel / signal)."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        typer.echo(f"run {payload['run_id']} → {payload['state']}")

    with _errors(state):
        _emit(state, _service(state).cancel_run(run_id, confirm=yes), human)


@run_app.command("results")
def run_results(ctx: typer.Context, run_id: str = typer.Argument(...)) -> None:
    """List result files produced by a run (in place, no transfer)."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        typer.echo(f"result dir: {payload['result_dir']}")
        render.table(
            ["path", "size"],
            [[e["path"], e["size"]] for e in payload["entries"]],
        )
        if payload.get("workdir_changes"):
            typer.secho(
                "\nfiles changed in the project directory during this run "
                "(diagnostic, not collected):",
                fg="yellow",
            )
            render.table(
                ["path", "size"],
                [[e["path"], e["size"]] for e in payload["workdir_changes"]],
            )
        typer.echo(f"\n{payload['note']}")

    with _errors(state):
        _emit(state, _service(state).collect_results(run_id), human)


@run_app.command("fetch")
def run_fetch(
    ctx: typer.Context,
    run_id: str = typer.Argument(...),
    dest: Path = typer.Option(..., "--dest", "-d", help="Local directory to write into."),
    force: bool = typer.Option(False, "--force", help="Merge into a non-empty directory."),
    yes: bool = typer.Option(False, "--yes", help="Confirm writing to the local disk."),
) -> None:
    """Copy a run's artifacts here (tar + scp, SHA-256 verified)."""
    state = _state(ctx)

    def human(payload: dict[str, Any]) -> None:
        render.kv_block(
            f"fetched {payload['run_id']}",
            [
                ("destination", payload["dest"]),
                ("source", payload["source"]),
                ("files", payload["files"]),
                ("bytes", payload["bytes"]),
                ("sha256", payload.get("sha256")),
                ("verified", payload["verified"]),
            ],
        )
        typer.echo("next: open the files in the destination directory")

    with _errors(state):
        _emit(
            state,
            _service(state).fetch_results(run_id, dest=str(dest), force=force, confirm=yes),
            human,
        )


@run_app.command("events")
def run_events(
    ctx: typer.Context,
    run_id: str = typer.Argument(...),
    limit: int = typer.Option(100, "--limit"),
) -> None:
    """Show the persisted event trail of a run."""
    state = _state(ctx)

    def human(rows: list[dict[str, Any]]) -> None:
        render.table(["at", "kind", "message"], [[r["at"], r["kind"], r["message"][:80]] for r in rows])

    with _errors(state):
        service = _service(state)
        _emit(state, [e.model_dump(mode="json") for e in service.runs.events(run_id, limit=limit)], human)


# --------------------------------------------------------------------------- #
# db / mcp
# --------------------------------------------------------------------------- #


@db_app.command("path")
def db_path(ctx: typer.Context) -> None:
    """Print the local database path."""
    state = _state(ctx)
    _emit(state, {"database": str(_service(state).config.db_path)})


@db_app.command("audit")
def db_audit(ctx: typer.Context, limit: int = typer.Option(50, "--limit")) -> None:
    """Show the audit trail of state-changing operations."""
    state = _state(ctx)

    def human(rows: list[dict[str, Any]]) -> None:
        render.table(
            ["at", "operation", "tier", "decision", "node", "run"],
            [
                [r["at"], r["operation"], r["tier"], r["decision"], r["node_id"] or "-", r["run_id"] or "-"]
                for r in rows
            ],
        )

    with _errors(state):
        _emit(state, _service(state).store.list_audits(limit=limit), human)


@mcp_app.command("serve")
def mcp_serve(
    ctx: typer.Context,
    transport: str = typer.Option("stdio", "--transport", help="stdio | sse | streamable-http"),
) -> None:
    """Run the MCP server (stdio by default, for Agent clients)."""
    state = _state(ctx)
    from ..mcp.server import build_server

    server = build_server(home=str(state.home) if state.home else None, actor="mcp")
    typer.secho(f"rcf mcp server starting (transport={transport})", fg="bright_black", err=True)
    server.run(transport)  # type: ignore[arg-type]


@mcp_app.command("tools")
def mcp_tools(ctx: typer.Context) -> None:
    """List the MCP tools exposed to Agents."""
    state = _state(ctx)
    from ..mcp.server import TOOL_SPECS

    payload = [
        {
            "tool": spec.name,
            "aliases": [spec.dotted],
            "read_only": spec.read_only,
            "cli": "rcf " + " ".join(spec.cli_path),
            "description": spec.description.strip().splitlines()[0],
        }
        for spec in TOOL_SPECS
    ]

    def human(rows: list[dict[str, Any]]) -> None:
        render.table(["tool", "kind", "cli"], [[r["tool"], "ro" if r["read_only"] else "rw", r["cli"]] for r in rows])

    with _errors(state):
        _emit(state, payload, human)


if __name__ == "__main__":  # pragma: no cover
    app()
