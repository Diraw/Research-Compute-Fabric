"""Render saved runs as one self-contained HTML page.

There is no server on purpose.  A report is a *projection* of the database: it can
be regenerated at any time, it cannot drift out of sync with what RCF knows, and it
cannot be mistaken for live data because it says when it was generated.

Two rules are inherited from the rest of the tool and are the reason this is not a
generic dashboard:

* A state that was never observed is never shown as healthy.  A stale ``running``
  claim is labelled ``unverified`` with its age, because RCF keeps the last known
  state on purpose.
* A failure whose kind is not recorded is shown as ``unknown``, not as a blank
  cell.  A blank reads as "fine", which is exactly the wrong conclusion.

No JavaScript, no fonts, no external stylesheet: the file has to survive being
scp'd to a machine that cannot reach anything, and ``publication_check`` should
have nothing to say about it.
"""

from __future__ import annotations

import html
import re
from typing import Any

#: Colour per failure kind.  ``domain`` is deliberately not red: the work ran and
#: reported a result, and painting half of all failures red trains the reader to
#: ignore red.
_KIND_COLOURS = {
    "domain": "#8a6d3b",
    "environment": "#b23c3c",
    "input": "#b2733c",
    "parameter": "#b2733c",
    "code": "#b23c3c",
    "unknown": "#6b7280",
}

_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 1120px; padding: 32px 40px 64px; background: #fafafa; color: #1f2328;
       font: 14px/1.5 -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; font-weight: 600; }
h2 { font-size: 15px; margin: 32px 0 10px; font-weight: 600; letter-spacing: .01em; }
.sub { color: #6b7280; font-size: 12.5px; margin-bottom: 24px; }
.cards { display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0 8px; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px 16px; min-width: 108px; }
.card .n { font-size: 22px; font-weight: 600; line-height: 1.2; }
.card .k { color: #6b7280; font-size: 12px; }
table { border-collapse: collapse; width: 100%; background: #fff; border: 1px solid #e5e7eb;
        border-radius: 8px; overflow: hidden; font-size: 13px; }
th { text-align: left; font-weight: 600; color: #57606a; background: #f6f8fa;
     padding: 8px 10px; border-bottom: 1px solid #e5e7eb; white-space: nowrap; }
td { padding: 7px 10px; border-bottom: 1px solid #f0f1f3; vertical-align: top; }
tr:last-child td { border-bottom: 0; }
td.id, td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
td.wrap { max-width: 460px; overflow-wrap: anywhere; color: #3d444d; }
.mono { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 12.5px; }
.badge { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11.5px;
         border: 1px solid currentColor; white-space: nowrap; }
.warn { color: #8a6d3b; } .bad { color: #b23c3c; } .muted { color: #6b7280; } .good { color: #2c7a4b; }
.note { color: #6b7280; font-size: 12px; margin: 6px 0 0; }
.empty { color: #6b7280; font-style: italic; padding: 10px 0; }
.kindbar { display: inline-block; height: 8px; border-radius: 2px; min-width: 3px; }
table.summary { width: auto; min-width: 480px; }
table.trend { width: auto; }
.scroll { overflow-x: auto; }
"""


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "never observed"
    return f"{_duration(seconds)} ago"


def _state_cell(run: dict[str, Any]) -> str:
    state = _esc(run["state"])
    if run.get("stale"):
        return (
            f'{state} <span class="badge warn" title="the saved state was not re-read">'
            f"unverified {_esc(_age(run.get('observed_age_seconds')))}</span>"
        )
    if run["state"] == "succeeded":
        return f'<span class="good">{state}</span>'
    if run["state"] in {"failed", "lost"}:
        return f'<span class="bad">{state}</span>'
    return state


_KIND_PREFIX = re.compile(r"^(domain|environment|input|parameter|code|unknown): ")


def _evidence(text: str | None, kind: str | None) -> str:
    """Drop the ``domain: `` prefix a classification event carries.

    The kind already has its own column; repeating it in every evidence cell is
    noise that pushes the actual finding out of view.
    """

    if not text:
        return "-"
    return _KIND_PREFIX.sub("", text, count=1) if kind else text


def _kind_badge(kind: str | None) -> str:
    label = kind or "unknown"
    colour = _KIND_COLOURS.get(label, "#6b7280")
    title = {
        "domain": "the program finished and reported a negative result",
        "environment": "a package, binary, library or resource was missing",
        "input": "a file or field the program needs was absent",
        "parameter": "the arguments were invalid for the program",
        "code": "the program raised inside itself",
        "unknown": "no decisive evidence was recorded",
    }.get(label, "")
    return (
        f'<span class="badge" style="color:{colour}" title="{_esc(title)}">{_esc(label)}</span>'
    )


def _trend_section(trend: dict[str, Any]) -> list[str]:
    """Kind over time: what appeared, when, and against how many runs.

    Deliberately a matrix of counts rather than a line chart.  A line invites the
    reader to extend it, and with this many failures the extension would be
    invented; the counts plus their denominators are what the data actually says.
    """

    buckets, kinds = trend["buckets"], trend["kinds"]
    if not kinds or not buckets:
        return ['<div class="empty">no failed runs in scope</div>']

    headers = ["kind"] + [f"{bucket['label']} ({bucket['finished']})" for bucket in buckets]
    headers.append("total")
    rows = []
    for row in kinds:
        cells = [_kind_badge(row["kind"])]
        for count in row["counts"]:
            # A zero is a real observation (nothing of this kind failed then), so it
            # keeps its column instead of collapsing the row to a sparse list.
            cells.append(
                f'<span class="num">{count}</span>' if count else '<span class="muted">·</span>'
            )
        cells.append(f'<span class="num">{row["total"]}</span>')
        rows.append(cells)

    weekly = trend["bucket"] == "week"
    unit = "week" if weekly else "day"
    note = (
        f"Columns are {unit}s"
        + (" (starting Monday)" if weekly else "")
        + f"; the number in brackets is how many runs finished in that {unit}, so a "
        "cell can be read against its denominator. These are counts, not rates: with a "
        "few dozen failures a difference of one is not a trend."
    )
    if trend.get("truncated"):
        note += (
            f" The first column folds everything before "
            f"{trend.get('folded_before') or 'the window'} into one count."
        )
    return [
        '<div class="scroll">'
        + _table(
            headers,
            rows,
            classes=["", *["num"] * len(buckets), "num"],
            css_class="trend",
        )
        + "</div>",
        f'<p class="note">{note}</p>',
    ]


def _table(
    headers: list[str],
    rows: list[list[str]],
    *,
    classes: list[str] | None = None,
    css_class: str = "",
) -> str:
    head = "".join(
        f'<th class="{(classes[i] if classes else "")}">{_esc(name)}</th>'
        for i, name in enumerate(headers)
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="{(classes[i] if classes else "")}">{cell}</td>' for i, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    name = f' class="{css_class}"' if css_class else ""
    return f"<table{name}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_report(data: dict[str, Any]) -> str:
    """Return the whole page as one string."""

    counts = data["counts"]
    filters = data["filters"]
    scope = []
    scope.append(f"newest {filters['limit']} scanned")
    if filters.get("since"):
        scope.append(f"since {filters['since']}")
    if filters.get("project"):
        scope.append(f"project {filters['project']}")

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>RCF report</title><style>{_CSS}</style></head><body>",
        "<h1>RCF report</h1>",
        f'<div class="sub">Generated {_esc(data["generated_at"])} from '
        f'<span class="mono">{_esc(data["home"])}</span> &middot; {_esc(", ".join(scope))}'
        f' &middot; {counts["total"]} runs</div>',
        '<div class="cards">',
        f'<div class="card"><div class="n">{counts["total"]}</div><div class="k">runs in scope</div></div>',
        f'<div class="card"><div class="n good">{counts["succeeded"]}</div><div class="k">succeeded</div></div>',
        f'<div class="card"><div class="n">{counts["failed"]}</div><div class="k">failed</div></div>',
        f'<div class="card"><div class="n">{counts["cancelled"]}</div><div class="k">cancelled</div></div>',
        f'<div class="card"><div class="n">{counts["active"]}</div><div class="k">not terminal</div></div>',
        "</div>",
    ]

    # -- what is happening now ------------------------------------------------
    parts.append("<h2>Not finished</h2>")
    if not data["active"]:
        parts.append('<div class="empty">nothing is running or queued</div>')
    else:
        parts.append(
            _table(
                ["run", "state", "node", "project", "submitted", "last confirmed"],
                [
                    [
                        f'<span class="mono">{_esc(run["run_id"])}</span>',
                        _state_cell(run),
                        _esc(run["node_id"]),
                        _esc(run["project_id"] or "-"),
                        _esc(run.get("submitted_at") or "-"),
                        _esc(_age(run.get("observed_age_seconds"))),
                    ]
                    for run in data["active"]
                ],
                classes=["id", "", "", "", "mono", ""],
            )
        )
        parts.append(
            '<p class="note">A state is a saved claim. A run marked '
            "<em>unverified</em> keeps its last known state because the last probe "
            "did not answer; it may have finished since.</p>"
        )

    # -- why runs failed ------------------------------------------------------
    parts.append("<h2>Failures by kind</h2>")
    if not data["failures"]:
        parts.append('<div class="empty">no failed runs in scope</div>')
    else:
        parts.append(
            _table(
                ["kind", "runs", "share", ""],
                [
                    [
                        _kind_badge(group["kind"]),
                        f'<span class="num">{group["count"]}</span>',
                        f'<span class="num">{group["share"]:.0%}</span>',
                        f'<span class="kindbar" style="width:{max(3, group["share"] * 180):.0f}px;'
                        f'background:{_KIND_COLOURS.get(group["kind"] or "unknown", "#6b7280")};'
                        'opacity:.35"></span>',
                    ]
                    for group in data["failures"]
                ],
                classes=["", "num", "num", ""],
                css_class="summary",
            )
        )
        parts.append(
            '<p class="note"><strong>domain</strong> means the job ran to completion and '
            "reported a negative result; nothing is broken and the next step is another "
            "configuration. <strong>unknown</strong> means no decisive evidence was "
            "recorded, not that the run was fine.</p>"
        )
        parts.append("<h2>When each kind appeared</h2>")
        parts.extend(_trend_section(data.get("trend") or {}))

        detail_rows = []
        for group in data["failures"]:
            for run in group["runs"]:
                detail_rows.append(
                    [
                        _kind_badge(group["kind"]),
                        f'<span class="mono">{_esc(run["run_id"])}</span>',
                        _esc(run["project_id"] or "-"),
                        _esc(run["node_id"]),
                        _esc(run.get("exit_code")),
                        _esc(run.get("created_at") or "-"),
                        _esc(_evidence(run.get("evidence"), group["kind"])),
                    ]
                )
        parts.append("<h2>Failed runs</h2>")
        parts.append(
            _table(
                ["kind", "run", "project", "node", "exit", "created", "evidence"],
                detail_rows,
                classes=["", "id", "", "", "num", "mono", "wrap"],
            )
        )

    # -- per project ----------------------------------------------------------
    parts.append("<h2>Projects</h2>")
    if not data["projects"]:
        parts.append('<div class="empty">no projects in scope</div>')
    else:
        parts.append(
            _table(
                ["project", "runs", "succeeded", "failed", "success", "p50", "longest"],
                [
                    [
                        _esc(row["project_id"] or "-"),
                        f'<span class="num">{row["total"]}</span>',
                        f'<span class="num">{row["succeeded"]}</span>',
                        f'<span class="num">{row["failed"]}</span>',
                        f'<span class="num">{row["success_rate"]:.0%}</span>',
                        _esc(_duration(row["p50_duration_s"])),
                        _esc(_duration(row["max_duration_s"])),
                    ]
                    for row in data["projects"]
                ],
                classes=["", "num", "num", "num", "num", "num", "num"],
            )
        )

    parts.append(
        '<p class="note">Durations are wall clock from submission to the observed '
        "finish, so a queued job includes its queue time.</p>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)
