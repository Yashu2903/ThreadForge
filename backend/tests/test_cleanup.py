"""The maintenance command deletes only what it should, in batches."""

import asyncio
import hashlib
import os
import subprocess
import sys
from datetime import timedelta

from app.commands.cleanup_auth import cleanup_auth
from tests.db_utils import BACKEND_DIR
from tests.factories import TEST_DATABASE_URL
from tests.helpers import execute, rows, scalar


def iv(text: str | None) -> timedelta | None:
    """'-2 days' / '+5 minutes' / '-1 hour' -> timedelta (asyncpg needs real intervals)."""
    if text is None:
        return None
    amount, unit = text.split()
    unit = unit.rstrip("s") + "s"
    return timedelta(**{unit: int(amount)})


def hex_of(name: str) -> str:
    """Readable test names -> valid 64-char hex values (the tables CHECK the hash format)."""
    return hashlib.sha256(name.encode()).hexdigest()


async def seed_user(sessionmaker):
    await execute(sessionmaker, "INSERT INTO users (display_name) VALUES ('Ada')")
    return await scalar(sessionmaker, "SELECT id FROM users")


async def add_session(sessionmaker, user, name, *, expires, revoked=None):
    """expires/revoked are SQL intervals relative to now(): '-2 days', '+1 day', or None."""
    await execute(
        sessionmaker,
        "INSERT INTO sessions (user_id, secret_hash, created_at, expires_at, revoked_at) "
        "VALUES (:u, :h, now() - interval '30 days', now() + CAST(:e AS interval), "
        "now() + CAST(:r AS interval))",
        u=user,
        h=hex_of(name),
        e=iv(expires),
        r=iv(revoked),
    )


async def add_attempt(sessionmaker, name, *, expires):
    await execute(
        sessionmaker,
        "INSERT INTO oidc_login_attempts (state_hash, browser_binding_hash, nonce, pkce_verifier, "
        "created_at, expires_at) VALUES (:s, :b, :n, :v, now() - interval '30 days', "
        "now() + CAST(:e AS interval))",
        s=hex_of(name),
        b="b" * 64,
        n="n" * 32,
        v="v" * 43,
        e=iv(expires),
    )


async def remaining(sessionmaker, table, names):
    column = "secret_hash" if table == "sessions" else "state_hash"
    present = {r[column] for r in await rows(sessionmaker, f"SELECT {column} FROM {table}")}
    return {name for name in names if hex_of(name) in present}


ATTEMPT_NAMES = ("keep-pending", "drop-expired")
SESSION_NAMES = (
    "keep-live",
    "keep-just-expired",
    "drop-long-expired",
    "keep-recently-revoked",
    "drop-old-revoked",
)


async def test_deletes_only_expired_attempts_and_stale_sessions(sessionmaker):
    user = await seed_user(sessionmaker)
    await add_attempt(sessionmaker, "keep-pending", expires="+5 minutes")
    await add_attempt(sessionmaker, "drop-expired", expires="-1 minute")

    await add_session(sessionmaker, user, "keep-live", expires="+3 days")
    await add_session(sessionmaker, user, "keep-just-expired", expires="-1 hour")  # within 1 day
    await add_session(sessionmaker, user, "drop-long-expired", expires="-2 days")
    await add_session(
        sessionmaker, user, "keep-recently-revoked", expires="+3 days", revoked="-1 day"
    )
    await add_session(sessionmaker, user, "drop-old-revoked", expires="+3 days", revoked="-8 days")

    result = await cleanup_auth(sessionmaker)

    assert (result.login_attempts, result.sessions) == (1, 2)
    assert await remaining(sessionmaker, "oidc_login_attempts", ATTEMPT_NAMES) == {"keep-pending"}
    assert await remaining(sessionmaker, "sessions", SESSION_NAMES) == {
        "keep-live",
        "keep-just-expired",
        "keep-recently-revoked",
    }
    # Users and identities are never touched.
    assert await scalar(sessionmaker, "SELECT count(*) FROM users") == 1


async def test_retention_is_configurable(sessionmaker):
    user = await seed_user(sessionmaker)
    await add_session(sessionmaker, user, "expired-1h", expires="-1 hour")
    await add_session(sessionmaker, user, "revoked-1d", expires="+3 days", revoked="-1 day")
    result = await cleanup_auth(sessionmaker, expired_retention_days=0, revoked_retention_days=0)
    assert result.sessions == 2


async def test_works_in_batches_and_deletes_everything_eligible(sessionmaker):
    for i in range(7):
        await add_attempt(sessionmaker, f"old-{i}", expires="-1 minute")
    user = await seed_user(sessionmaker)
    for i in range(5):
        await add_session(sessionmaker, user, f"gone-{i}", expires="-3 days")
    await add_session(sessionmaker, user, "stays", expires="+1 day")

    result = await cleanup_auth(sessionmaker, batch_size=2)

    assert (result.login_attempts, result.sessions) == (7, 5)
    assert await scalar(sessionmaker, "SELECT count(*) FROM oidc_login_attempts") == 0
    assert await scalar(sessionmaker, "SELECT count(*) FROM sessions") == 1


async def test_idempotent_on_a_clean_database(sessionmaker):
    result = await cleanup_auth(sessionmaker)
    assert (result.login_attempts, result.sessions) == (0, 0)


async def test_command_line_entry_point_runs_end_to_end(sessionmaker):
    user = await seed_user(sessionmaker)
    await add_session(sessionmaker, user, "gone", expires="-3 days")
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL}
    completed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "app.commands.cleanup_auth"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "deleted 0 login attempts and 1 sessions" in completed.stdout
    assert await scalar(sessionmaker, "SELECT count(*) FROM sessions") == 0


def test_command_line_rejects_invalid_arguments():
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL}
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "app.commands.cleanup_auth", "--batch-size", "0"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 2
    assert "batch size" in completed.stderr
