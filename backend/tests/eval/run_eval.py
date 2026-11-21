"""Run an evaluation through pytest and record its TRUE exit status.

    cd backend
    uv run --env-file ../.env python -m tests.eval.run_eval proposal      # the gate
    uv run --env-file ../.env python -m tests.eval.run_eval answer
    QUALITY_SPLIT=dev uv run --env-file ../.env python -m tests.eval.run_eval quality

Why a runner: a shell wrapper such as `pytest ...; echo done` (or a trailing logging command) reports the status of its LAST
command, so a failed evaluation can look like a success, and the reverse. Here the evaluator is the only thing whose status
is reported: this process exits with exactly pytest's status, and writes it, with the command, to
`results/run-status-<kind>-<stamp>.json` next to the results.

Pytest's own statuses: 0 passed; 1 tests failed (for the proposal evaluation that includes the gate failing); 2 interrupted;
3 internal error; 4 command-line usage error; 5 no tests collected. The runner adds none of its own.

The test and evaluation code ignores `.env` files, so load keys with `uv run --env-file ../.env` as above.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[2]
RESULTS = Path(__file__).parent / "results"
TARGETS = {
    "proposal": "tests/eval/test_proposal_eval.py",
    "answer": "tests/eval/test_eval.py",
    "quality": "tests/eval/test_quality_eval.py",  # QUALITY_SPLIT=dev|heldout|all is read from the environment
}
MEANING = {
    0: "passed",
    1: "tests failed (for the proposal evaluation this includes the acceptance gate)",
    2: "interrupted",
    3: "internal error",
    4: "command-line usage error",
    5: "no tests collected",
}


def run(
    kind: str,
    *,
    command: list[str] | None = None,
    env: dict[str, str] | None = None,
    results_dir: Path = RESULTS,
) -> int:
    """Run the evaluation and return the evaluator's own exit status (also recorded)."""
    cmd = command or [
        sys.executable,
        "-m",
        "pytest",
        TARGETS[kind],
        "-s",
        "-p",
        "no:cacheprovider",
    ]
    started = dt.datetime.now(dt.UTC)
    child_env = {**os.environ, "RUN_EVAL": "1", "EVAL_PROVIDER": "real", **(env or {})}
    status = subprocess.run(cmd, cwd=BACKEND, env=child_env, check=False).returncode  # noqa: S603
    record: dict[str, Any] = {
        "kind": kind,
        # the executable by name only: an absolute path would put a local user name into a file that gets committed
        "command": [Path(cmd[0]).name, *cmd[1:]],
        "exit_status": status,
        "meaning": MEANING.get(status, "other status"),
        "started_utc": started.isoformat(),
        "finished_utc": dt.datetime.now(dt.UTC).isoformat(),
        "note": "exit_status is the evaluator process's own status, not that of any command that ran after it",
    }
    results_dir.mkdir(exist_ok=True)
    stamp = started.strftime("%Y%m%d-%H%M%S")
    (results_dir / f"run-status-{kind}-{stamp}.json").write_text(
        json.dumps(record, indent=2), encoding="utf-8"
    )
    return status


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in TARGETS:
        print(f"usage: python -m tests.eval.run_eval {{{'|'.join(TARGETS)}}}", file=sys.stderr)
        return 4
    status = run(argv[0])
    print(f"\n{argv[0]} evaluation: pytest exit status {status} ({MEANING.get(status, 'other')})")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
