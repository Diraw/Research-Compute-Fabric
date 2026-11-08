"""Turn one request with a parameter grid into the list of combinations to run.

A sweep is not "submit in a loop".  The value is that every combination is planned
*before* anything is submitted, so a grid with one unschedulable point fails loudly
instead of spending twenty minutes submitting and then stopping at number seventeen.

Nothing here talks to a node or the store; expansion is a pure function so the
order and the limits can be tested without a cluster.
"""

from __future__ import annotations

import itertools
from typing import Any

from ..core.errors import SpecError

#: Refuse a grid that would queue more jobs than this in one command.  A typo in a
#: range should cost an error message, not a cluster-wide surprise.
MAX_COMBOS = 200


def expand_grid(
    grid: dict[str, list[Any]], *, max_combos: int = MAX_COMBOS
) -> list[dict[str, Any]]:
    """Cartesian product of the varying parameters, in a stable order.

    The last parameter varies fastest, which is what a shell loop over nested
    ``for`` statements produces and therefore what the reader expects to see in a
    list of combinations.
    """

    if not grid:
        raise SpecError(
            "a sweep needs at least one varying parameter",
            hint="pass --grid name=v1,v2 (and --param name=value for the fixed ones)",
            detail={"grid": {}},
        )
    for name, values in grid.items():
        if not values:
            raise SpecError(
                f"--grid {name} has no values",
                hint="give at least one value: --grid name=v1",
                detail={"param": name},
            )
    total = 1
    for values in grid.values():
        total *= len(values)
    if total > max_combos:
        raise SpecError(
            f"this grid expands to {total} runs, above the {max_combos} limit",
            hint="narrow the grid, or raise the limit deliberately with --max-runs",
            detail={"combinations": total, "limit": max_combos},
        )
    names = list(grid)
    return [
        dict(zip(names, values, strict=True))
        for values in itertools.product(*(grid[name] for name in names))
    ]
