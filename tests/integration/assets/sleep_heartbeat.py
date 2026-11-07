"""Heartbeat for long-run control-plane tests.

A long job needs evidence that grows while it runs, otherwise "still running"
cannot be distinguished from "the log stopped growing an hour ago". This prints
one flushed line per interval and exits after ``--seconds``.
"""

from __future__ import annotations

import argparse
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=3900)
    parser.add_argument("--interval", type=float, default=30)
    args = parser.parse_args()

    start = time.monotonic()
    index = 0
    while True:
        elapsed = time.monotonic() - start
        print(f"heartbeat {index:04d} elapsed={elapsed:.0f}s", flush=True)
        index += 1
        remaining = args.seconds - elapsed
        if remaining <= 0:
            break
        time.sleep(min(args.interval, remaining))
    print(f"done after {time.monotonic() - start:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
