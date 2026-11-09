"""Turn the global AI switch back on (see ai_disable). Needs only DATABASE_URL.

    python -m app.commands.ai_enable [--note "why"]

This clears the operator kill switch only. AI also needs AI_ENABLED=true and valid settings.
"""

from __future__ import annotations

from app.commands.ai_disable import run


def main(argv: list[str] | None = None) -> int:
    return run(True, argv)


if __name__ == "__main__":
    raise SystemExit(main())
