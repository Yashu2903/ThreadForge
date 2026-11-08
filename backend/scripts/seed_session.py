"""TEST-ONLY: create a user and a real session, and print the cookie, for browser tests.

    uv run python -m scripts.seed_session --name "Alice"
    uv run python -m scripts.seed_session --name "Bob" --join <room_id> --role editor

Browser (Playwright) tests cannot complete a real Google login, so they seed a logged-in session
here and set the cookie in the browser. The session is created by the SAME service function the
login flow uses (app.services.sessions.create_session), so every request still goes through the
real cookie -> hash -> lookup path. There is deliberately NO impersonation endpoint in the API:
this script needs direct access to the database, which only the operator of a test environment has.

Refuses to run with APP_ENV=production. Prints one JSON line: {"user_id", "cookie_name", "secret"}.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy import text

from app.config import load_settings
from app.db import create_engine, create_sessionmaker
from app.services.sessions import create_session


async def seed(name: str, join: str | None, role: str) -> dict[str, str]:
    settings = load_settings()
    if settings.is_production:
        raise SystemExit("seed_session refuses to run with APP_ENV=production.")
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    try:
        async with sessionmaker() as db, db.begin():
            user_id = (
                await db.execute(
                    text("INSERT INTO users (display_name) VALUES (:n) RETURNING id"), {"n": name}
                )
            ).scalar_one()
            if join:
                await db.execute(
                    text(
                        "INSERT INTO memberships (room_id, user_id, role, state)"
                        " VALUES (:r, :u, :role, 'active')"
                    ),
                    {"r": join, "u": user_id, "role": role},
                )
            session = await create_session(db, settings, user_id)
        return {
            "user_id": str(user_id),
            "cookie_name": settings.session_cookie_name,
            "secret": session.secret,
        }
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--join", help="room id to join directly (bypasses invitations)")
    parser.add_argument("--role", choices=["editor", "viewer"], default="editor")
    args = parser.parse_args()
    sys.stdout.write(json.dumps(asyncio.run(seed(args.name, args.join, args.role))) + "\n")


if __name__ == "__main__":
    main()
