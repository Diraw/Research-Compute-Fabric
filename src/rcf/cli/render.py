"""Model → terminal rendering helpers (human mode only; --json prints raw data)."""

from __future__ import annotations

from typing import Any

import typer

STATUS_MARK = {"ok": "+", "warn": "!", "fail": "x", "unknown": "?", "skipped": "-"}


def kv_block(title: str, rows: list[tuple[str, Any]]) -> None:
    typer.echo(typer.style(title, bold=True))
    width = max((len(k) for k, _ in rows), default=0)
    for key, value in rows:
        if value is None or value == "" or value == [] or value == {}:
            continue
        typer.echo(f"  {key.ljust(width)}  {_fmt(value)}")
    typer.echo("")


def table(headers: list[str], rows: list[list[Any]]) -> None:
    if not rows:
        typer.echo("  (none)")
        return
    widths = [len(h) for h in headers]
    rendered = [[_fmt(cell) for cell in row] for row in rows]
    for row in rendered:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(cell))
    typer.echo("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    typer.echo("  " + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rendered:
        typer.echo("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def checks(items: list[dict[str, Any]]) -> None:
    for check in items:
        mark = STATUS_MARK.get(check.get("status", "unknown"), "?")
        line = f"  [{mark}] {check.get('name', '?'):<24} {check.get('detail', '')}"
        style = {"ok": "green", "warn": "yellow", "fail": "red"}.get(check.get("status", ""), None)
        typer.echo(typer.style(line, fg=style) if style else line)


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items()) if value else ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_fmt(v) for v in value)
    return str(value)
