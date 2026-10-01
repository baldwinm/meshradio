"""Live-state WebSocket: forwards player state to pages, elects speakers."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..bus import OUTPUT_CHANGED, PLAYER_STATE, POWER_STATE
from ..media.player import PlayerService
from .context import ctx_of
from .sessions import SESSION_COOKIE, SpeakerRegistry, verify_cookie

log = logging.getLogger(__name__)

router = APIRouter()

# A page that has stopped reading (a laptop lid closing mid-send) must not
# hold the others' state update; past this the send is abandoned and the
# socket's own loops will notice the dead connection.
SEND_TIMEOUT_S = 5.0


async def broadcast_state(reg: SpeakerRegistry, p: PlayerService) -> None:
    """Push fresh state to a session's pages (speaker role may have moved).
    All at once: one slow socket used to delay everyone after it in line."""
    state = p.state()  # snapshot once; only the speaker flag is per-connection

    async def push(conn) -> None:
        try:
            await asyncio.wait_for(
                conn.send_json({
                    "topic": PLAYER_STATE,
                    "data": {**state, "speaker": reg.is_speaker(conn)},
                }),
                timeout=SEND_TIMEOUT_S,
            )
        except Exception:
            pass

    clients = reg.clients()
    if clients:
        await asyncio.gather(*(push(conn) for conn in clients))


@router.websocket("/ws")
async def ws(websocket: WebSocket):
    ctx = ctx_of(websocket)
    if ctx.sessions is not None:
        # Per-visitor session: this browser's own player/bus/speakers. The
        # page that opens this socket always carries the cookie (the HTTP
        # middleware issued it with the page), so a handshake without one we
        # signed is not our page. Minting a sid here would open a session
        # nothing could ever present again — one per bot connection.
        sid = verify_cookie(websocket.cookies.get(SESSION_COOKIE), await ctx.sessions.secret())
        if sid is None:
            # Close before accept: uvicorn turns that into a 403 (see
            # server.OriginGuard._deny_websocket for why not a denial body).
            await websocket.close(code=1008)
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
