"""Migration 0007 (AI foundations): expand-only, and a working downgrade.

Plain (sync) tests like test_migrations.py: Alembic's async env runs its own event loop.
"""

import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.db_utils import AI_TABLES, alembic_config, build_fresh_schema, table_names
from tests.factories import TEST_DATABASE_URL


async def sql(statement: str, **params):
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(statement), params)
            return result.all() if result.returns_rows else None
    finally:
        await engine.dispose()


@pytest.fixture
def at_head():
    build_fresh_schema()
    yield
    build_fresh_schema()


def test_upgrade_adds_ai_tables_and_the_single_flag_row(at_head):
    assert set(AI_TABLES) <= table_names()
    [(count, enabled)] = asyncio.run(
        sql("SELECT count(*), bool_and(ai_enabled) FROM ai_global_flags")
    )
    assert (count, enabled) == (1, True)
    # existing rooms get AI on by default (the column default), and nothing was dropped
    cols = {
        r[0]
        for r in asyncio.run(
            sql("SELECT column_name FROM information_schema.columns WHERE table_name = 'rooms'")
        )
    }
    assert "ai_enabled" in cols
    # the single-row table refuses a second row
    with pytest.raises(Exception, match="single_row"):
        asyncio.run(sql("INSERT INTO ai_global_flags (id) VALUES (2)"))


def test_downgrade_removes_ai_objects_and_events_and_upgrade_works_again(at_head):
    asyncio.run(sql("INSERT INTO users (id, display_name) VALUES (gen_random_uuid(), 'u')"))
    asyncio.run(
        sql("INSERT INTO rooms (name, owner_id, event_seq) SELECT 'r', id, 1 FROM users LIMIT 1")
    )
    asyncio.run(
        sql(
            "INSERT INTO room_events (room_id, seq, type, resource_type, resource_id, actor_id, payload, created_at) "
            "SELECT r.id, 1, 'room_ai_settings_changed', 'room', r.id, r.owner_id, '{\"ai_enabled\": false}', now() "
            "FROM rooms r"
        )
    )
    asyncio.run(
        sql(
            "INSERT INTO outbox (event_id, room_id, room_seq, kind, payload, state, next_attempt_at) "
            "SELECT e.event_id, e.room_id, e.seq, 'room_event', "
            "jsonb_build_object('schema_version', 1, 'event_id', e.event_id, 'room_id', e.room_id, 'room_seq', e.seq), "
            "'pending', now() FROM room_events e"
        )
    )
    config = alembic_config()
    command.downgrade(config, "0006")
    names = table_names()
    assert not (set(AI_TABLES) & names)
    assert asyncio.run(sql("SELECT count(*) FROM room_events")) == [
        (0,)
    ]  # the new-type event is gone
    assert asyncio.run(sql("SELECT count(*) FROM rooms")) == [(1,)]  # the room itself is untouched
    cols = {
        r[0]
        for r in asyncio.run(
            sql("SELECT column_name FROM information_schema.columns WHERE table_name = 'rooms'")
        )
    }
    assert "ai_enabled" not in cols
    command.upgrade(config, "head")
    assert set(AI_TABLES) <= table_names()
