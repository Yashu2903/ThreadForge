"""Sources over HTTP (AI phase 1, checkpoint 2): the permission table on every route, the three-step
device upload, idempotency, hostile files, quotas, replace, removal, download and races.

Real PostgreSQL, real storage directory, the real sandboxed inspector. No AI provider exists in any
of these tests because uploading never calls one (a separate test proves the modules do not even
import the provider code).
"""

import asyncio
import hashlib
import uuid
from pathlib import Path

import pytest

from app.storage import Storage
from tests import source_files as sf
from tests.helpers import TRUSTED_ORIGIN, add_member, execute, rows, scalar
from tests.test_invitations_api import new_room


@pytest.fixture
def storage_dir(tmp_path):
    return tmp_path / "storage"


@pytest.fixture
def cfg(settings, storage_dir):
    return settings.model_copy(update={"storage_path": str(storage_dir)})


@pytest.fixture
def store(storage_dir):
    return Storage(storage_dir)


class World:
    """One room with an owner, an editor, a second editor, a viewer, an outsider and a removed
    member; every actor talks to an app that shares one settings object."""

    def __init__(self, cfg, sessionmaker, make_actor):
        self.cfg, self.sm, self.make_actor = cfg, sessionmaker, make_actor

    async def build(self):
        make = lambda n: self.make_actor(n, app_settings=self.cfg)  # noqa: E731
        self.owner, self.editor, self.editor2 = (
            await make("Owner"),
            await make("Editor"),
            await make("Editor2"),
        )
        self.viewer, self.outsider, self.removed = (
            await make("Viewer"),
            await make("Outsider"),
            await make("Removed"),
        )
        self.room = await new_room(self.owner)
        for actor, role, state in (
            (self.editor, "editor", "active"),
            (self.editor2, "editor", "active"),
            (self.viewer, "viewer", "active"),
            (self.removed, "editor", "removed"),
        ):
            await add_member(self.sm, self.room, actor.id, role, state)
        return self


@pytest.fixture
async def world(cfg, sessionmaker, make_actor):
    return await World(cfg, sessionmaker, make_actor).build()


# ---- request helpers -----------------------------------------------------------------------------------


def intent_body(name, data, *, declared=None, source_id=None, request_id=None, size=None):
    body = {
        "client_request_id": request_id or str(uuid.uuid4()),
        "filename": name,
        "byte_size": len(data) if size is None else size,
    }
    if declared:
        body["media_type"] = declared
    if source_id:
        body["source_id"] = source_id
    return body


async def start(actor, room, name, data, **kw):
    return await actor.post(f"/rooms/{room}/source-uploads", json=intent_body(name, data, **kw))


async def put(actor, room, upload_id, data, **kw):
    return await actor.put(
        f"/rooms/{room}/source-uploads/{upload_id}/content",
        content=data,
        headers={"Content-Type": "application/octet-stream"},
        **kw,
    )


async def finalize(actor, room, upload_id, **kw):
    return await actor.post(f"/rooms/{room}/source-uploads/{upload_id}/finalize", **kw)


async def upload(actor, room, name, data, **kw):
    """The whole flow; asserts every step succeeded and returns the finalized source."""
    r = await start(actor, room, name, data, **kw)
    assert r.status_code in (200, 201), r.text
    upload_id = r.json()["id"]
    r = await put(actor, room, upload_id, data)
    assert r.status_code == 200, r.text
    r = await finalize(actor, room, upload_id)
    assert r.status_code == 201, r.text
    return r.json()


def module_text(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


def stored(store):
    return sorted(k for k, _p, _m in store.iter_files("sources"))


PDF = sf.text_pdf(2)


# =====================================================================================================
# the happy path, per kind
# =====================================================================================================


@pytest.mark.parametrize(
    ("name", "data", "kind", "checks"),
    [
        (
            "report.pdf",
            sf.text_pdf(3),
            "pdf",
            {"page_count": 3, "text_page_count": 3, "likely_scanned_pages": 0},
        ),
        (
            "scan.pdf",
            sf.scanned_pdf(2),
            "pdf",
            {"page_count": 2, "text_page_count": 0, "likely_scanned_pages": 2, "image_count": 2},
        ),
        (
            "spec.docx",
            sf.docx(["a", "bc"], images=2),
            "docx",
            {"page_count": None, "image_count": 2, "text_chars": 3},
        ),
        ("notes.txt", b"hello world\n", "txt", {"text_chars": 12, "page_count": None}),
        ("readme.md", b"# Title\n\nbody\n", "md", {"text_chars": 14, "image_count": 0}),
    ],
)
async def test_each_accepted_kind_uploads_and_is_inspected_but_not_analyzed(
    world, store, name, data, kind, checks
):
    source = await upload(world.editor, world.room, name, data)
    version = source["current_version"]
    assert version["kind"] == kind and version["version_no"] == 1
    assert version["analysis_state"] == "not_analyzed"  # upload never analyzes
    assert version["byte_size"] == len(data)
    assert version["sha256"] == hashlib.sha256(data).hexdigest()
    for key, expected in checks.items():
        assert version[key] == expected, key
    assert source["display_name"] == name and source["created_by"] == str(world.editor.id)
    assert "storage_key" not in version and "storage_key" not in source
    [key] = stored(store)
    assert store.path_for("sources", key).read_bytes() == data  # the original bytes, untouched
    assert source["versions"][0]["id"] == version["id"]


async def test_the_list_shows_state_usage_limits_and_what_the_caller_may_do(world):
    await upload(world.editor, world.room, "a.pdf", PDF)
    await upload(world.owner, world.room, "b.txt", b"abc")
    r = await world.viewer.get(f"/rooms/{world.room}/sources")
    assert r.status_code == 200
    body = r.json()
    assert [s["display_name"] for s in body["sources"]] == ["a.pdf", "b.txt"]
    assert body["used_sources"] == 2 and body["used_bytes"] == len(PDF) + 3
    assert body["limits"]["max_upload_bytes"] == 25 * 1024 * 1024
    assert body["limits"]["accepted"] == ["pdf", "docx", "txt", "md"]
    assert (body["can_upload"], body["can_download"]) == (False, True)
    owner = (await world.owner.get(f"/rooms/{world.room}/sources")).json()
    assert owner["can_upload"] is True
    assert all(s["versions"] is None for s in body["sources"])  # the list is light


async def test_no_provider_code_is_involved_in_uploading(world):
    import app.api.sources as api
    import app.services.sources as service
    import app.sources.files as files
    import app.sources.sandbox as sandbox

    for module in (api, service, files, sandbox):
        text = module_text(module)
        assert "app.ai" not in text and "httpx" not in text, module.__name__
    before = await scalar(world.sm, "SELECT count(*) FROM ai_usage_ledger")
    await upload(world.editor, world.room, "a.pdf", PDF)
    assert await scalar(world.sm, "SELECT count(*) FROM ai_usage_ledger") == before == 0


async def test_uploads_still_work_when_the_room_or_the_operator_switched_ai_off(
    world, sessionmaker
):
    r = await world.owner.request(
        "PUT", f"/rooms/{world.room}/ai-settings", json={"ai_enabled": False}
    )
    assert r.status_code == 200, r.text
    await upload(world.editor, world.room, "a.pdf", PDF)
    await execute(sessionmaker, "UPDATE ai_global_flags SET ai_enabled = false")
    source = await upload(world.editor, world.room, "b.txt", b"abc")
    assert source["current_version"]["analysis_state"] == "not_analyzed"  # stored, not analyzed


async def test_logs_never_contain_file_names_or_content(world, caplog):
    caplog.set_level("DEBUG")
    secret_name = "quarterly-layoff-plan-ZEBRA42.txt"
    await upload(world.editor, world.room, secret_name, b"ZEBRA-PAYLOAD-secret-content")
    bad = await start(world.editor, world.room, "x-ZEBRA77.exe", b"abc")
    assert bad.status_code == 415
    assert "ZEBRA" not in caplog.text and "layoff" not in caplog.text


# =====================================================================================================
# permissions: every route, every role
# =====================================================================================================


@pytest.mark.parametrize(
    ("who", "status"),
    [("owner", 200), ("editor", 200), ("viewer", 200), ("outsider", 404), ("removed", 404)],
)
async def test_listing_and_inspecting_are_for_every_active_member(world, who, status):
    source = await upload(world.owner, world.room, "a.pdf", PDF)
    actor = getattr(world, who)
    assert (await actor.get(f"/rooms/{world.room}/sources")).status_code == status
    assert (await actor.get(f"/rooms/{world.room}/sources/{source['id']}")).status_code == status


@pytest.mark.parametrize(
    ("who", "status"),
    [("owner", 201), ("editor", 201), ("viewer", 403), ("outsider", 404), ("removed", 404)],
)
async def test_only_owners_and_editors_can_start_an_upload(world, who, status):
    r = await start(getattr(world, who), world.room, "a.pdf", PDF)
    assert r.status_code == status, r.text
    if status != 201:
        assert await scalar(world.sm, "SELECT count(*) FROM source_uploads") == 0


@pytest.mark.parametrize(
    ("who", "status"),
    [("viewer", 403), ("outsider", 404), ("removed", 404), ("editor2", 404)],
)
async def test_nobody_else_can_use_my_upload(world, who, status):
    mine = (await start(world.editor, world.room, "a.pdf", PDF)).json()["id"]
    other = getattr(world, who)
    assert (await put(other, world.room, mine, PDF)).status_code == status
    assert (await other.get(f"/rooms/{world.room}/source-uploads/{mine}")).status_code in (403, 404)
    assert (await finalize(other, world.room, mine)).status_code == status
    assert (await other.delete(f"/rooms/{world.room}/source-uploads/{mine}")).status_code == status
    # an owner is not special either: uploads are private to their creator
    assert (await put(world.owner, world.room, mine, PDF)).status_code == 404
    assert (await put(world.editor, world.room, mine, PDF)).status_code == 200


async def test_the_owner_can_replace_but_an_upload_id_from_another_room_is_unknown(world):
    other_room = await new_room(world.owner, "Other")
    foreign = (await start(world.owner, other_room, "a.pdf", PDF)).json()["id"]
    assert (await put(world.owner, world.room, foreign, PDF)).status_code == 404
    assert (await finalize(world.owner, world.room, foreign)).status_code == 404


@pytest.mark.parametrize(
    ("who", "mine", "status"),
    [
        ("owner", False, 204),  # the owner removes anyone's source
        ("editor", True, 204),  # an editor removes their own upload
        ("editor", False, 403),  # but not someone else's
        ("viewer", False, 403),
        ("outsider", False, 404),
        ("removed", False, 404),
    ],
)
async def test_removal_rights(world, store, who, mine, status):
    uploader = getattr(world, who) if mine else world.editor2
    source = await upload(uploader, world.room, "a.pdf", PDF)
    r = await getattr(world, who).delete(f"/rooms/{world.room}/sources/{source['id']}")
    assert r.status_code == status, r.text
    assert len(stored(store)) == (0 if status == 204 else 1)


async def test_an_owner_cannot_be_blocked_from_downloading_by_settings_and_roles_are_checked_on_every_call(
    cfg, sessionmaker, make_actor, store
):
    only = cfg.model_copy(update={"source_download_roles": ["owner", "editor"]})
    w = await World(only, sessionmaker, make_actor).build()
    source = await upload(w.editor, w.room, "a.pdf", PDF)
    vid = source["current_version"]["id"]
    url = f"/rooms/{w.room}/sources/{source['id']}/versions/{vid}/download"
    assert (await w.owner.get(url)).status_code == 200
    assert (await w.editor.get(url)).status_code == 200
    assert (await w.viewer.get(url)).status_code == 403
    assert (await w.outsider.get(url)).status_code == 404
    assert (await w.removed.get(url)).status_code == 404
    listed = (await w.viewer.get(f"/rooms/{w.room}/sources")).json()
    assert listed["can_download"] is False


@pytest.mark.parametrize("who", ["owner", "editor", "viewer"])
async def test_the_default_download_policy_lets_every_member_download(world, who):
    source = await upload(world.owner, world.room, "a.pdf", PDF)
    vid = source["current_version"]["id"]
    r = await getattr(world, who).get(
        f"/rooms/{world.room}/sources/{source['id']}/versions/{vid}/download"
    )
    assert r.status_code == 200 and r.content == PDF


async def test_a_member_who_loses_access_loses_the_files_at_once(world):
    source = await upload(world.owner, world.room, "a.pdf", PDF)
    vid = source["current_version"]["id"]
    url = f"/rooms/{world.room}/sources/{source['id']}/versions/{vid}/download"
    assert (await world.viewer.get(url)).status_code == 200
    await add_member(world.sm, world.room, world.viewer.id, "viewer", "removed")
    assert (await world.viewer.get(url)).status_code == 404
    assert (await world.viewer.get(f"/rooms/{world.room}/sources")).status_code == 404


async def test_authentication_and_csrf_come_before_everything(world, client):
    anonymous = await client.get(f"/rooms/{world.room}/sources")
    assert anonymous.status_code == 401
    body = intent_body("a.pdf", PDF)
    no_csrf = await world.editor.request(
        "POST", f"/rooms/{world.room}/source-uploads", json=body, csrf=False
    )
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "csrf_failed"
    no_origin = await world.editor.request(
        "POST", f"/rooms/{world.room}/source-uploads", json=body, origin=False
    )
    assert no_origin.status_code == 403
    sid = uuid.uuid4()
    for method, url in (
        ("PUT", f"/rooms/{world.room}/source-uploads/{sid}/content"),
        ("POST", f"/rooms/{world.room}/source-uploads/{sid}/finalize"),
        ("DELETE", f"/rooms/{world.room}/source-uploads/{sid}"),
        ("DELETE", f"/rooms/{world.room}/sources/{sid}"),
    ):
        r = await world.editor.request(
            method, url, csrf=False, content=b"x" if method == "PUT" else None
        )
        assert r.status_code == 403, (method, url)
        assert (await client.request(method, url)).status_code == 401, (method, url)
    assert await scalar(world.sm, "SELECT count(*) FROM source_uploads") == 0


async def test_a_non_member_sending_garbage_learns_nothing_and_a_viewer_gets_403_not_422(world):
    url = f"/rooms/{world.room}/source-uploads"
    assert (await world.outsider.post(url, json={"nonsense": 1})).status_code == 404
    assert (await world.viewer.post(url, json={"nonsense": 1})).status_code == 403
    assert (await world.editor.post(url, json={"nonsense": 1})).status_code == 422


async def test_downgrade_between_the_steps_is_caught_under_the_room_lock(world, store):
    r = await start(world.editor, world.room, "a.pdf", PDF)
    uid = r.json()["id"]
    assert (await put(world.editor, world.room, uid, PDF)).status_code == 200
    await add_member(world.sm, world.room, world.editor.id, "viewer")  # demoted before finalize
    done = await finalize(world.editor, world.room, uid)
    assert done.status_code == 403
    assert await scalar(world.sm, "SELECT count(*) FROM sources") == 0
    await add_member(world.sm, world.room, world.editor.id, "editor", "removed")  # removed entirely
    assert (await finalize(world.editor, world.room, uid)).status_code == 404
    assert await scalar(world.sm, "SELECT count(*) FROM sources") == 0


# =====================================================================================================
# the intent
# =====================================================================================================


@pytest.mark.parametrize(
    ("name", "declared", "status", "code"),
    [
        ("virus.exe", None, 415, "unsupported_media_type"),
        ("noextension", None, 415, "unsupported_media_type"),
        ("a.doc", "application/msword", 415, "unsupported_media_type"),
        ("a.html", "text/html", 415, "unsupported_media_type"),
        ("a.pdf", "text/plain", 415, "type_mismatch"),
        ("a.txt", "application/octet-stream", 415, "type_mismatch"),
    ],
)
async def test_only_pdf_docx_txt_and_markdown_are_accepted(world, name, declared, status, code):
    r = await start(world.editor, world.room, name, b"abc", declared=declared)
    assert r.status_code == status and r.json()["error"]["code"] == code
    assert await scalar(world.sm, "SELECT count(*) FROM source_uploads") == 0


async def test_the_intent_validates_its_fields(world):
    url = f"/rooms/{world.room}/source-uploads"
    good = intent_body("a.pdf", PDF)
    assert (await world.editor.post(url, json={**good, "byte_size": 0})).status_code == 422
    assert (await world.editor.post(url, json={**good, "byte_size": -5})).status_code == 422
    assert (await world.editor.post(url, json={**good, "byte_size": "12"})).status_code == 422
    assert (await world.editor.post(url, json={**good, "filename": ""})).status_code == 422
    assert (
        await world.editor.post(url, json={**good, "filename": "a" * 300 + ".pdf"})
    ).status_code == 422
    assert (
        await world.editor.post(url, json={**good, "client_request_id": "nope"})
    ).status_code == 422
    assert (
        await world.editor.post(url, json={**good, "created_by": str(world.owner.id)})
    ).status_code == 422
    assert (
        await world.editor.post(url, json={**good, "room_id": str(uuid.uuid4())})
    ).status_code == 422
    assert (
        await world.editor.post(
            url, content=b"{not json", headers={"Content-Type": "application/json"}
        )
    ).status_code == 422
    big = await world.editor.post(url, json={**good, "byte_size": 26 * 1024 * 1024})
    assert big.status_code == 413
    assert await scalar(world.sm, "SELECT count(*) FROM source_uploads") == 0


async def test_the_stored_name_is_the_sanitized_one(world):
    r = await start(world.editor, world.room, "..\\..\\evil‮gnp.PDF", PDF)
    assert r.status_code == 201
    assert r.json()["original_name"] == "evilgnp.pdf"


async def test_replacing_needs_an_active_source_of_this_room(world):
    other_room = await new_room(world.owner, "Other")
    foreign = await upload(world.owner, other_room, "a.pdf", PDF)
    r = await start(world.editor, world.room, "b.pdf", PDF, source_id=foreign["id"])
    assert r.status_code == 404 and r.json()["error"]["code"] == "source_not_found"
    mine = await upload(world.owner, world.room, "m.pdf", PDF)
    await world.owner.delete(f"/rooms/{world.room}/sources/{mine['id']}")
    assert (
        await start(world.editor, world.room, "b.pdf", PDF, source_id=mine["id"])
    ).status_code == 404
    assert (
        await start(world.editor, world.room, "b.pdf", PDF, source_id=str(uuid.uuid4()))
    ).status_code == 404


# =====================================================================================================
# idempotency
# =====================================================================================================


async def test_the_same_request_id_returns_the_same_upload_and_consumes_nothing(world):
    body = intent_body("a.pdf", PDF)
    url = f"/rooms/{world.room}/source-uploads"
    first = await world.editor.post(url, json=body)
    again = await world.editor.post(url, json=body)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["id"] == again.json()["id"]
    assert await scalar(world.sm, "SELECT count(*) FROM source_uploads") == 1


async def test_the_same_request_id_with_different_fields_is_a_conflict(world):
    body = intent_body("a.pdf", PDF)
    url = f"/rooms/{world.room}/source-uploads"
    assert (await world.editor.post(url, json=body)).status_code == 201
    for change in (
        {"filename": "b.pdf"},
        {"byte_size": len(PDF) + 1},
        {"media_type": "application/pdf"},
    ):
        r = await world.editor.post(url, json={**body, **change})
        assert r.status_code == 409 and r.json()["error"]["code"] == "source_upload_id_reused", (
            change
        )
    assert await scalar(world.sm, "SELECT count(*) FROM source_uploads") == 1


async def test_request_ids_are_per_user(world):
    body = intent_body("a.pdf", PDF)
    url = f"/rooms/{world.room}/source-uploads"
    a = await world.editor.post(url, json=body)
    b = await world.editor2.post(url, json=body)
    assert (a.status_code, b.status_code) == (201, 201) and a.json()["id"] != b.json()["id"]


async def test_the_fingerprint_is_keyed_not_a_plain_hash(world):
    await start(
        world.editor, world.room, "a.pdf", PDF, request_id="11111111-1111-4111-8111-111111111111"
    )
    [row] = await rows(world.sm, "SELECT request_fingerprint FROM source_uploads")
    plain = hashlib.sha256(b"11111111-1111-4111-8111-111111111111a.pdf").digest()
    assert (
        bytes(row["request_fingerprint"]) != plain and len(bytes(row["request_fingerprint"])) == 32
    )


async def test_repeating_the_bytes_and_the_finalize_is_safe(world, store):
    uid = (await start(world.editor, world.room, "a.pdf", PDF)).json()["id"]
    first = await put(world.editor, world.room, uid, PDF)
    again = await put(world.editor, world.room, uid, PDF)  # a retry after a lost response
    assert first.status_code == again.status_code == 200
    assert first.json()["sha256"] == again.json()["sha256"] == hashlib.sha256(PDF).hexdigest()
    a = await finalize(world.editor, world.room, uid)
    b = await finalize(world.editor, world.room, uid)
    assert (a.status_code, b.status_code) == (201, 201)
    assert (
        a.json()["id"] == b.json()["id"]
        and a.json()["current_version"]["id"] == b.json()["current_version"]["id"]
    )
    assert await scalar(world.sm, "SELECT count(*) FROM sources") == 1
    assert await scalar(world.sm, "SELECT count(*) FROM source_versions") == 1
    assert len(stored(store)) == 1
    after = await put(world.editor, world.room, uid, b"different bytes entirely")
    assert (
        after.status_code == 200 and after.json()["state"] == "finalized"
    )  # ignored, nothing changed
    assert (
        await scalar(world.sm, "SELECT sha256 FROM source_versions") == hashlib.sha256(PDF).digest()
    )


async def test_two_finalize_calls_at_the_same_time_make_one_source(world, store):
    uid = (await start(world.editor, world.room, "a.pdf", PDF)).json()["id"]
    await put(world.editor, world.room, uid, PDF)
    results = await asyncio.gather(*[finalize(world.editor, world.room, uid) for _ in range(4)])
    assert {r.status_code for r in results} == {201}
    assert len({r.json()["id"] for r in results}) == 1
    assert await scalar(world.sm, "SELECT count(*) FROM sources") == 1
    assert len(stored(store)) == 1


# =====================================================================================================
# the bytes
# =====================================================================================================


@pytest.mark.parametrize("delta", [-1, 1, 1000])
async def test_the_byte_count_must_equal_the_declared_size(world, store, delta):
    declared = len(PDF) + delta
    uid = (await start(world.editor, world.room, "a.pdf", PDF, size=declared)).json()["id"]
    r = await put(world.editor, world.room, uid, PDF)
    assert r.status_code == 422 and r.json()["error"]["reason"] == "size_mismatch"
    assert stored(store) == []
    row = (await rows(world.sm, "SELECT state, error_code, storage_key FROM source_uploads"))[0]
    assert (row["state"], row["error_code"], row["storage_key"]) == (
        "failed",
        "size_mismatch",
        None,
    )
    assert (
        await put(world.editor, world.room, uid, PDF)
    ).status_code == 409  # a failed upload is over


async def test_an_empty_body_is_a_size_mismatch(world, store):
    uid = (await start(world.editor, world.room, "a.pdf", PDF)).json()["id"]
    assert (await put(world.editor, world.room, uid, b"")).status_code == 422
    assert stored(store) == []


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("a.pdf", sf.docx()),  # a DOCX called .pdf
        ("a.docx", PDF),  # a PDF called .docx
        ("a.txt", PDF),  # a PDF called .txt
        ("a.md", sf.docx()),
        ("a.pdf", b"just text, not a pdf"),
        ("a.docx", b"just text, not a zip"),
        ("a.txt", b"MZ\x90\x00 pretend executable"),
    ],
)
async def test_the_real_signature_must_match_the_extension(world, store, name, data):
    uid = (await start(world.editor, world.room, name, data)).json()["id"]
    r = await put(world.editor, world.room, uid, data)
    assert r.status_code == 415 and r.json()["error"]["code"] == "type_mismatch"
    assert stored(store) == []
    assert await scalar(world.sm, "SELECT state FROM source_uploads") == "failed"


async def test_a_body_over_the_hard_limit_is_cut_off_while_streaming(
    cfg, sessionmaker, make_actor, store
):
    small = cfg.model_copy(
        update={"source_max_upload_bytes": 2000, "source_room_quota_bytes": 100_000}
    )
    w = await World(small, sessionmaker, make_actor).build()
    data = b"a" * 5000
    uid = (await start(w.editor, w.room, "a.txt", b"a" * 1000)).json()["id"]  # declares 1000
    r = await put(w.editor, w.room, uid, data)
    assert r.status_code == 413
    assert stored(store) == []
    assert (
        await start(w.editor, w.room, "b.txt", data)
    ).status_code == 413  # declared over the limit


async def test_bytes_cannot_be_sent_before_the_intent_or_after_it_ended(world, store):
    assert (await put(world.editor, world.room, str(uuid.uuid4()), PDF)).status_code == 404
    assert (await put(world.editor, world.room, "not-a-uuid", PDF)).status_code == 404
    uid = (await start(world.editor, world.room, "a.pdf", PDF)).json()["id"]
    cancelled = await world.editor.delete(f"/rooms/{world.room}/source-uploads/{uid}")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    r = await put(world.editor, world.room, uid, PDF)
    assert r.status_code == 409 and r.json()["error"]["code"] == "source_upload_not_open"
    assert stored(store) == []


async def test_an_expired_upload_cannot_take_bytes_or_be_finalized(world, store):
    uid = (await start(world.editor, world.room, "a.pdf", PDF)).json()["id"]
    expire = (
        "UPDATE source_uploads SET created_at = now() - interval '3 hours', "
        "expires_at = now() - interval '1 hour'"
    )
    await execute(world.sm, expire)
    r = await put(world.editor, world.room, uid, PDF)
    assert r.status_code == 409 and r.json()["error"]["state"] == "expired"
    assert (await world.editor.get(f"/rooms/{world.room}/source-uploads/{uid}")).json()[
        "state"
    ] == "expired"

    uid2 = (await start(world.editor, world.room, "b.pdf", PDF)).json()["id"]
    await put(world.editor, world.room, uid2, PDF)
    await execute(world.sm, f"{expire} WHERE id = '{uid2}'")
    done = await finalize(world.editor, world.room, uid2)
    assert done.status_code == 409 and done.json()["error"]["state"] == "expired"
    assert await scalar(world.sm, "SELECT count(*) FROM sources") == 0


async def test_finalizing_before_the_bytes_arrived_is_refused(world):
    uid = (await start(world.editor, world.room, "a.pdf", PDF)).json()["id"]
    r = await finalize(world.editor, world.room, uid)
    assert r.status_code == 409 and r.json()["error"]["state"] == "intent"


# =====================================================================================================
# hostile files: refused at finalize, bytes deleted, nothing created
# =====================================================================================================


@pytest.mark.parametrize(
    ("name", "data", "reason"),
    [
        ("locked.pdf", sf.encrypted_pdf("secret"), "pdf_encrypted"),
        ("owner-locked.pdf", sf.encrypted_pdf(""), "pdf_encrypted"),
        ("broken.pdf", b"%PDF-1.4\nnot really a pdf at all", "pdf_invalid"),
        ("macro.docx", sf.docx(macros=True), "docx_macros"),
        ("macro2.docx", sf.docx(main_type=sf.DOCM_MAIN), "docx_macros"),
        ("empty-zip.docx", b"PK\x03\x04" + b"\x00" * 30, "docx_invalid"),
        ("noparts.docx", sf.docx(skip=("word/document.xml",)), "docx_invalid"),
        ("dtd.docx", sf.docx(doctype='<!DOCTYPE d [<!ENTITY x "y">]>'), "docx_invalid"),
        ("slip.docx", sf.docx(extra={"../../evil.xml": b"x"}), "docx_invalid"),
        ("nul.txt", b"abc\x00def", "text_invalid"),
        ("latin1.txt", "café".encode("latin-1"), "text_invalid"),
        ("blank.md", b"  \n ", "text_invalid"),
    ],
)
async def test_hostile_files_are_refused_and_leave_nothing_behind(world, store, name, data, reason):
    uid = (await start(world.editor, world.room, name, data)).json()["id"]
    assert (await put(world.editor, world.room, uid, data)).status_code == 200
    r = await finalize(world.editor, world.room, uid)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_source" and r.json()["error"]["reason"] == reason
    assert stored(store) == []
    row = (await rows(world.sm, "SELECT state, error_code, storage_key FROM source_uploads"))[0]
    assert (row["state"], row["error_code"], row["storage_key"]) == ("failed", reason, None)
    assert await scalar(world.sm, "SELECT count(*) FROM sources") == 0
    assert await scalar(world.sm, "SELECT count(*) FROM source_versions") == 0
    assert [p for p in (store.root / "tmp").glob("inspect-*")] == []
    again = await finalize(world.editor, world.room, uid)  # the verdict is final
    assert again.status_code == 409


async def test_a_zip_bomb_and_a_page_bomb_are_refused(cfg, sessionmaker, make_actor, store):
    tight = cfg.model_copy(
        update={"source_docx_max_uncompressed_bytes": 4 * 1024 * 1024, "source_max_pages": 3}
    )
    w = await World(tight, sessionmaker, make_actor).build()
    for name, data, reason in (
        ("bomb.docx", sf.zip_bomb_docx(20), "docx_invalid"),
        ("long.pdf", sf.text_pdf(4), "too_many_pages"),
    ):
        uid = (await start(w.editor, w.room, name, data)).json()["id"]
        await put(w.editor, w.room, uid, data)
        r = await finalize(w.editor, w.room, uid)
        assert r.status_code == 422 and r.json()["error"]["reason"] == reason
    assert stored(store) == []
    ok = await upload(w.editor, w.room, "fine.pdf", sf.text_pdf(3))
    assert ok["current_version"]["page_count"] == 3


async def test_a_type_problem_found_by_the_inspector_is_a_415(world):
    data = (
        b"%PDF-1.4 looks like text but is a pdf"  # passes the .txt signature? no: starts with %PDF
    )
    uid = (await start(world.editor, world.room, "a.txt", data)).json()["id"]
    assert (await put(world.editor, world.room, uid, data)).status_code == 415


# =====================================================================================================
# replace and versions
# =====================================================================================================


async def test_replacing_adds_an_immutable_version_and_moves_the_pointer(world, store):
    v1 = await upload(world.owner, world.room, "plan.pdf", sf.text_pdf(1))
    v2 = await upload(world.editor, world.room, "plan-v2.pdf", sf.text_pdf(2), source_id=v1["id"])
    assert v2["id"] == v1["id"] and v2["display_name"] == "plan.pdf"  # same source, same name
    assert v2["created_by"] == str(world.owner.id)  # the creator is unchanged
    assert v2["version_count"] == 2 and v2["current_version"]["version_no"] == 2
    assert v2["current_version"]["page_count"] == 2
    assert [v["version_no"] for v in v2["versions"]] == [2, 1]
    assert v2["versions"][1]["original_name"] == "plan.pdf" and v2["versions"][1]["page_count"] == 1
    assert len(stored(store)) == 2  # the old file is kept: versions are immutable
    detail = (await world.viewer.get(f"/rooms/{world.room}/sources/{v1['id']}")).json()
    assert detail["current_version"]["id"] == v2["current_version"]["id"]
    listed = (await world.viewer.get(f"/rooms/{world.room}/sources")).json()
    assert len(listed["sources"]) == 1 and listed["sources"][0]["version_count"] == 2
    old = v2["versions"][1]["id"]
    r = await world.viewer.get(f"/rooms/{world.room}/sources/{v1['id']}/versions/{old}/download")
    assert r.status_code == 200 and r.content == sf.text_pdf(1)  # the OLD version is still there


async def test_a_replacement_may_be_a_different_kind(world):
    first = await upload(world.owner, world.room, "notes.txt", b"abc")
    second = await upload(world.owner, world.room, "notes.pdf", PDF, source_id=first["id"])
    assert [v["kind"] for v in second["versions"]] == ["pdf", "txt"]


async def test_concurrent_replacements_get_distinct_version_numbers(world):
    base = await upload(world.owner, world.room, "a.pdf", PDF)
    pending = []
    for i in range(3):
        data = sf.text_pdf(1 + i)
        uid = (
            await start(world.editor, world.room, f"r{i}.pdf", data, source_id=base["id"])
        ).json()["id"]
        await put(world.editor, world.room, uid, data)
        pending.append(uid)
    results = await asyncio.gather(*[finalize(world.editor, world.room, u) for u in pending])
    assert {r.status_code for r in results} == {201}
    numbers = await rows(world.sm, "SELECT version_no FROM source_versions ORDER BY version_no")
    assert [r["version_no"] for r in numbers] == [1, 2, 3, 4]  # unique, no gaps
    current = await scalar(
        world.sm,
        "SELECT v.version_no FROM sources s JOIN source_versions v ON v.id = s.current_version_id",
    )
    assert current == 4  # the last one to commit is current


async def test_a_viewer_cannot_replace_and_a_removed_source_cannot_be_replaced(world):
    base = await upload(world.owner, world.room, "a.pdf", PDF)
    assert (
        await start(world.viewer, world.room, "b.pdf", PDF, source_id=base["id"])
    ).status_code == 403
    uid = (await start(world.editor, world.room, "b.pdf", PDF, source_id=base["id"])).json()["id"]
    await put(world.editor, world.room, uid, PDF)
    assert (
        await world.owner.delete(f"/rooms/{world.room}/sources/{base['id']}")
    ).status_code == 204
    gone = await finalize(world.editor, world.room, uid)  # removed between intent and finalize
    assert gone.status_code == 404
    assert (
        await scalar(
            world.sm, "SELECT count(*) FROM source_versions WHERE analysis_state <> 'removed'"
        )
        == 0
    )


# =====================================================================================================
# quotas and rates
# =====================================================================================================


async def test_the_source_count_quota_counts_uploads_in_flight(cfg, sessionmaker, make_actor):
    w = await World(
        cfg.model_copy(update={"source_max_per_room": 2}), sessionmaker, make_actor
    ).build()
    await upload(w.owner, w.room, "a.pdf", PDF)
    one = await start(w.editor, w.room, "b.pdf", PDF)  # takes the last slot (in flight)
    assert one.status_code == 201
    over = await start(w.editor2, w.room, "c.pdf", PDF)
    assert over.status_code == 409 and over.json()["error"]["code"] == "quota_exceeded"
    await w.editor.delete(f"/rooms/{w.room}/source-uploads/{one.json()['id']}")  # frees it again
    assert (await start(w.editor2, w.room, "c.pdf", PDF)).status_code == 201


async def test_parallel_intents_cannot_both_take_the_last_slot(cfg, sessionmaker, make_actor):
    w = await World(
        cfg.model_copy(update={"source_max_per_room": 1}), sessionmaker, make_actor
    ).build()
    results = await asyncio.gather(
        start(w.editor, w.room, "a.pdf", PDF),
        start(w.editor2, w.room, "b.pdf", PDF),
        start(w.owner, w.room, "c.pdf", PDF),
    )
    assert sorted(r.status_code for r in results) == [201, 409, 409]


async def test_the_byte_quota_counts_live_versions_and_uploads_in_flight(
    cfg, sessionmaker, make_actor, store
):
    quota = len(PDF) * 3 + 10
    w = await World(
        cfg.model_copy(update={"source_room_quota_bytes": quota, "source_max_upload_bytes": quota}),
        sessionmaker,
        make_actor,
    ).build()
    first = await upload(w.owner, w.room, "a.pdf", PDF)
    await upload(w.owner, w.room, "a2.pdf", PDF, source_id=first["id"])  # versions count too
    flying = await start(w.editor, w.room, "b.pdf", PDF)  # reserved while in flight
    assert flying.status_code == 201
    over = await start(w.editor2, w.room, "c.pdf", PDF)
    assert over.status_code == 409 and over.json()["error"]["code"] == "quota_exceeded"
    assert (await w.owner.delete(f"/rooms/{w.room}/sources/{first['id']}")).status_code == 204
    assert (await start(w.editor2, w.room, "c.pdf", PDF)).status_code == 201  # removal freed it


async def test_quota_is_rechecked_with_the_real_size_at_finalize(cfg, sessionmaker, make_actor):
    quota = len(PDF) * 2
    w = await World(
        cfg.model_copy(update={"source_room_quota_bytes": quota, "source_max_upload_bytes": quota}),
        sessionmaker,
        make_actor,
    ).build()
    uid = (await start(w.editor, w.room, "a.pdf", PDF)).json()["id"]
    await put(w.editor, w.room, uid, PDF)
    other = await upload(w.owner, w.room, "b.txt", b"x" * 40)
    # something else grows to fill the room while the first file waits to be finalized
    await execute(
        w.sm,
        "UPDATE source_versions SET byte_size = :n WHERE id = :v",
        n=quota,
        v=other["current_version"]["id"],
    )
    r = await finalize(w.editor, w.room, uid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "quota_exceeded"
    assert await scalar(w.sm, "SELECT state FROM source_uploads WHERE id = :u", u=uid) == "uploaded"


async def test_the_per_minute_upload_rate_is_per_user_and_retries_do_not_consume_it(
    cfg, sessionmaker, make_actor
):
    w = await World(
        cfg.model_copy(
            update={"source_uploads_per_minute": 2, "source_max_open_uploads_per_user": 10}
        ),
        sessionmaker,
        make_actor,
    ).build()
    same = intent_body("a.pdf", PDF)
    url = f"/rooms/{w.room}/source-uploads"
    for _ in range(3):
        assert (await w.editor.post(url, json=same)).status_code in (
            200,
            201,
        )  # one intent, three sends
    assert (await start(w.editor, w.room, "b.pdf", PDF)).status_code == 201
    limited = await start(w.editor, w.room, "c.pdf", PDF)
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    assert limited.json()["error"]["code"] == "rate_limited"
    assert (
        await start(w.editor2, w.room, "d.pdf", PDF)
    ).status_code == 201  # another user is unaffected
    assert (
        await start(w.editor, await new_room(w.editor, "mine"), "e.pdf", PDF)
    ).status_code == 201  # per room


async def test_a_user_can_have_only_a_few_unfinished_uploads(cfg, sessionmaker, make_actor):
    w = await World(
        cfg.model_copy(update={"source_max_open_uploads_per_user": 2}), sessionmaker, make_actor
    ).build()
    ids = [(await start(w.editor, w.room, f"{i}.pdf", PDF)).json()["id"] for i in range(2)]
    third = await start(w.editor, w.room, "3.pdf", PDF)
    assert third.status_code == 409 and third.json()["error"]["code"] == "too_many_open_uploads"
    await w.editor.delete(f"/rooms/{w.room}/source-uploads/{ids[0]}")
    assert (await start(w.editor, w.room, "3.pdf", PDF)).status_code == 201
    await upload(
        w.editor2, w.room, "x.pdf", PDF
    )  # finished uploads do not count; others are unaffected


# =====================================================================================================
# removal
# =====================================================================================================


async def test_removal_deletes_every_file_at_once_and_hides_the_source(world, store):
    v1 = await upload(world.editor, world.room, "a.pdf", sf.text_pdf(1))
    await upload(world.editor, world.room, "a2.pdf", sf.text_pdf(2), source_id=v1["id"])
    other = await upload(world.editor, world.room, "keep.txt", b"keep me")
    assert len(stored(store)) == 3
    r = await world.editor.delete(f"/rooms/{world.room}/sources/{v1['id']}")
    assert r.status_code == 204 and r.content == b""
    assert len(stored(store)) == 1  # only the other source's file is left
    assert (await world.viewer.get(f"/rooms/{world.room}/sources/{v1['id']}")).status_code == 404
    listed = (await world.viewer.get(f"/rooms/{world.room}/sources")).json()
    assert [s["id"] for s in listed["sources"]] == [other["id"]]
    assert listed["used_bytes"] == len(b"keep me")
    state = await rows(
        world.sm,
        "SELECT analysis_state, storage_key, removed_at FROM source_versions WHERE source_id = :s",
        s=v1["id"],
    )
    assert {(s["analysis_state"], s["storage_key"]) for s in state} == {("removed", None)}
    assert all(s["removed_at"] is not None for s in state)
    src = (await rows(world.sm, "SELECT state, removed_by FROM sources WHERE id = :s", s=v1["id"]))[
        0
    ]
    assert src["state"] == "removed" and str(src["removed_by"]) == str(world.editor.id)
    for vid in [
        v["id"]
        for v in (
            await rows(world.sm, "SELECT id FROM source_versions WHERE source_id = :s", s=v1["id"])
        )
    ]:
        d = await world.owner.get(f"/rooms/{world.room}/sources/{v1['id']}/versions/{vid}/download")
        assert d.status_code == 404
    assert (
        await world.owner.delete(f"/rooms/{world.room}/sources/{v1['id']}")
    ).status_code == 404  # already gone


async def test_a_failed_file_delete_is_remembered_and_finished_by_cleanup(
    world, store, monkeypatch
):
    from app.commands.cleanup_ai import cleanup_ai

    source = await upload(world.editor, world.room, "a.pdf", PDF)
    real = Storage.delete

    def broken(self, namespace, key):
        raise OSError("disk says no")

    monkeypatch.setattr(Storage, "delete", broken)
    assert (
        await world.editor.delete(f"/rooms/{world.room}/sources/{source['id']}")
    ).status_code == 204
    keyed = await scalar(
        world.sm,
        "SELECT count(*) FROM source_versions WHERE analysis_state = 'removed' AND storage_key IS NOT NULL",
    )
    assert keyed == 1 and len(stored(store)) == 1  # the rows still say which file is left
    monkeypatch.setattr(Storage, "delete", real)
    result = await cleanup_ai(world.sm, store)
    assert result.removed_version_files_deleted == 1
    assert stored(store) == []
    assert (
        await scalar(world.sm, "SELECT count(*) FROM source_versions WHERE storage_key IS NOT NULL")
        == 0
    )


async def test_a_name_can_be_reused_after_removal_and_the_quota_is_freed(world):
    a = await upload(world.editor, world.room, "same.pdf", PDF)
    await world.editor.delete(f"/rooms/{world.room}/sources/{a['id']}")
    b = await upload(world.editor, world.room, "same.pdf", PDF)
    assert b["id"] != a["id"]
    listed = (await world.owner.get(f"/rooms/{world.room}/sources")).json()
    assert listed["used_sources"] == 1 and listed["used_bytes"] == len(PDF)


async def test_removing_one_room_s_source_by_id_from_another_room_is_not_found(world):
    other = await new_room(world.owner, "Other")
    mine = await upload(world.owner, world.room, "a.pdf", PDF)
    assert (await world.owner.delete(f"/rooms/{other}/sources/{mine['id']}")).status_code == 404
    assert (await world.owner.get(f"/rooms/{other}/sources/{mine['id']}")).status_code == 404
    assert (await world.owner.delete(f"/rooms/{world.room}/sources/not-a-uuid")).status_code == 404
    assert (await world.owner.get(f"/rooms/{world.room}/sources/{mine['id']}")).status_code == 200


# =====================================================================================================
# download
# =====================================================================================================


async def test_download_is_an_attachment_with_safe_headers(world):
    data = sf.docx(["x"])
    source = await upload(world.editor, world.room, "résumé final.docx", data)
    vid = source["current_version"]["id"]
    r = await world.viewer.get(
        f"/rooms/{world.room}/sources/{source['id']}/versions/{vid}/download"
    )
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-type"] == sf.DOCX_TYPE
    assert r.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''r%C3%A9sum%C3%A9%20final.docx" in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert r.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(
    ("name", "data", "media"),
    [
        ("a.pdf", PDF, "application/pdf"),
        ("a.txt", b"abc", "text/plain; charset=utf-8"),
        ("a.md", b"# t", "text/markdown; charset=utf-8"),
    ],
)
async def test_the_download_type_is_the_validated_one(world, name, data, media):
    source = await upload(world.owner, world.room, name, data)
    vid = source["current_version"]["id"]
    r = await world.owner.get(f"/rooms/{world.room}/sources/{source['id']}/versions/{vid}/download")
    assert r.headers["content-type"] == media


async def test_a_version_id_only_works_with_its_own_source_and_room(world):
    a = await upload(world.owner, world.room, "a.pdf", PDF)
    b = await upload(world.owner, world.room, "b.txt", b"abc")
    a_ver, b_ver = a["current_version"]["id"], b["current_version"]["id"]
    assert (
        await world.owner.get(f"/rooms/{world.room}/sources/{a['id']}/versions/{b_ver}/download")
    ).status_code == 404
    other = await new_room(world.owner, "Other")
    assert (
        await world.owner.get(f"/rooms/{other}/sources/{a['id']}/versions/{a_ver}/download")
    ).status_code == 404
    assert (
        await world.owner.get(f"/rooms/{world.room}/sources/{a['id']}/versions/zzz/download")
    ).status_code == 404


async def test_a_missing_file_is_reported_as_not_found_not_a_crash(world, store):
    source = await upload(world.owner, world.room, "a.pdf", PDF)
    for _key, path, _m in list(store.iter_files("sources")):
        path.unlink()
    vid = source["current_version"]["id"]
    r = await world.owner.get(f"/rooms/{world.room}/sources/{source['id']}/versions/{vid}/download")
    assert r.status_code == 404


# =====================================================================================================
# attaching later: the admission helper checkpoint 4 will call
# =====================================================================================================


async def test_resolve_versions_accepts_only_live_versions_of_this_room(world):
    from app.errors import AppError
    from app.services.sources import resolve_versions

    a = await upload(world.owner, world.room, "a.pdf", PDF)
    b = await upload(world.owner, world.room, "b.txt", b"abc")
    other = await new_room(world.owner, "Other")
    foreign = await upload(world.owner, other, "c.txt", b"abc")
    a_ver, b_ver = uuid.UUID(a["current_version"]["id"]), uuid.UUID(b["current_version"]["id"])
    room = uuid.UUID(world.room)
    async with world.sm() as db:
        got = await resolve_versions(db, room, [b_ver, a_ver, b_ver])
        assert [v.id for v in got] == [b_ver, a_ver]  # input order, duplicates collapsed
        with pytest.raises(AppError) as caught:
            await resolve_versions(db, room, [a_ver, uuid.UUID(foreign["current_version"]["id"])])
        assert caught.value.code == "source_unavailable"
        assert caught.value.extra["source_version_ids"] == [foreign["current_version"]["id"]]
        with pytest.raises(AppError):
            await resolve_versions(db, room, [uuid.uuid4()])
    await world.owner.delete(f"/rooms/{world.room}/sources/{a['id']}")
    async with world.sm() as db:
        with pytest.raises(AppError) as caught:
            await resolve_versions(db, room, [a_ver, b_ver])
        assert caught.value.extra["source_version_ids"] == [str(a_ver)]


# =====================================================================================================
# database constraints behind the service
# =====================================================================================================


async def test_the_database_refuses_inconsistent_source_rows(world):
    source = await upload(world.owner, world.room, "a.pdf", PDF)
    sid, vid = source["id"], source["current_version"]["id"]

    async def refused(sql, **params):
        with pytest.raises(Exception, match="violates|check"):
            await execute(world.sm, sql, **params)

    await refused(
        "UPDATE source_versions SET storage_key = NULL WHERE id = :v", v=vid
    )  # not removed
    await refused(
        "UPDATE source_versions SET analysis_state = 'removed' WHERE id = :v", v=vid
    )  # no removed_at
    await refused("UPDATE source_versions SET analysis_state = 'weird' WHERE id = :v", v=vid)
    await refused("UPDATE source_versions SET sha256 = '\\x00'::bytea WHERE id = :v", v=vid)
    await refused("UPDATE source_versions SET byte_size = 0 WHERE id = :v", v=vid)
    await refused("UPDATE source_versions SET media_type = 'image/png' WHERE id = :v", v=vid)
    await refused("UPDATE source_versions SET image_count = -1 WHERE id = :v", v=vid)
    await refused("UPDATE sources SET state = 'removed' WHERE id = :s", s=sid)  # no removed_at
    await refused("UPDATE sources SET state = 'gone' WHERE id = :s", s=sid)
    await refused("UPDATE sources SET current_version_id = gen_random_uuid() WHERE id = :s", s=sid)
    await refused(
        "INSERT INTO source_versions (source_id, room_id, version_no, original_name, kind, media_type, byte_size, sha256, storage_key, created_by) "
        "SELECT id, room_id, 1, 'dup.pdf', 'pdf', 'application/pdf', 5, decode(repeat('00', 32), 'hex'), 'k', created_by FROM sources WHERE id = :s",
        s=sid,
    )  # (source_id, version_no) is unique
    other = await new_room(world.owner, "Other")
    await refused(
        "UPDATE source_versions SET room_id = :r WHERE id = :v", r=other, v=vid
    )  # not the source's room


async def test_a_pointer_to_another_sources_version_is_refused(world):
    a = await upload(world.owner, world.room, "a.pdf", PDF)
    other = await new_room(world.owner, "Other")
    b = await upload(world.owner, other, "b.pdf", PDF)
    with pytest.raises(Exception, match="violates"):
        await execute(
            world.sm,
            "UPDATE sources SET current_version_id = :v WHERE id = :s",
            v=b["current_version"]["id"],
            s=a["id"],
        )


async def test_uploads_origin_header_and_csrf_are_enforced_on_the_streaming_route_too(world):
    uid = (await start(world.editor, world.room, "a.pdf", PDF)).json()["id"]
    no_origin = await world.editor.request(
        "PUT", f"/rooms/{world.room}/source-uploads/{uid}/content", content=PDF, origin=False
    )
    assert no_origin.status_code == 403
    wrong_origin = await world.editor.request(
        "PUT",
        f"/rooms/{world.room}/source-uploads/{uid}/content",
        content=PDF,
        origin=False,
        headers={"Origin": "https://evil.example"},
    )
    assert wrong_origin.status_code == 403
    assert TRUSTED_ORIGIN  # (the helper's trusted origin is the one that works)
    assert (await put(world.editor, world.room, uid, PDF)).status_code == 200
