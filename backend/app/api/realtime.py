"""The room WebSocket:  /rooms/{room_id}/ws

HANDSHAKE (all before accept). Three checks, one identical failure:
  * Origin: exact match against TRUSTED_ORIGINS; a missing Origin is rejected (browser-only
    endpoint). This is NOT CORS: browsers do not apply CORS to WebSocket handshakes, and the session
    cookie is attached to a cross-site handshake automatically, so without an Origin check any web
    page could open an authenticated socket as the visitor ("cross-site WebSocket hijacking").
  * Session: the cookie is validated by load_session(), the function behind the HTTP dependency.
    Credentials are never taken from the URL.
  * Membership: the user must be an ACTIVE member of this room.
Every failure is the same bare HTTP 403 (Starlette turns a close before accept into a handshake
rejection), so an unknown room, a nonmember, a bad session and a bad origin look identical.

Message writes stay on HTTP; this socket only delivers events.
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket

from app.dependencies.rooms import parse_uuid

router = APIRouter(tags=["realtime"])


@router.websocket("/rooms/{room_id}/ws")
async def room_socket(websocket: WebSocket, room_id: str) -> None:
    app = websocket.app
    settings = app.state.settings
    hub = app.state.realtime

    origin = websocket.headers.get("origin")
    parsed_room = parse_uuid(room_id)
    identity = None
    if origin is not None and origin.lower() in settings.trusted_origins:
        identity = await hub.authenticate(
            websocket.cookies.get(settings.session_cookie_name), parsed_room
        )
    if identity is None or parsed_room is None:
        await websocket.close()  # BEFORE accept: the client sees HTTP 403 and nothing else
        return

    await websocket.accept()
    await hub.serve(websocket, parsed_room, identity)
