"""Database-level guarantees: uniqueness, CHECKs, RESTRICT foreign keys and defaults.

These test the schema itself, so they use raw SQL inserts rather than the application code.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

HEX64 = "a" * 64
HEX64_B = "b" * 64


async def exec_sql(sessionmaker, sql: str, **params):
    async with sessionmaker() as session:
        result = await session.execute(text(sql), params)
        await session.commit()
        return result


async def make_user(sessionmaker, name="Ada") -> uuid.UUID:
    async with sessionmaker() as session:
        row = await session.execute(
            text("INSERT INTO users (display_name) VALUES (:n) RETURNING id"), {"n": name}
        )
        await session.commit()
        return row.scalar_one()


async def make_session_row(sessionmaker, user_id, secret_hash=HEX64):
    await exec_sql(
        sessionmaker,
        "INSERT INTO sessions (user_id, secret_hash, expires_at) "
        "VALUES (:u, :h, now() + interval '1 day')",
        u=user_id,
        h=secret_hash,
    )


async def test_user_defaults(sessionmaker):
    async with sessionmaker() as session:
        row = (
            (
                await session.execute(
                    text("INSERT INTO users (display_name) VALUES ('Ada') RETURNING *")
                )
            )
            .mappings()
            .one()
        )
        await session.commit()
    assert isinstance(row["id"], uuid.UUID)
    assert row["state"] == "active"
    assert row["version"] == 0
    assert row["email_verified"] is False
    assert row["email"] is None
    assert row["created_at"].tzinfo is not None
    assert row["created_at"].utcoffset().total_seconds() == 0
    assert abs((datetime.now(UTC) - row["created_at"]).total_seconds()) < 60


async def test_email_is_not_unique(sessionmaker):
    for _ in range(2):
        await exec_sql(
            sessionmaker,
            "INSERT INTO users (display_name, email) VALUES ('A', 'same@example.com')",
        )


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        ("INSERT INTO users (display_name, state) VALUES ('A', 'banned')", "ck_users_state_valid"),
        ("INSERT INTO users (display_name) VALUES ('')", "ck_users_display_name_length"),
        ("INSERT INTO users (display_name) VALUES ('   ')", "ck_users_display_name_length"),
        ("INSERT INTO users (display_name) VALUES (repeat('x', 101))", "too long"),
        (
            "INSERT INTO users (display_name, version) VALUES ('A', -1)",
            "ck_users_version_non_negative",
        ),
        ("INSERT INTO users (display_name, email) VALUES ('A', '')", "ck_users_email_length"),
    ],
)
async def test_user_checks(sessionmaker, statement, constraint):
    with pytest.raises(Exception, match=constraint):
        await exec_sql(sessionmaker, statement)


async def test_identity_unique_on_issuer_and_subject(sessionmaker):
    u1, u2 = await make_user(sessionmaker), await make_user(sessionmaker, "Bob")
    insert = "INSERT INTO auth_identities (user_id, issuer, subject) VALUES (:u, :i, :s)"
    await exec_sql(sessionmaker, insert, u=u1, i="https://accounts.google.com", s="123")
    # Same subject under a different issuer is a different identity.
    await exec_sql(sessionmaker, insert, u=u1, i="https://other.example", s="123")
    with pytest.raises(IntegrityError, match="uq_auth_identities_issuer_subject"):
        await exec_sql(sessionmaker, insert, u=u2, i="https://accounts.google.com", s="123")


@pytest.mark.parametrize(
    ("issuer", "subject", "constraint"),
    [
        ("http://insecure.example", "1", "ck_auth_identities_issuer_valid"),
        ("accounts.google.com", "1", "ck_auth_identities_issuer_valid"),
        ("https://accounts.google.com", "", "ck_auth_identities_subject_length"),
    ],
)
async def test_identity_checks(sessionmaker, issuer, subject, constraint):
    user = await make_user(sessionmaker)
    with pytest.raises(IntegrityError, match=constraint):
        await exec_sql(
            sessionmaker,
            "INSERT INTO auth_identities (user_id, issuer, subject) VALUES (:u, :i, :s)",
            u=user,
            i=issuer,
            s=subject,
        )


async def test_identity_requires_existing_user(sessionmaker):
    with pytest.raises(IntegrityError, match="fk_auth_identities_user_id_users"):
        await exec_sql(
            sessionmaker,
            "INSERT INTO auth_identities (user_id, issuer, subject) "
            "VALUES (gen_random_uuid(), 'https://accounts.google.com', '1')",
        )


async def test_session_secret_hash_is_unique(sessionmaker):
    user = await make_user(sessionmaker)
    await make_session_row(sessionmaker, user)
    with pytest.raises(IntegrityError, match="uq_sessions_secret_hash"):
        await make_session_row(sessionmaker, user)


@pytest.mark.parametrize("bad_hash", ["short", "A" * 64, "g" * 64, "a" * 63])
async def test_session_hash_must_be_lowercase_sha256_hex(sessionmaker, bad_hash):
    user = await make_user(sessionmaker)
    with pytest.raises(IntegrityError, match="ck_sessions_secret_hash_format"):
        await make_session_row(sessionmaker, user, secret_hash=bad_hash)


async def test_session_must_expire_after_creation(sessionmaker):
    user = await make_user(sessionmaker)
    with pytest.raises(IntegrityError, match="ck_sessions_expires_after_created"):
        await exec_sql(
            sessionmaker,
            "INSERT INTO sessions (user_id, secret_hash, expires_at) "
            "VALUES (:u, :h, now() - interval '1 second')",
            u=user,
            h=HEX64,
        )


async def test_user_delete_restricted_by_identity_and_session(sessionmaker):
    user = await make_user(sessionmaker)
    await exec_sql(
        sessionmaker,
        "INSERT INTO auth_identities (user_id, issuer, subject) "
        "VALUES (:u, 'https://accounts.google.com', '1')",
        u=user,
    )
    with pytest.raises(IntegrityError, match="fk_auth_identities_user_id_users"):
        await exec_sql(sessionmaker, "DELETE FROM users WHERE id = :u", u=user)

    await exec_sql(sessionmaker, "DELETE FROM auth_identities WHERE user_id = :u", u=user)
    await make_session_row(sessionmaker, user)
    with pytest.raises(IntegrityError, match="fk_sessions_user_id_users"):
        await exec_sql(sessionmaker, "DELETE FROM users WHERE id = :u", u=user)


async def insert_attempt(
    sessionmaker, *, state=HEX64, binding=HEX64_B, nonce="n" * 32, verifier="v" * 43
):
    await exec_sql(
        sessionmaker,
        "INSERT INTO oidc_login_attempts (state_hash, browser_binding_hash, nonce, pkce_verifier, "
        "expires_at) VALUES (:s, :b, :n, :v, now() + interval '10 minutes')",
        s=state,
        b=binding,
        n=nonce,
        v=verifier,
    )


async def test_attempt_state_hash_unique(sessionmaker):
    await insert_attempt(sessionmaker)
    with pytest.raises(IntegrityError, match="uq_oidc_login_attempts_state_hash"):
        await insert_attempt(sessionmaker)


@pytest.mark.parametrize("length", [43, 128])
async def test_attempt_accepts_valid_verifier_lengths(sessionmaker, length):
    await insert_attempt(sessionmaker, verifier="v" * length)


@pytest.mark.parametrize("length", [42, 129])
async def test_attempt_rejects_bad_verifier_lengths(sessionmaker, length):
    with pytest.raises(Exception, match="pkce_verifier_length|too long"):
        await insert_attempt(sessionmaker, verifier="v" * length)


@pytest.mark.parametrize(
    ("kwargs", "constraint"),
    [
        ({"state": "xyz"}, "ck_oidc_login_attempts_state_hash_format"),
        ({"binding": "xyz"}, "ck_oidc_login_attempts_binding_hash_format"),
        ({"nonce": "short"}, "ck_oidc_login_attempts_nonce_length"),
    ],
)
async def test_attempt_other_checks(sessionmaker, kwargs, constraint):
    with pytest.raises(IntegrityError, match=constraint):
        await insert_attempt(sessionmaker, **kwargs)
