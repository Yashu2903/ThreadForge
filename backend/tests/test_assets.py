"""Image assets: processing, upload and download routes, quotas, rate limit, lifecycle cleanup."""

import asyncio
import hashlib
import io
import os
import time
import uuid

import pytest
from PIL import Image, PngImagePlugin

from app.assets_pipeline import ImageRejected, configure_pillow, process_image, sniff
from app.commands.cleanup_documents import cleanup_documents
from app.storage import Storage, StorageError
from tests.doc_helpers import content, new_id, room_with_doc, save
from tests.helpers import TRUSTED_ORIGIN, add_member, execute, rows, scalar
from tests.test_documents_save import with_image
from tests.test_invitations_api import new_room

# ---- image builders ---------------------------------------------------------------------------------------


def png(w=8, h=6, color=(200, 30, 30), **save_kwargs) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG", **save_kwargs)
    return buf.getvalue()


def jpeg(w=8, h=6, **save_kwargs) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buf, "JPEG", **save_kwargs)
    return buf.getvalue()


def webp(w=8, h=6, **save_kwargs) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (90, 90, 90)).save(buf, "WEBP", **save_kwargs)
    return buf.getvalue()


def animated_webp() -> bytes:
    frames = [Image.new("RGB", (8, 8), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    buf = io.BytesIO()
    frames[0].save(buf, "WEBP", save_all=True, append_images=frames[1:], duration=50, loop=0)
    return buf.getvalue()


def animated_png() -> bytes:
    frames = [Image.new("RGB", (8, 8), c) for c in ((255, 0, 0), (0, 255, 0))]
    buf = io.BytesIO()
    frames[0].save(buf, "PNG", save_all=True, append_images=frames[1:], duration=50)
    return buf.getvalue()


def jpeg_with_exif(w=20, h=10, orientation=6) -> bytes:
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = "SecretCameraMaker"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (37.0, 25.0, 19.0), "W", (122.0, 5.0, 6.0)
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (255, 0, 0)).save(buf, "JPEG", exif=exif, comment=b"secret-comment")
    return buf.getvalue()


async def upload(actor, room_id, data, ctype="image/png", **kw):
    return await actor.request(
        "POST", f"/rooms/{room_id}/assets", content=data, headers={"Content-Type": ctype}, **kw
    )


@pytest.fixture
def storage_dir(tmp_path):
    return tmp_path / "storage"


@pytest.fixture
def cfg(settings, storage_dir):
    return settings.model_copy(update={"storage_path": str(storage_dir)})


@pytest.fixture
def store(storage_dir):
    return Storage(storage_dir)


async def owner_room(make_actor, cfg, sessionmaker):
    owner = await make_actor("Owner", app_settings=cfg)
    room_id = await new_room(owner)
    editor, viewer = (
        await make_actor("Editor", app_settings=cfg),
        await make_actor("Viewer", app_settings=cfg),
    )
    await add_member(sessionmaker, room_id, editor.id, "editor")
    await add_member(sessionmaker, room_id, viewer.id, "viewer")
    return owner, editor, viewer, room_id


def stored_files(store, ns="assets"):
    return sorted(k for k, _p, _m in store.iter_files(ns))


# ---- the pipeline itself -------------------------------------------------------------------------------------


def run(tmp_path, data, declared=None, max_pixels=25_000_000):
    configure_pillow(max_pixels)  # as the application does at startup
    src = tmp_path / "in.bin"
    src.write_bytes(data)
    return process_image(src, declared, max_pixels)


@pytest.mark.parametrize(
    ("data", "media"), [(png(), "image/png"), (jpeg(), "image/jpeg"), (webp(), "image/webp")]
)
def test_each_accepted_format_is_re_encoded_in_the_same_format(tmp_path, data, media):
    out = run(tmp_path, data, media)
    assert (out.media_type, out.width, out.height) == (media, 8, 6)
    stored = out.path.read_bytes()
    assert out.byte_size == len(stored) and out.sha256 == hashlib.sha256(stored).digest()
    assert sniff(stored[:16]) == media
    with Image.open(io.BytesIO(stored)) as again:
        assert again.size == (8, 6)


@pytest.mark.parametrize(
    "data",
    [
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        b"GIF89a\x01\x00\x01\x00\x00\x00\x00;",
        b"BM" + b"\x00" * 60,
        b"<html><script>alert(1)</script></html>",
        b"%PDF-1.7 not an image",
        b"",
        b"\x00" * 100,
    ],
)
def test_other_formats_are_refused_by_their_real_signature(tmp_path, data):
    with pytest.raises(ImageRejected) as caught:
        run(tmp_path, data, "image/png")
    assert caught.value.code == "unsupported_media_type"


def test_the_declared_type_must_match_the_real_one(tmp_path):
    with pytest.raises(ImageRejected) as caught:
        run(tmp_path, jpeg(), "image/png")  # a JPEG labelled PNG
    assert caught.value.code == "type_mismatch"


def test_a_valid_header_with_garbage_body_is_not_an_image(tmp_path):
    with pytest.raises(ImageRejected) as caught:
        run(tmp_path, png()[:40], "image/png")  # truncated
    assert caught.value.code == "invalid_image"
    with pytest.raises(ImageRejected):
        run(tmp_path, png()[:30] + os.urandom(200), "image/png")


def test_a_polyglot_loses_everything_after_the_image_and_all_metadata(tmp_path):
    marker = b"EMBEDDED-PAYLOAD-<script>alert(1)</script>"
    zip_ish = b"PK\x03\x04" + marker
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "secret-png-text")
    for data, media in (
        (png(pnginfo=info) + zip_ish, "image/png"),
        (jpeg_with_exif() + zip_ish, "image/jpeg"),
        (webp() + zip_ish, "image/webp"),
    ):
        out = run(tmp_path, data, media)
        stored = out.path.read_bytes()
        assert marker not in stored and b"PK\x03\x04" not in stored
        assert b"secret-png-text" not in stored and b"secret-comment" not in stored
        assert b"SecretCameraMaker" not in stored and b"Exif" not in stored


def test_exif_gps_and_orientation_are_handled(tmp_path):
    out = run(tmp_path, jpeg_with_exif(20, 10, orientation=6), "image/jpeg")
    assert (out.width, out.height) == (
        10,
        20,
    )  # rotated by the orientation tag, then the tag is gone
    with Image.open(io.BytesIO(out.path.read_bytes())) as clean:
        assert clean.size == (10, 20)
        assert len(clean.getexif()) == 0
        assert "exif" not in clean.info and "comment" not in clean.info
    plain = run(tmp_path, jpeg_with_exif(20, 10, orientation=1), "image/jpeg")
    assert (plain.width, plain.height) == (20, 10)


def test_pixel_limits_are_checked_from_the_header_before_decoding(tmp_path):
    big = png(200, 200)
    with pytest.raises(ImageRejected) as caught:
        run(tmp_path, big, "image/png", max_pixels=10_000)
    assert caught.value.code == "too_many_pixels"
    run(tmp_path, big, "image/png", max_pixels=40_000)  # exactly at the limit is fine


def test_a_tiny_file_that_claims_a_huge_canvas_is_refused_quickly(tmp_path):
    bomb = png(
        12_000, 12_000, optimize=False, compress_level=9
    )  # a few hundred KiB, 144 megapixels
    assert len(bomb) < 2_000_000
    started = time.perf_counter()
    with pytest.raises(ImageRejected) as caught:
        run(tmp_path, bomb, "image/png")
    assert caught.value.code == "too_many_pixels"
    assert time.perf_counter() - started < 2  # nothing was decoded


def test_a_wide_but_low_pixel_image_is_still_bounded_by_dimension(tmp_path):
    wide = png(10_001, 2)
    with pytest.raises(ImageRejected) as caught:
        run(tmp_path, wide, "image/png")
    assert caught.value.code == "dimensions_too_large"


@pytest.mark.parametrize("data", [animated_webp(), animated_png()])
def test_animated_images_are_refused(tmp_path, data):
    declared = "image/webp" if data[8:12] == b"WEBP" else "image/png"
    with pytest.raises(ImageRejected) as caught:
        run(tmp_path, data, declared)
    assert caught.value.code == "animated"


def test_transparency_survives_in_png_and_webp(tmp_path):
    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), (255, 0, 0, 0)).save(buf, "PNG")
    with Image.open(
        io.BytesIO(run(tmp_path, buf.getvalue(), "image/png").path.read_bytes())
    ) as out:
        assert out.mode == "RGBA" and out.getpixel((0, 0))[3] == 0


# ---- the storage adapter -------------------------------------------------------------------------------------


def test_storage_keys_are_opaque_and_validated(store):
    key = store.new_key()
    assert len(key) == 32 and store.path_for("assets", key).parts[-1] == key
    for bad in ("../../etc/passwd", "a" * 31, "A" * 32, key + "x", "", "..\\..\\x", "g" * 32):
        with pytest.raises(StorageError):
            store.path_for("assets", bad)
    with pytest.raises(StorageError):
        store.path_for("elsewhere", key)


def test_put_is_atomic_move_and_delete_is_idempotent(store):
    fd, tmp = store.temp_file()
    os.write(fd, b"data")
    os.close(fd)
    key = store.new_key()
    store.put("assets", key, tmp)
    assert not tmp.exists() and store.exists("assets", key)
    assert store.path_for("assets", key).read_bytes() == b"data"
    assert store.delete("assets", key) is True and store.delete("assets", key) is False


# ---- upload: permissions ---------------------------------------------------------------------------------------


async def test_owner_and_editor_can_upload_and_get_a_pending_asset(
    make_actor, cfg, sessionmaker, store
):
    owner, editor, _viewer, room_id = await owner_room(make_actor, cfg, sessionmaker)
    events_before = await scalar(sessionmaker, "SELECT count(*) FROM room_events")
    for actor, data, media in (
        (owner, png(), "image/png"),
        (editor, jpeg(), "image/jpeg"),
        (editor, webp(), "image/webp"),
    ):
        response = await upload(actor, room_id, data, media)
        assert response.status_code == 201, response.text
        body = response.json()
        assert set(body) == {
            "id",
            "room_id",
            "media_type",
            "byte_size",
            "width_px",
            "height_px",
            "sha256",
            "state",
            "created_at",
        }
        assert (body["state"], body["media_type"], body["width_px"], body["height_px"]) == (
            "pending",
            media,
            8,
            6,
        )
        assert "storage_key" not in response.text
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM room_assets WHERE state = 'pending'") == 3
    )
    assert len(stored_files(store)) == 3
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM room_events") == events_before
    )  # uploads emit nothing


async def test_stored_hash_and_size_describe_the_re_encoded_bytes(
    make_actor, cfg, sessionmaker, store
):
    owner, _e, _v, room_id = await owner_room(make_actor, cfg, sessionmaker)
    body = (await upload(owner, room_id, jpeg_with_exif(), "image/jpeg")).json()
    [row] = await rows(
        sessionmaker, "SELECT storage_key, sha256, byte_size, width_px, height_px FROM room_assets"
    )
    data = store.path_for("assets", row["storage_key"]).read_bytes()
    assert bytes(row["sha256"]).hex() == body["sha256"] == hashlib.sha256(data).hexdigest()
    assert row["byte_size"] == len(data) == body["byte_size"]
    assert (row["width_px"], row["height_px"]) == (10, 20)
    assert row["storage_key"] not in str(body)


async def test_a_viewer_cannot_upload_and_outsiders_get_404(make_actor, cfg, sessionmaker, store):
    _o, _e, viewer, room_id = await owner_room(make_actor, cfg, sessionmaker)
    stranger = await make_actor("Stranger", app_settings=cfg)
    response = await upload(viewer, room_id, png())
    assert (response.status_code, response.json()["error"]["code"]) == (403, "insufficient_role")
    response = await upload(stranger, room_id, png())
    assert (response.status_code, response.json()["error"]["code"]) == (404, "room_not_found")
    assert (
        stored_files(store) == []
        and await scalar(sessionmaker, "SELECT count(*) FROM room_assets") == 0
    )


async def test_upload_needs_authentication_and_csrf(make_actor, cfg, sessionmaker, client):
    owner, _e, _v, room_id = await owner_room(make_actor, cfg, sessionmaker)
    anonymous = await client.post(
        f"/rooms/{room_id}/assets", content=png(), headers={"Content-Type": "image/png"}
    )
    assert anonymous.status_code == 401
    response = await upload(owner, room_id, png(), csrf=False)
    assert (response.status_code, response.json()["error"]["code"]) == (403, "csrf_failed")


async def test_a_role_downgrade_that_commits_first_beats_a_slow_upload(
    make_actor, cfg, sessionmaker, store
):
    """The pre-check says editor; under the room lock the service sees viewer and refuses, and the
    already-stored bytes are removed again."""
    from sqlalchemy import text

    owner, editor, _v, room_id = await owner_room(make_actor, cfg, sessionmaker)
    async with sessionmaker() as db, db.begin():
        await db.execute(
            text("SELECT 1 FROM rooms WHERE id = :r FOR NO KEY UPDATE"), {"r": room_id}
        )
        await db.execute(
            text("UPDATE memberships SET role = 'viewer' WHERE room_id = :r AND user_id = :u"),
            {"r": room_id, "u": editor.id},
        )
        task = asyncio.create_task(upload(editor, room_id, png()))
        await asyncio.sleep(0.6)
        assert not task.done()
    response = await task
    assert (response.status_code, response.json()["error"]["code"]) == (403, "insufficient_role")
    assert (
        stored_files(store) == []
        and await scalar(sessionmaker, "SELECT count(*) FROM room_assets") == 0
    )
    assert owner


# ---- upload: validation ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ctype",
    ["text/plain", "image/gif", "image/svg+xml", "application/octet-stream", "image/png2", ""],
)
async def test_only_the_three_image_content_types_are_accepted(
    make_actor, cfg, sessionmaker, store, ctype
):
    owner, _e, _v, room_id = await owner_room(make_actor, cfg, sessionmaker)
    response = await upload(owner, room_id, png(), ctype)
    assert (response.status_code, response.json()["error"]["code"]) == (
        415,
        "unsupported_media_type",
    )
    assert stored_files(store) == []


async def test_content_type_parameters_are_tolerated(make_actor, cfg, sessionmaker):
    owner, _e, _v, room_id = await owner_room(make_actor, cfg, sessionmaker)
    assert (await upload(owner, room_id, png(), "IMAGE/PNG; charset=binary")).status_code == 201


async def test_signature_spoofing_is_rejected_whatever_the_header_says(
    make_actor, cfg, sessionmaker, store
):
    owner, _e, _v, room_id = await owner_room(make_actor, cfg, sessionmaker)
    attacks = [
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "image/png"),
        (b"GIF89a" + b"\x00" * 20, "image/png"),
        (jpeg(), "image/png"),
        (png(), "image/webp"),
        (b"<html><script>1</script></html>", "image/jpeg"),
    ]
    for data, ctype in attacks:
        response = await upload(owner, room_id, data, ctype)
        assert (response.status_code, response.json()["error"]["code"]) == (
            415,
            "unsupported_media_type",
        ), ctype
    assert (
        stored_files(store) == []
        and await scalar(sessionmaker, "SELECT count(*) FROM room_assets") == 0
    )


async def test_unusable_images_are_422_with_a_reason(make_actor, cfg, sessionmaker, store):
    owner, _e, _v, room_id = await owner_room(make_actor, cfg, sessionmaker)
    tiny_limit = cfg.model_copy(update={"asset_max_pixels": 100})
    small_owner = await make_actor("Small", app_settings=tiny_limit)
    small_room = await new_room(small_owner)
    cases = [
        (owner, room_id, animated_webp(), "image/webp", "animated"),
        (owner, room_id, png()[:50], "image/png", "invalid_image"),
        (owner, room_id, b"", "image/png", "empty"),
        (small_owner, small_room, png(20, 20), "image/png", "too_many_pixels"),
    ]
    for actor, rid, data, ctype, reason in cases:
        response = await upload(actor, rid, data, ctype)
        error = response.json()["error"]
        assert (response.status_code, error["code"], error["reason"]) == (
            422,
            "invalid_image",
            reason,
        )
    assert stored_files(store) == []


async def test_metadata_never_reaches_the_stored_or_served_bytes(make_actor, cfg, sessionmaker):
    owner, _e, viewer, room_id = await owner_room(make_actor, cfg, sessionmaker)
    created = (await upload(owner, room_id, jpeg_with_exif(), "image/jpeg")).json()
    served = await viewer.get(f"/rooms/{room_id}/assets/{created['id']}")
    for secret in (b"SecretCameraMaker", b"secret-comment", b"Exif"):
        assert secret not in served.content
    with Image.open(io.BytesIO(served.content)) as img:
        assert img.size == (10, 20) and len(img.getexif()) == 0


# ---- upload: size ---------------------------------------------------------------------------------------------------------


async def test_oversized_uploads_are_413_by_actual_bytes(make_actor, cfg, sessionmaker, store):
    small = cfg.model_copy(update={"asset_max_upload_bytes": 5_000})
    owner = await make_actor("Small", app_settings=small)
    room_id = await new_room(owner)
    big = png(300, 300, color=(1, 2, 3))
    noisy = io.BytesIO()
    Image.frombytes("RGB", (60, 60), os.urandom(60 * 60 * 3)).save(noisy, "PNG")
    assert len(noisy.getvalue()) > 5_000
    response = await upload(owner, room_id, noisy.getvalue())
    assert (response.status_code, response.json()["error"]["code"]) == (413, "payload_too_large")
    assert big  # a compressible image under the byte limit is fine
    assert (await upload(owner, room_id, png(40, 40))).status_code == 201
    # a lying Content-Length and a chunked body (no Content-Length) are counted the same way
    csrf = await owner.csrf_token()
    headers = {"X-CSRF-Token": csrf, "Origin": TRUSTED_ORIGIN, "Content-Type": "image/png"}
    lying = await owner.client.post(
        f"/rooms/{room_id}/assets",
        content=noisy.getvalue(),
        headers={**headers, "Content-Length": "100"},
    )
    assert lying.status_code == 413

    async def chunks():
        for i in range(0, len(noisy.getvalue()), 1000):
            yield noisy.getvalue()[i : i + 1000]

    chunked = await owner.client.post(f"/rooms/{room_id}/assets", content=chunks(), headers=headers)
    assert chunked.status_code == 413
    assert (
        len(stored_files(store)) == 1
        and await scalar(sessionmaker, "SELECT count(*) FROM room_assets") == 1
    )
    assert list(store.tmp_dir.iterdir()) == []  # no temporary file survived any of the failures


# ---- quotas and rate limit -----------------------------------------------------------------------------------------------------


async def test_the_room_byte_quota_and_count_quota(make_actor, cfg, sessionmaker, store):
    first = png(30, 30)
    limited = cfg.model_copy(
        update={
            "asset_room_quota_bytes": len(first) * 2 + 10,
            "asset_max_upload_bytes": len(first) * 2,
        }
    )
    owner = await make_actor("Quota", app_settings=limited)
    room_id = await new_room(owner)
    assert (await upload(owner, room_id, first)).status_code == 201
    second = await upload(owner, room_id, png(30, 30, color=(9, 9, 9)))
    assert second.status_code == 201
    refused = await upload(owner, room_id, png(30, 30, color=(4, 4, 4)))
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "quota_exceeded")
    assert len(stored_files(store)) == 2  # the refused upload's bytes were removed again
    # expired assets stop counting
    await execute(
        sessionmaker,
        "UPDATE room_assets SET state = 'expired' WHERE id = (SELECT id FROM room_assets LIMIT 1)",
    )
    assert (await upload(owner, room_id, png(30, 30, color=(4, 4, 4)))).status_code == 201

    counted = cfg.model_copy(update={"asset_max_per_room": 2})
    other = await make_actor("Count", app_settings=counted)
    other_room = await new_room(other)
    assert [(await upload(other, other_room, png(5 + i, 5))).status_code for i in range(3)] == [
        201,
        201,
        409,
    ]


async def test_the_upload_rate_limit_is_per_user_per_room_and_refusals_consume_nothing(
    make_actor, cfg, sessionmaker
):
    limited = cfg.model_copy(update={"asset_uploads_per_minute": 2})
    owner = await make_actor("Rate", app_settings=limited)
    editor = await make_actor("Rate2", app_settings=limited)
    room_id = await new_room(owner)
    await add_member(sessionmaker, room_id, editor.id, "editor")
    # refused uploads (wrong type) do not count
    for _ in range(5):
        assert (await upload(owner, room_id, png(), "text/plain")).status_code == 415
    assert (await upload(owner, room_id, png())).status_code == 201
    assert (await upload(owner, room_id, png())).status_code == 201
    third = await upload(owner, room_id, png())
    assert (third.status_code, third.json()["error"]["code"]) == (429, "rate_limited")
    assert 1 <= int(third.headers["retry-after"]) <= 60
    assert (await upload(editor, room_id, png())).status_code == 201  # another user, own budget
    other_room = await new_room(owner)
    assert (await upload(owner, other_room, png())).status_code == 201  # another room, own budget
    assert await scalar(sessionmaker, "SELECT count(*) FROM room_assets") == 4


async def test_uploading_does_not_use_the_document_save_quota(make_actor, cfg, sessionmaker):
    limited = cfg.model_copy(update={"document_writes_per_minute": 1})
    owner = await make_actor("Both", app_settings=limited)
    room_id, doc = await room_with_doc(owner, sessionmaker)
    for _ in range(4):
        assert (await upload(owner, room_id, png())).status_code == 201
    assert (await save(owner, room_id, doc["id"], 1, content("still allowed"))).status_code == 200


# ---- download -------------------------------------------------------------------------------------------------------------------


async def test_members_download_with_safe_headers_and_an_etag(make_actor, cfg, sessionmaker):
    owner, editor, viewer, room_id = await owner_room(make_actor, cfg, sessionmaker)
    created = (await upload(editor, room_id, webp(), "image/webp")).json()
    for actor in (owner, editor, viewer):
        response = await actor.get(f"/rooms/{room_id}/assets/{created['id']}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/webp"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"].startswith("private")
        assert response.headers["etag"] == f'"{created["sha256"]}"'
        assert response.headers["content-security-policy"].startswith("default-src 'none'")
        assert hashlib.sha256(response.content).hexdigest() == created["sha256"]
    cached = await viewer.get(
        f"/rooms/{room_id}/assets/{created['id']}",
        headers={"If-None-Match": f'"{created["sha256"]}"'},
    )
    assert (cached.status_code, cached.content) == (304, b"")


async def test_download_is_authorized_on_every_request_and_scoped_to_the_room(
    make_actor, cfg, sessionmaker, client
):
    owner, _e, viewer, room_id = await owner_room(make_actor, cfg, sessionmaker)
    created = (await upload(owner, room_id, png())).json()
    url = f"/rooms/{room_id}/assets/{created['id']}"
    assert (await client.get(url)).status_code == 401
    stranger = await make_actor("Stranger", app_settings=cfg)
    assert (await stranger.get(url)).status_code == 404
    other_owner = await make_actor("Other", app_settings=cfg)
    other_room = await new_room(other_owner)
    cross = await other_owner.get(f"/rooms/{other_room}/assets/{created['id']}")
    assert (cross.status_code, cross.json()["error"]["code"]) == (404, "asset_not_found")
    await execute(
        sessionmaker,
        "UPDATE memberships SET state = 'removed', removed_at = now() WHERE user_id = :u",
        u=viewer.id,
    )
    assert (
        await viewer.get(url)
    ).status_code == 404  # removal takes effect on the very next request
    for junk in ("not-a-uuid", str(uuid.uuid4()), "..%2F..%2Fetc%2Fpasswd"):
        assert (await owner.get(f"/rooms/{room_id}/assets/{junk}")).status_code == 404


async def test_expired_or_missing_bytes_are_404(make_actor, cfg, sessionmaker, store):
    owner, _e, _v, room_id = await owner_room(make_actor, cfg, sessionmaker)
    created = (await upload(owner, room_id, png())).json()
    url = f"/rooms/{room_id}/assets/{created['id']}"
    key = await scalar(sessionmaker, "SELECT storage_key FROM room_assets")
    store.delete("assets", key)
    assert (await owner.get(url)).status_code == 404  # row without bytes
    await execute(sessionmaker, "UPDATE room_assets SET state = 'expired'")
    assert (await owner.get(url)).status_code == 404


async def test_saving_a_document_attaches_the_asset_and_it_can_be_displayed(
    make_actor, cfg, sessionmaker
):
    owner, _e, viewer, room_id = await owner_room(make_actor, cfg, sessionmaker)
    doc = (
        await owner.post(
            f"/rooms/{room_id}/documents",
            json={"title": "D", "template_key": "blank", "client_request_id": new_id()},
        )
    ).json()
    created = (await upload(owner, room_id, png())).json()
    assert (await save(owner, room_id, doc["id"], 1, with_image(created["id"]))).status_code == 200
    assert await scalar(sessionmaker, "SELECT state FROM room_assets") == "attached"
    assert (await viewer.get(f"/rooms/{room_id}/assets/{created['id']}")).status_code == 200


# ---- lifecycle cleanup -------------------------------------------------------------------------------------------------------------


async def make_assets(make_actor, cfg, sessionmaker):
    owner, _e, _v, room_id = await owner_room(make_actor, cfg, sessionmaker)
    ids = []
    for i in range(3):
        ids.append((await upload(owner, room_id, png(8 + i, 6))).json()["id"])
    return owner, room_id, ids


async def test_unused_pending_assets_expire_after_the_ttl_and_their_bytes_go(
    make_actor, cfg, sessionmaker, store
):
    _o, _room, ids = await make_assets(make_actor, cfg, sessionmaker)
    young, old_pending, _third = ids
    await execute(
        sessionmaker,
        "UPDATE room_assets SET created_at = now() - interval '25 hours' WHERE id = :i",
        i=old_pending,
    )
    result = await cleanup_documents(sessionmaker, store)
    assert result.pending_expired == 1 and result.unreferenced_expired == 0
    states = {
        r["id"]: r["state"] for r in await rows(sessionmaker, "SELECT id, state FROM room_assets")
    }
    assert states[uuid.UUID(old_pending)] == "expired" and states[uuid.UUID(young)] == "pending"
    assert len(stored_files(store)) == 2
    again = await cleanup_documents(sessionmaker, store)
    assert (again.pending_expired, again.unreferenced_expired) == (0, 0)  # idempotent


async def test_a_dropped_image_survives_the_grace_period_then_expires_but_a_referenced_one_never_does(
    make_actor, cfg, sessionmaker, store
):
    owner, room_id, ids = await make_assets(make_actor, cfg, sessionmaker)
    doc = (
        await owner.post(
            f"/rooms/{room_id}/documents",
            json={"title": "D", "template_key": "blank", "client_request_id": new_id()},
        )
    ).json()
    a, b, _c = ids
    body = with_image(a)
    body["blocks"][0]["children"].append(
        {"type": "image", "id": new_id(), "asset_id": b, "width_px": 5, "height_px": 5}
    )
    assert (await save(owner, room_id, doc["id"], 1, body)).status_code == 200
    assert (await save(owner, room_id, doc["id"], 2, with_image(a))).status_code == 200  # b dropped
    assert await scalar(
        sessionmaker, "SELECT unreferenced_since IS NOT NULL FROM room_assets WHERE id = :i", i=b
    )
    # inside the 7-day grace period: nothing happens
    await execute(
        sessionmaker,
        "UPDATE room_assets SET unreferenced_since = now() - interval '6 days' WHERE id = :i",
        i=b,
    )
    assert (await cleanup_documents(sessionmaker, store)).unreferenced_expired == 0
    assert (
        await owner.get(f"/rooms/{room_id}/assets/{b}")
    ).status_code == 200  # a client undo still works
    # past it: expired and the bytes deleted; the still-referenced asset is untouched
    await execute(
        sessionmaker,
        "UPDATE room_assets SET unreferenced_since = now() - interval '8 days' WHERE id = :i",
        i=b,
    )
    await execute(
        sessionmaker,
        "UPDATE room_assets SET created_at = now() - interval '9 days' WHERE id = :i",
        i=a,
    )
    result = await cleanup_documents(sessionmaker, store)
    assert result.unreferenced_expired == 1
    states = {
        str(r["id"]): r["state"]
        for r in await rows(sessionmaker, "SELECT id, state FROM room_assets")
    }
    assert (states[a], states[b]) == ("attached", "expired")
    assert (await owner.get(f"/rooms/{room_id}/assets/{b}")).status_code == 404


async def test_re_referencing_inside_the_grace_period_clears_the_clock(
    make_actor, cfg, sessionmaker, store
):
    owner, room_id, ids = await make_assets(make_actor, cfg, sessionmaker)
    doc = (
        await owner.post(
            f"/rooms/{room_id}/documents",
            json={"title": "D", "template_key": "blank", "client_request_id": new_id()},
        )
    ).json()
    a = ids[0]
    assert (await save(owner, room_id, doc["id"], 1, with_image(a))).status_code == 200
    assert (await save(owner, room_id, doc["id"], 2, content("none"))).status_code == 200
    await execute(
        sessionmaker,
        "UPDATE room_assets SET unreferenced_since = now() - interval '8 days' WHERE id = :i",
        i=a,
    )
    assert (
        await save(owner, room_id, doc["id"], 3, with_image(a))
    ).status_code == 200  # the undo, saved
    assert (await cleanup_documents(sessionmaker, store)).unreferenced_expired == 0


async def test_orphan_files_and_stale_temp_files_are_removed_only_when_old(
    make_actor, cfg, sessionmaker, store
):
    _o, _room, ids = await make_assets(make_actor, cfg, sessionmaker)
    orphan_key = store.new_key()
    fd, tmp = store.temp_file()
    os.write(fd, b"x")
    os.close(fd)
    store.put("assets", orphan_key, tmp)
    fd, stale = store.temp_file()
    os.close(fd)
    now = time.time()
    young = await cleanup_documents(sessionmaker, store, now=now)
    assert (young.orphan_files, young.temp_files) == (0, 0)  # too new to judge
    assert store.exists("assets", orphan_key) and stale.exists()
    old = await cleanup_documents(sessionmaker, store, now=now + 25 * 3600)
    assert (old.orphan_files, old.temp_files) == (1, 1)
    assert not store.exists("assets", orphan_key) and not stale.exists()
    assert len(stored_files(store)) == 3  # files with live rows are never touched, however old
    assert ids


async def test_the_cleanup_command_runs_as_a_program(
    make_actor, cfg, sessionmaker, store, storage_dir
):
    import subprocess
    import sys

    from tests.factories import TEST_DATABASE_URL

    _o, _room, ids = await make_assets(make_actor, cfg, sessionmaker)
    await execute(
        sessionmaker,
        "UPDATE room_assets SET created_at = now() - interval '2 days' WHERE id = :i",
        i=ids[0],
    )
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL, "STORAGE_PATH": str(storage_dir)}
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "app.commands.cleanup_documents"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "expired 1 unused pending" in result.stdout
    assert len(stored_files(store)) == 2


async def test_old_document_write_windows_are_cleaned_with_the_other_buckets(
    make_actor, cfg, sessionmaker
):
    from app.commands.cleanup_auth import cleanup_auth

    owner, _e, _v, room_id = await owner_room(make_actor, cfg, sessionmaker)
    await execute(
        sessionmaker,
        "INSERT INTO document_write_buckets (room_id, user_id, window_start, count)"
        " VALUES (:r, :u, now() - interval '3 days', 5), (:r, :u, now(), 1)",
        r=room_id,
        u=owner.id,
    )
    result = await cleanup_auth(sessionmaker)
    assert result.document_write_buckets == 1
    assert await scalar(sessionmaker, "SELECT count(*) FROM document_write_buckets") == 1
