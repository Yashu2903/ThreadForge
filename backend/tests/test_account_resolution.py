"""Concurrent first logins must produce exactly one user and one identity, never an orphan."""

import asyncio
import logging

import pytest

from app.services import accounts
from app.services.oidc import VerifiedIdentity
from tests.helpers import error_code, make_client, rows, scalar, sign_in, start_login

IDENTITY = VerifiedIdentity(
    issuer="https://accounts.google.com",
    subject="race-subject",
    name="Ada Lovelace",
    email="ada@example.com",
    email_verified=True,
)


async def assert_no_orphans(sessionmaker):
    assert await scalar(sessionmaker, "SELECT count(*) FROM users") == 1
    assert await scalar(sessionmaker, "SELECT count(*) FROM auth_identities") == 1
    orphans = await scalar(
        sessionmaker,
        "SELECT count(*) FROM users u WHERE NOT EXISTS "
        "(SELECT 1 FROM auth_identities i WHERE i.user_id = u.id)",
    )
    assert orphans == 0


async def test_concurrent_first_logins_for_same_identity_create_one_account(sessionmaker):
    # Each call opens its own AsyncSession from the sessionmaker (never shared across tasks).
    results = await asyncio.gather(
        *[accounts.resolve_account(sessionmaker, IDENTITY) for _ in range(8)]
    )
    assert len({r.user_id for r in results}) == 1
    assert all(r.state == "active" for r in results)
    await assert_no_orphans(sessionmaker)


async def test_lost_race_rolls_back_the_extra_user_and_returns_the_winner(
    sessionmaker, monkeypatch, caplog
):
    """Deterministically force the race: the identity already exists, but our first lookup
    misses it (as if a concurrent login committed between our SELECT and INSERT)."""
    caplog.set_level(logging.INFO, logger="threadforge")
    winner = await accounts.resolve_account(sessionmaker, IDENTITY)

    real_lookup = accounts._find_and_refresh
    calls = []

    async def lookup_that_misses_once(sm, identity):
        calls.append(1)
        return None if len(calls) == 1 else await real_lookup(sm, identity)

    monkeypatch.setattr(accounts, "_find_and_refresh", lookup_that_misses_once)
    loser = await accounts.resolve_account(sessionmaker, IDENTITY)

    assert loser.user_id == winner.user_id
    assert len(calls) == 2  # miss, then re-select after the unique violation
    assert "first-login race lost" in caplog.text
    await assert_no_orphans(sessionmaker)  # the loser's user insert was rolled back


async def test_non_identity_integrity_errors_are_not_swallowed(sessionmaker):
    from sqlalchemy.exc import IntegrityError

    bad = VerifiedIdentity(
        issuer="http://not-https.example", subject="s", name="x", email=None, email_verified=False
    )
    with pytest.raises(IntegrityError, match="ck_auth_identities_issuer_valid"):
        await accounts.resolve_account(sessionmaker, bad)
    assert await scalar(sessionmaker, "SELECT count(*) FROM users") == 0  # rolled back, no orphan


async def test_concurrent_http_first_logins_for_the_same_google_account(
    settings, engine, google, sessionmaker
):
    """Two browsers finish their first Google login at the same instant."""
    async with (
        make_client(settings, engine, google) as a,
        make_client(settings, engine, google) as b,
    ):
        flows = []
        for client in (a, b):
            login = await start_login(client)
            code, state = google.authorize(login.headers["location"], sub="same-person")
            flows.append((client, code, state))
        responses = await asyncio.gather(
            *[
                c.get("/auth/callback", params={"code": code, "state": state})
                for c, code, state in flows
            ]
        )
    assert [error_code(r) for r in responses] == [None, None]
    await assert_no_orphans(sessionmaker)
    assert await scalar(sessionmaker, "SELECT count(*) FROM sessions") == 2


async def test_identity_row_is_only_touched_when_something_changed(client, google, sessionmaker):
    await sign_in(client, google)
    [before] = await rows(sessionmaker, "SELECT updated_at FROM auth_identities")
    await sign_in(client, google)  # identical profile
    [after] = await rows(sessionmaker, "SELECT updated_at FROM auth_identities")
    assert after["updated_at"] == before["updated_at"]
