"""Helpers for the document tests: building content and calling the document routes."""

from __future__ import annotations

import uuid

from tests.helpers import add_member
from tests.test_invitations_api import new_room


def new_id() -> str:
    return str(uuid.uuid4())


def text_node(value: str, **marks) -> dict:
    node = {"type": "text", "text": value}
    if marks:
        node["marks"] = marks
    return node


def paragraph(value: str = "", **attrs) -> dict:
    node: dict = {"type": "paragraph", "id": new_id()}
    if attrs:
        node["attrs"] = attrs
    if value:
        node["children"] = [text_node(value)]
    return node


def content(*paragraphs: str) -> dict:
    """A canonical document with one paragraph per string (an empty string is an empty paragraph)."""
    return {"schema_version": 1, "blocks": [paragraph(p) for p in (paragraphs or ("",))]}


async def create_doc(owner, room_id, title="Plan", template="blank", rid=None, **kw):
    return await owner.post(
        f"/rooms/{room_id}/documents",
        json={"title": title, "template_key": template, "client_request_id": rid or new_id()},
        **kw,
    )


async def save(actor, room_id, doc_id, version, body, **kw):
    return await actor.put(
        f"/rooms/{room_id}/documents/{doc_id}/content",
        json={"schema_version": 1, "expected_content_version": version, "content": body},
        **kw,
    )


async def room_with_doc(alice, sessionmaker, *members, template="blank"):
    """A room owned by alice with one document (active) and the given (actor, role) members."""
    room_id = await new_room(alice)
    for actor, role in members:
        await add_member(sessionmaker, room_id, actor.id, role)
    response = await create_doc(alice, room_id, template=template)
    assert response.status_code == 201, response.text
    return room_id, response.json()


async def events_of(sm, room_id, *, after=0):
    from tests.helpers import rows

    return await rows(
        sm,
        "SELECT seq, type, resource_type, resource_id, resource_version, payload FROM room_events"
        " WHERE room_id = :r AND seq > :a ORDER BY seq",
        r=room_id,
        a=after,
    )
