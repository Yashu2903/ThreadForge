"""Migrations on an empty database, round trip, and model/migration drift.

These are plain (sync) tests: Alembic's async env runs its own event loop via asyncio.run.
Each test leaves the database at head so later tests are unaffected.
"""

import pytest
from alembic import command

from tests.db_utils import (
    APP_TABLES,
    PART1_TABLES,
    PART2_TABLES,
    PART3_TABLES,
    alembic_config,
    build_fresh_schema,
    reset_schema,
    table_names,
    upgrade_to_head,
)


@pytest.fixture
def empty_database():
    reset_schema()
    yield
    build_fresh_schema()


def test_upgrade_on_empty_database_creates_exactly_the_part1_and_part2_tables(empty_database):
    assert table_names() == set()
    upgrade_to_head()
    assert table_names() == {*APP_TABLES, "alembic_version"}


def test_no_tables_for_later_parts_exist(empty_database):
    upgrade_to_head()
    forbidden = {"documents", "uploads", "presence", "message_versions", "consumer_receipts"}
    assert not forbidden & table_names()


def test_upgrade_downgrade_upgrade_round_trip(empty_database):
    config = alembic_config()
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    assert table_names() == {"alembic_version"}
    command.upgrade(config, "head")
    assert table_names() == {*APP_TABLES, "alembic_version"}


def test_models_and_migrations_have_not_drifted(empty_database):
    upgrade_to_head()
    # Raises CommandError if autogenerate would produce any operation
    # (types, nullability, indexes, uniques, FKs, server defaults).
    command.check(alembic_config())


def test_part2_downgrade_leaves_part1_intact_and_reapplies(empty_database):
    config = alembic_config()
    command.upgrade(config, "head")
    command.downgrade(config, "0001")
    assert table_names() == {*PART1_TABLES, "alembic_version"}
    command.upgrade(config, "head")
    assert table_names() == {*APP_TABLES, "alembic_version"}


def test_part3_downgrade_leaves_part2_intact_and_reapplies(empty_database):
    config = alembic_config()
    command.upgrade(config, "head")
    command.downgrade(config, "0002")
    assert table_names() == {*PART1_TABLES, *PART2_TABLES, "alembic_version"}
    command.upgrade(config, "head")
    assert table_names() == {*APP_TABLES, "alembic_version"}


def test_part4_downgrade_leaves_part3_intact_and_reapplies(empty_database):
    config = alembic_config()
    command.upgrade(config, "head")
    command.downgrade(config, "0003")
    assert table_names() == {*PART1_TABLES, *PART2_TABLES, *PART3_TABLES, "alembic_version"}
    command.upgrade(config, "head")
    assert table_names() == {*APP_TABLES, "alembic_version"}
