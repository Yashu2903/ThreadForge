"""Run the whole backend test suite in sequential batches.

    uv run python scripts/run_suite_in_batches.py [--batches 5] [-- extra pytest args]

One single pytest run of this suite is killed by some hosts for low memory (many real servers
and database connections live at once), so the files are split into batches run one after another,
each in its own process. The totals are printed at the end; exit status is non-zero if any batch fails.
"""

from __future__ import annotations

import argparse
import re
import subprocess  # noqa: S404 - the operator tool runs pytest with a fixed argv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--batches", type=int, default=5)
    args, extra = parser.parse_known_args()
    extra = [a for a in extra if a != "--"]
    files = sorted(
        str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "tests").glob("test_*.py")
    )
    # balance by size so no batch gets all the heavy real-server files
    files.sort(key=lambda f: -(ROOT / f).stat().st_size)
    batches: list[list[str]] = [[] for _ in range(args.batches)]
    loads = [0] * args.batches
    for f in files:
        i = loads.index(min(loads))
        batches[i].append(f)
        loads[i] += (ROOT / f).stat().st_size
    totals = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    status = 0
    for number, batch in enumerate(batches, start=1):
        print(f"--- batch {number}/{len(batches)}: {len(batch)} files", flush=True)
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *extra, *batch],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        tail = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
        print(tail, flush=True)
        for key in totals:
            found = re.search(rf"(\d+) {key}", tail)
            totals[key] += int(found.group(1)) if found else 0
        if done.returncode != 0:
            status = 1
            print(done.stdout[-6000:], flush=True)
    print("TOTAL", totals, flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
