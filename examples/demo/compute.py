"""Small deterministic CPU workload requiring only the Python standard library."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.steps <= 1_000_000:
        parser.error("steps must be between 1 and 1000000")
    result = sum((step + args.seed) % 97 for step in range(args.steps))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "result.json").write_text(
        json.dumps({"steps": args.steps, "seed": args.seed, "result": result}) + "\n",
        encoding="utf-8",
    )
    print(f"Completed {args.steps} steps; result written to the declared output directory")


if __name__ == "__main__":
    main()
