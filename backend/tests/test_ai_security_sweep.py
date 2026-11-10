"""Cross-cutting security checks for the AI feature (phase 1, checkpoint 5).

The per-feature tests already cover the permission table, key handling, logs and prompt injection where
they happen. These tests guard the properties that must stay true however the code changes:

* the AI HTTP surface is exactly the reviewed list (a new route fails here until someone reviews it);
* no AI route is reachable without a login, and an outsider cannot tell a real room from a missing one;
* no internal (service-to-service) route lives in the public API;
* provider calls are only reachable from the modules meant to make them (never from API route modules,
  services or models);
* every retrieval query filters by room and by the pinned generations inside the SQL.
"""

from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path

import pytest

from app.ai import retrieval
from tests.analysis_helpers import build_world
from tests.helpers import scalar

APP = Path(__file__).resolve().parents[1] / "app"

REVIEWED_ROUTES = {
    ("GET", "/ai/consent"),
    ("POST", "/ai/consent"),
    ("GET", "/ai/keys"),
    ("DELETE", "/ai/keys/{provider}"),
    ("PUT", "/ai/keys/{provider}"),
    ("GET", "/ai/status"),
    ("POST", "/rooms/{room_id}/ai-requests"),
    ("POST", "/rooms/{room_id}/ai-requests/estimate"),
    ("GET", "/rooms/{room_id}/ai-requests/{request_id}"),
    ("POST", "/rooms/{room_id}/ai-requests/{request_id}/cancel"),
    ("GET", "/rooms/{room_id}/ai-requests/{request_id}/citations/{n}"),
    ("GET", "/rooms/{room_id}/ai-requests/{request_id}/citations/{n}/image"),
    ("GET", "/rooms/{room_id}/ai-requests/{request_id}/context"),
    ("GET", "/rooms/{room_id}/ai-requests/{request_id}/result"),
    ("POST", "/rooms/{room_id}/ai-requests/{request_id}/retry"),
    ("GET", "/rooms/{room_id}/ai-proposals/{proposal_id}"),
    ("POST", "/rooms/{room_id}/ai-proposals/{proposal_id}/apply"),
    ("POST", "/rooms/{room_id}/ai-proposals/{proposal_id}/reject"),
    ("POST", "/rooms/{room_id}/ai-proposals/{proposal_id}/regenerate"),
    ("POST", "/rooms/{room_id}/ai-proposals/{proposal_id}/publish"),
    ("GET", "/rooms/{room_id}/ai-settings"),
    ("PUT", "/rooms/{room_id}/ai-settings"),
    ("GET", "/rooms/{room_id}/ai-usage"),
    ("POST", "/rooms/{room_id}/source-uploads"),
    ("DELETE", "/rooms/{room_id}/source-uploads/{upload_id}"),
    ("GET", "/rooms/{room_id}/source-uploads/{upload_id}"),
    ("PUT", "/rooms/{room_id}/source-uploads/{upload_id}/content"),
    ("POST", "/rooms/{room_id}/source-uploads/{upload_id}/finalize"),
    ("GET", "/rooms/{room_id}/sources"),
    ("DELETE", "/rooms/{room_id}/sources/{source_id}"),
    ("GET", "/rooms/{room_id}/sources/{source_id}"),
    ("GET", "/rooms/{room_id}/sources/{source_id}/versions/{version_id}/analysis"),
    ("DELETE", "/rooms/{room_id}/sources/{source_id}/versions/{version_id}/analyze"),
    ("POST", "/rooms/{room_id}/sources/{source_id}/versions/{version_id}/analyze"),
    ("GET", "/rooms/{room_id}/sources/{source_id}/versions/{version_id}/download"),
    ("GET", "/rooms/{room_id}/sources/{source_id}/versions/{version_id}/passages"),
}
AI_PATH = re.compile(r"^/(ai/|rooms/\{room_id\}/(ai-|source))")
OPENAPI_METHODS = {"get", "post", "put", "patch", "delete"}


@pytest.fixture
async def world(make_actor, sessionmaker, tmp_path, redis_prefix):
    return await build_world(make_actor, sessionmaker, tmp_path, redis_prefix)


async def surface(world) -> set[tuple[str, str]]:
    spec = (await world.owner.get("/openapi.json")).json()
    return {
        (method.upper(), path)
        for path, ops in spec["paths"].items()
        for method in ops
        if method in OPENAPI_METHODS and AI_PATH.match(path)
    }


async def test_the_ai_http_surface_is_exactly_the_reviewed_list(world):
    found = await surface(world)
    assert found == REVIEWED_ROUTES, (
        f"unreviewed: {sorted(found - REVIEWED_ROUTES)}; gone: {sorted(REVIEWED_ROUTES - found)}"
    )


async def test_no_internal_route_is_part_of_the_public_api(world):
    spec = (await world.owner.get("/openapi.json")).json()
    assert [p for p in spec["paths"] if "internal" in p] == []
    # the collaboration service's private read route is not served here either
    for method in ("get", "post"):
        r = await getattr(world.owner, method)("/internal/ai/read")
        assert r.status_code in (404, 405)


def fill(path: str, ids: dict[str, str]) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: ids[m.group(1)], path)


async def real_ids(world) -> dict[str, str]:
    data = b"# Ops\n\nThe warehouse processed twelve thousand orders in March.\n"
    source = await world.upload(world.owner, "ops.md", data)
    await world.analyze(world.owner, source)
    await world.run_worker()
    request_id = (await world.send(world.owner, sources=[source], task_hint="ask")).json()["id"]
    await world.drain()
    pending = await world.owner.post(
        f"/rooms/{world.room_id}/source-uploads",
        json={"client_request_id": str(uuid.uuid4()), "filename": "x.md", "byte_size": 3},
    )
    sid, vid = world.version_of(source)
    return {
        "room_id": str(world.room_id),
        "source_id": sid,
        "version_id": vid,
        "request_id": request_id,
        "upload_id": pending.json()["id"],
        "proposal_id": str(uuid.uuid4()),
        "n": "1",
        "provider": "openai",
    }


def missing_ids() -> dict[str, str]:
    return {
        k: ("1" if k == "n" else "openai" if k == "provider" else str(uuid.uuid4()))
        for k in REAL_KEYS
    }


REAL_KEYS = (
    "room_id",
    "source_id",
    "version_id",
    "request_id",
    "upload_id",
    "proposal_id",
    "n",
    "provider",
)


async def call(actor, method: str, url: str):
    kwargs: dict = {}
    if method in ("POST", "PUT"):
        kwargs["json"] = {}
    return await getattr(actor, method.lower())(url, **kwargs)


async def test_anonymous_callers_are_refused_on_every_ai_route(world, make_actor):
    anon = await make_actor("Anon", app_settings=world.cfg)
    anon.client.cookies.clear()
    ids = await real_ids(world)
    for method, path in sorted(REVIEWED_ROUTES):
        kwargs = {"json": {}} if method in ("POST", "PUT") else {}
        r = await anon.client.request(method, fill(path, ids), **kwargs)  # no login, no CSRF token
        assert r.status_code in (401, 403), (method, path, r.status_code)
        assert "orders" not in r.text and ids["request_id"] not in r.text, (method, path)


async def test_an_outsider_cannot_tell_a_real_room_from_a_missing_one(world, make_actor):
    outsider = await make_actor("Outsider", app_settings=world.cfg)
    ids = await real_ids(world)
    for method, path in sorted(REVIEWED_ROUTES):
        if not path.startswith("/rooms/"):
            continue
        real = await call(outsider, method, fill(path, ids))
        gone = await call(outsider, method, fill(path, missing_ids()))
        shape = lambda r: (r.status_code, (r.json().get("error") or {}).get("code"))  # noqa: E731
        assert shape(real) == shape(gone), (method, path, shape(real), shape(gone))
        assert real.status_code in (403, 404), (method, path, real.status_code)
        assert "orders" not in real.text


async def test_an_outsiders_attempts_change_nothing(world, make_actor):
    outsider = await make_actor("Outsider2", app_settings=world.cfg)
    ids = await real_ids(world)
    counts = "SELECT (SELECT count(*) FROM ai_requests) + (SELECT count(*) FROM sources) + (SELECT count(*) FROM source_uploads) + (SELECT count(*) FROM analysis_jobs) AS n"
    before = await _scalar(world, counts)
    for method, path in sorted(REVIEWED_ROUTES):
        if method in ("POST", "PUT", "DELETE") and path.startswith("/rooms/"):
            await call(outsider, method, fill(path, ids))
    assert await _scalar(world, counts) == before


async def _scalar(world, query: str):
    return await scalar(world.sm, query)


# ---- structure ---------------------------------------------------------------------------------------------


def importers(prefixes: tuple[str, ...]) -> set[str]:
    """App modules that import any module whose dotted name starts with one of the prefixes."""
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *[f"{node.module}.{a.name}" for a in node.names]]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                continue
            if any(n == p or n.startswith(p + ".") for n in names for p in prefixes):
                found.add(path.relative_to(APP.parent).as_posix())
    return found


def test_provider_adapters_are_only_built_by_the_gateway_the_key_check_and_the_self_check():
    assert importers(("app.ai.factory", "app.ai.providers")) == {
        "app/ai/factory.py",
        "app/ai/analysis/gateway.py",
        "app/ai/keys.py",
        "app/ai/selfcheck.py",
    }


def test_the_metered_gateway_is_only_used_by_workers_and_operator_commands():
    users = importers(("app.ai.analysis.gateway",))
    assert users == {
        "app/ai/analysis/pipeline.py",
        "app/ai/answer/execute.py",
        "app/ai/answer/strategies.py",
        "app/ai/edit/generate.py",
        "app/commands/ai_retrieve.py",
    }
    for path in users:
        assert not path.startswith(("app/api/", "app/services/", "app/models/")), path


def test_no_api_route_module_calls_a_provider_directly():
    """Only app/api/ai.py (key validation and the self-check status) may touch app.ai.keys; nothing in
    app/api imports the factory, the providers or the gateway."""
    api = {p for p in importers(("app.ai.keys",)) if p.startswith("app/api/")}
    assert api == {"app/api/ai.py"}
    assert not {
        p
        for p in importers(("app.ai.factory", "app.ai.providers", "app.ai.analysis.gateway"))
        if p.startswith(("app/api/", "app/services/", "app/models/"))
    }


@pytest.mark.parametrize("name", ["_LEXICAL", "_VECTOR", "_FETCH", "_GEN_INFO"])
def test_every_retrieval_query_filters_by_room_and_pinned_generations_inside_the_sql(name):
    statement = str(getattr(retrieval, name))
    assert re.search(r"\.room_id = :room", statement), name
    assert re.search(r"generation_id = ANY\(:gens\)|g\.id = ANY\(:gens\)", statement), name
    if name in ("_LEXICAL", "_VECTOR", "_GEN_INFO"):
        assert "analysis_state <> 'removed'" in statement, name


async def test_a_removed_source_is_unreachable_through_every_read_route(world):
    ids = await real_ids(world)
    sid, vid = ids["source_id"], ids["version_id"]
    assert (await world.owner.delete(f"/rooms/{world.room_id}/sources/{sid}")).status_code in (
        200,
        204,
    )
    base = f"/rooms/{world.room_id}/sources/{sid}"
    for suffix in (
        "",
        f"/versions/{vid}/analysis",
        f"/versions/{vid}/passages",
        f"/versions/{vid}/download",
    ):
        r = await world.owner.get(base + suffix)
        assert r.status_code in (404, 409, 410), (suffix, r.status_code)
