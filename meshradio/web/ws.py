"""Live-state WebSocket: forwards player state to pages, elects speakers."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from ..bus import OUTPUT_CHANGED, PLAYER_STATE, POWER_STATE
from ..media.player import PlayerService
from .context import ctx_of
from .sessions import SESSION_COOKIE, SpeakerRegistry, valid_sid

log = logging.getLogger(__name__)

router = APIRouter()


async def broadcast_state(reg: SpeakerRegistry, p: PlayerService) -> None:
    """Push fresh state to a session's pages (speaker role may have moved)."""
    state = p.state()  # snapshot once; only the speaker flag is per-connection
    for conn in reg.clients():
        try:
            await conn.send_json({
                "topic": PLAYER_STATE,
                "data": {**state, "speaker": reg.is_speaker(conn)},
            })
        except Exception:
            pass


@router.websocket("/ws")
async def ws(websocket: WebSocket):
    ctx = ctx_of(websocket)
    if ctx.sessions is not None:
        # Per-visitor session: this browser's own player/bus/speakers. The
        # page that opens this socket always carries the cookie (the HTTP
        # middleware issued it with the page), so a handshake without a
        # valid one is not our page. Minting a sid here would open a session
        # nothing could ever present again — one per bot connection.
        sid = websocket.cookies.get(SESSION_COOKIE)
        if not valid_sid(sid):
            await websocket.send_denial_response(
                PlainTextResponse("session cookie required", status_code=403)
            )
            return
        await websocket.accept()
        session = await ctx.sessions.get(sid)
        reg, p = session.speakers, session.player
        sub = session.bus.subscribe(PLAYER_STATE)
    else:
        await websocket.accept()
        reg, p = ctx.speakers, ctx.player
        sub = ctx.bus.subscribe(PLAYER_STATE, OUTPUT_CHANGED, POWER_STATE)
    reg.join(websocket)

    async def recv_loop():
        while True:
            msg = await websocket.receive_text()
            if msg == "claim":
                reg.claim(websocket)
                await broadcast_state(reg, p)

    async def send_loop():
        async for topic, payload in sub:
            if topic == PLAYER_STATE:
                payload = {**payload, "speaker": reg.is_speaker(websocket)}
            await websocket.send_json({"topic": topic, "data": payload})

    tasks = [asyncio.create_task(recv_loop()), asyncio.create_task(send_loop())]
    try:
        await broadcast_state(reg, p)  # joining may reassign the speaker
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        log.debug("websocket closed", exc_info=True)
    finally:
        for task in tasks:
            task.cancel()
        sub.close()
        reg.leave(websocket)
        await broadcast_state(reg, p)  # promote the next speaker
