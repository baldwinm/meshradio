"""Live-state WebSocket: forwards player state to pages, elects speakers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

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

# Process-wide ceiling on open sockets, over the per-session and communal
# ones in sessions.py: every socket is two tasks and a fan-out target, and
# there is nothing a thousand of them could be showing.
MAX_SOCKETS = 1024

# How often one page may claim the speaker role. A claim re-elects and
# broadcasts fresh state to every socket in the registry, so a page sending
# "claim" in a loop was a fan-out to everyone on each message.
CLAIM_INTERVAL_S = 1.0

# "Try again later": the close code for a refused-because-full handshake,
# as distinct from the policy violation a bad origin or cookie gets.
_TRY_AGAIN_LATER = 1013


async def broadcast_state(reg: SpeakerRegistry, p: PlayerService) -> None:
    """Push fresh state to a session's pages (speaker role may have moved).
    All at once: one slow socket used to delay everyone after it in line."""
    state = p.state()  # snapshot once; only the speaker flag is per-connection

    async def push(conn) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                conn.send_json({
                    "topic": PLAYER_STATE,
                    "data": {**state, "speaker": reg.is_speaker(conn)},
                }),
                timeout=SEND_TIMEOUT_S,
            )

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
        session = await ctx.sessions.get(sid)
        reg, p = session.speakers, session.player
        topics: tuple[str, ...] = (PLAYER_STATE,)
        bus = session.bus
    else:
        reg, p = ctx.speakers, ctx.player
        topics = (PLAYER_STATE, OUTPUT_CHANGED, POWER_STATE)
        bus = ctx.bus
    # The slot is taken before the handshake completes so two handshakes
    # can't both squeeze past the same last place; a refused one is closed
    # before accept, like a bad origin or cookie, with a code that says why.
    if ctx.open_sockets >= MAX_SOCKETS or not reg.join(websocket):
        await websocket.close(code=_TRY_AGAIN_LATER)
        return
    ctx.open_sockets += 1
    sub = None
    tasks: list[asyncio.Task] = []
    last_claim = float("-inf")

    async def recv_loop():
        nonlocal last_claim
        while True:
            msg = await websocket.receive_text()
            # A claim from the page that already has the role changes nothing,
            # and one page may only re-elect so often: each claim broadcasts
            # to every socket in the registry.
            if msg != "claim" or reg.is_speaker(websocket):
                continue
            now = time.monotonic()
            if now - last_claim < CLAIM_INTERVAL_S:
                continue
            last_claim = now
            reg.claim(websocket)
            await broadcast_state(reg, p)

    async def send_loop():
        async for topic, payload in sub:
            if topic == PLAYER_STATE:
                payload = {**payload, "speaker": reg.is_speaker(websocket)}
            await websocket.send_json({"topic": topic, "data": payload})

    try:
        await websocket.accept()
        sub = bus.subscribe(*topics)
        tasks = [asyncio.create_task(recv_loop()), asyncio.create_task(send_loop())]
        await broadcast_state(reg, p)  # joining may reassign the speaker
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        log.debug("websocket closed", exc_info=True)
    finally:
        for task in tasks:
            task.cancel()
        if sub is not None:
            sub.close()
        reg.leave(websocket)
        ctx.open_sockets -= 1
        await broadcast_state(reg, p)  # promote the next speaker
