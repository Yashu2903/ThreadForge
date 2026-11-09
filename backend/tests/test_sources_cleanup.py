"""python -m app.commands.cleanup_ai (checkpoint 2 steps): abandoned uploads, leftover files, orphan
files and scratch space. Each step is checked on its own, then together and repeated."""

import asyncio
import os
import subprocess
import sys
import time
import uuid

import pytest

from app.commands.cleanup_ai import ORPHAN_AGE_SECONDS, SCRATCH_AGE_SECONDS, cleanup_ai
from app.storage import Storage
from tests import source_files as sf
from tests.db_utils import BACKEND_DIR
from tests.factories import TEST_DATABASE_URL
from tests.helpers import add_member, execute, rows, scalar
from tests.test_invitations_api import new_room
from tests.test_sources_api import finalize, put, start, stored, upload


@pytest.fixture
def storage_dir(tmp_path):
    return tmp_path / "storage"


@pytest.fixture
def store(storage_dir):
    return Storage(storage_dir)


@pytest.fixture
def cfg(settings, storage_dir):
    return settings.model_copy(update={"storage_path": str(storage_dir)})


@pytest.fixture
async def room(cfg, make_actor, sessionmaker):
    owner = await make_actor("Owner", app_settings=cfg)
    editor = await make_actor("Editor", app_settings=cfg)
    room_id = await new_room(owner)
    await add_member(sessionmaker, room_id, editor.id, "editor")
    return owner, editor, room_id


PDF = sf.text_pdf(1)
LONG_AGO = "created_at = now() - interval '3 hours', expires_at = now() - interval '1 hour'"


def age(path, seconds):
    old = time.time() - seconds
    os.utime(path, (old, old))


async def test_abandoned_uploads_expire_and_their_bytes_go(room, sessionmaker, store):
    _owner, editor, room_id = room
    waiting = (await start(editor, room_id, "a.pdf", PDF)).json()["id"]  # intent only
    sent = (await start(editor, room_id, "b.pdf", PDF)).json()["id"]
    await put(editor, room_id, sent, PDF)  # bytes stored, never finalized
    fresh = (await start(editor, room_id, "c.pdf", PDF)).json()["id"]
    await put(editor, room_id, fresh, PDF)
    assert len(stored(store)) == 2
    await execute(
        sessionmaker,
        f"UPDATE source_uploads SET {LONG_AGO} WHERE id IN (:a, :b)",
        a=waiting,
        b=sent,
    )

    result = await cleanup_ai(sessionmaker, store)

    assert result.uploads_expired == 2 and result.upload_files_deleted == 1
    states = {
        str(r["id"]): (r["state"], r["storage_key"])
        for r in await rows(sessionmaker, "SELECT id, state, storage_key FROM source_uploads")
    }
    assert states[waiting] == ("expired", None) and states[sent] == ("expired", None)
    assert states[fresh][0] == "uploaded" and states[fresh][1] is not None  # still usable
    assert len(stored(store)) == 1
    assert (await finalize(editor, room_id, fresh)).status_code == 201  # the live one still works
    assert (await put(editor, room_id, sent, PDF)).status_code == 409  # the expired one does not


async def test_an_upload_that_was_finalized_is_never_expired_or_deleted(room, sessionmaker, store):
    _owner, editor, room_id = room
    source = await upload(editor, room_id, "a.pdf", PDF)
    await execute(sessionmaker, f"UPDATE source_uploads SET {LONG_AGO}")
    result = await cleanup_ai(sessionmaker, store)
    assert (result.uploads_expired, result.upload_files_deleted, result.orphan_files) == (0, 0, 0)
    assert await scalar(sessionmaker, "SELECT state FROM source_uploads") == "finalized"
    assert len(stored(store)) == 1  # the version's file is untouched
    vid = source["current_version"]["id"]
    assert (
        await editor.get(f"/rooms/{room_id}/sources/{source['id']}/versions/{vid}/download")
    ).status_code == 200


async def test_leftover_files_of_failed_and_cancelled_uploads_are_retried(
    room, sessionmaker, store
):
    _owner, editor, room_id = room
    key = store.new_key()
    path = store.path_for("sources", key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PDF)
    uid = (await start(editor, room_id, "a.pdf", PDF)).json()["id"]
    await execute(
        sessionmaker,
        "UPDATE source_uploads SET state = 'failed', storage_key = :k WHERE id = :u",
        k=key,
        u=uid,
    )
    result = await cleanup_ai(sessionmaker, store)
    assert result.upload_files_deleted == 1 and stored(store) == []
    assert await scalar(sessionmaker, "SELECT storage_key FROM source_uploads") is None


async def test_orphan_files_are_deleted_only_when_old_and_unreferenced(room, sessionmaker, store):
    _owner, editor, room_id = room
    kept = await upload(editor, room_id, "kept.pdf", PDF)  # referenced by a version
    in_flight = (await start(editor, room_id, "f.pdf", PDF)).json()["id"]
    await put(editor, room_id, in_flight, PDF)  # referenced by an upload row
    old_orphan, young_orphan = store.new_key(), store.new_key()
    for key in (old_orphan, young_orphan):
        p = store.path_for("sources", key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"orphan")
    for _key, path, _m in store.iter_files("sources"):
        age(path, ORPHAN_AGE_SECONDS + 3600)  # everything is old...
    age(store.path_for("sources", young_orphan), 60)  # ...except this one
    assert len(stored(store)) == 4

    result = await cleanup_ai(sessionmaker, store)

    assert result.orphan_files == 1
    remaining = set(stored(store))
    assert old_orphan not in remaining and young_orphan in remaining
    assert len(remaining) == 3  # the two referenced files and the young orphan
    vid = kept["current_version"]["id"]
    assert (
        await editor.get(f"/rooms/{room_id}/sources/{kept['id']}/versions/{vid}/download")
    ).status_code == 200
    assert (await finalize(editor, room_id, in_flight)).status_code == 201


async def test_the_orphan_sweep_leaves_other_namespaces_alone(room, sessionmaker, store):
    key = store.new_key()
    path = store.path_for("assets", key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"an asset the other cleanup owns")
    age(path, ORPHAN_AGE_SECONDS + 3600)
    await cleanup_ai(sessionmaker, store)
    assert path.exists()


async def test_stale_scratch_space_is_removed_and_fresh_scratch_is_not(room, sessionmaker, store):
    tmp = store.tmp_dir
    old_dir, new_dir = tmp / "inspect-old", tmp / "inspect-new"
    for d in (old_dir, new_dir):
        d.mkdir()
        (d / "x").write_bytes(b"x")
    old_up, new_up, other = tmp / "up-old", tmp / "up-new", tmp / "unrelated.txt"
    for f in (old_up, new_up, other):
        f.write_bytes(b"x")
    for p in (old_dir, old_up, other):
        age(p, SCRATCH_AGE_SECONDS + 600)
    result = await cleanup_ai(sessionmaker, store)
    assert result.scratch_removed == 2
    assert not old_dir.exists() and not old_up.exists()
    assert new_dir.exists() and new_up.exists() and other.exists()  # not ours, or too young


async def test_running_twice_changes_nothing_the_second_time(room, sessionmaker, store):
    _owner, editor, room_id = room
    uid = (await start(editor, room_id, "a.pdf", PDF)).json()["id"]
    await put(editor, room_id, uid, PDF)
    await execute(sessionmaker, f"UPDATE source_uploads SET {LONG_AGO}")
    first = await cleanup_ai(sessionmaker, store)
    second = await cleanup_ai(sessionmaker, store)
    assert first.uploads_expired == 1 and first.upload_files_deleted == 1
    assert (second.uploads_expired, second.upload_files_deleted, second.orphan_files) == (0, 0, 0)


async def test_cleanup_and_finalize_racing_never_lose_a_finished_source(room, sessionmaker, store):
    """Whichever commits first wins; the loser sees the new state. A source is never half-made."""
    _owner, editor, room_id = room
    for _ in range(3):
        uid = (await start(editor, room_id, f"{uuid.uuid4().hex[:6]}.pdf", PDF)).json()["id"]
        await put(editor, room_id, uid, PDF)
        await execute(
            sessionmaker,
            f"UPDATE source_uploads SET expires_at = now() + interval '1 second', created_at = now() - interval '1 hour' WHERE id = '{uid}'",
        )
        await asyncio.sleep(1.1)
        done, _ = await asyncio.gather(
            finalize(editor, room_id, uid), cleanup_ai(sessionmaker, store)
        )
        state = await scalar(sessionmaker, "SELECT state FROM source_uploads WHERE id = :u", u=uid)
        if done.status_code == 201:
            assert state == "finalized"
            assert (
                await scalar(
                    sessionmaker,
                    "SELECT count(*) FROM source_versions WHERE storage_key IS NOT NULL",
                )
                >= 1
            )
        else:
            assert done.status_code == 409 and state == "expired"
        await cleanup_ai(sessionmaker, store)
    for key in await rows(
        sessionmaker, "SELECT storage_key FROM source_versions WHERE storage_key IS NOT NULL"
    ):
        assert store.path_for("sources", key["storage_key"]).is_file()  # no live row lost its file


async def test_the_command_line_entry_point_runs_end_to_end(room, sessionmaker, storage_dir):
    _owner, editor, room_id = room
    uid = (await start(editor, room_id, "a.pdf", PDF)).json()["id"]
    await put(editor, room_id, uid, PDF)
    await execute(sessionmaker, f"UPDATE source_uploads SET {LONG_AGO}")
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL, "STORAGE_PATH": str(storage_dir)}
    done = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "app.commands.cleanup_ai"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "expired 1 abandoned uploads" in done.stdout and "deleted 1 upload files" in done.stdout
    assert await scalar(sessionmaker, "SELECT state FROM source_uploads") == "expired"


def test_the_command_line_rejects_a_bad_batch_size():
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL}
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "app.commands.cleanup_ai", "--batch-size", "0"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 2 and "batch size" in done.stderr
