"""The evaluation harness is not part of the normal suite: it only runs when asked.

    RUN_EVAL=1 uv run pytest tests/eval -s                       # against the local provider stubs
    RUN_EVAL=1 EVAL_PROVIDER=real AI_... uv run pytest tests/eval -s   # against the real providers

See docs/ai.md, "Evaluation".
"""

import os

import pytest


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    if os.environ.get("RUN_EVAL") == "1":
        return
    skip = pytest.mark.skip(reason="evaluation harness: set RUN_EVAL=1 to run it (not part of CI)")
    for item in items:
        if (
            "tests/eval" in item.nodeid.replace("\\", "/")
            and item.fspath.basename == "test_eval.py"
        ):
            item.add_marker(skip)
