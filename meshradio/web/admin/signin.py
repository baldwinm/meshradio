"""Signing in and out: the password page, the two-step code page, and the
session cookie a right answer earns. The checks themselves (scrypt, codes,
the throttle) are ``admin_auth``'s."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from ..admin_auth import (
    ADMIN_COOKIE,
    ADMIN_PATH,
    SESSION_IDLE_S,
    SESSION_MAX_S,
    STEP_COOKIE,
    STEP_MAX_TRIES,
    STEP_PATH,
    STEP_TTL_S,
    check_totp,
    csrf_token,
    new_session_token,
    token_hash,
    verify_password,
)
from ..context import ctx_of
from .common import (
    checked_form,
    client_ip,
    current_admin,
    field,
    go,
    https,
    read_form,
    render,
    safe_next,
    settings_of,
)

router = APIRouter(prefix=ADMIN_PATH)

TOTP_STEP_KEY = "admin.totp_last_step"


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = ""):
    if await current_admin(request):
        return go(safe_next(next))
    # The same page whether or not a code is configured: nothing here tells
    # a visitor there's a second step.
    return render(request, "login.html", None, next=next)


def _paused(throttle_wait: float) -> str:
    minutes = max(1, round(throttle_wait / 60))
    return (f"Too many tries. Sign-in is paused for {minutes} more minute"
            f"{'' if minutes == 1 else 's'}.")


@router.post("/login")
async def login(request: Request):
    settings = settings_of(request)
    db = ctx_of(request).db
    ip = client_ip(request)
    form = await read_form(request)
    nxt = field(form, "next")
    now = time.time()

    wait = settings.throttle.locked_for(ip, now)
    if wait:
        return render(request, "login.html", None, next=nxt, error=_paused(wait),
                      status_code=429)
    settings.throttle.begin(ip, now)

    password = form.get("password", [""])[0]
    # scrypt is ~50 ms of CPU: off the event loop, so a sign-in can't stall
    # every visitor's page while it runs.
    ok = bool(password) and await asyncio.to_thread(
        verify_password, password, settings.password_hash
    )
    if not ok:
        await db.log_admin("sign_in_failed", ip=ip)
        return render(request, "login.html", None, next=nxt,
                      error="That password didn't match.", status_code=401)

    if not settings.totp_secret:
        settings.throttle.succeed(ip, now)
        return await _signed_in(request, nxt)

    # Right password, code still to come. The attempt isn't a failure, but
    # the address's earlier ones stand until the code is right too.
    settings.throttle.passed(ip, now)
    step = settings.pending.open(ip, nxt, settings.fingerprint, now)
    response = go(f"{STEP_PATH}/code")
    response.set_cookie(
        STEP_COOKIE, step, max_age=STEP_TTL_S, path=STEP_PATH,
        httponly=True, samesite="strict", secure=https(request),
    )
    return response


def _drop_step(response: Response) -> Response:
    response.delete_cookie(STEP_COOKIE, path=STEP_PATH)
    return response


@router.get("/login/code", response_class=HTMLResponse)
async def code_page(request: Request):
    settings = settings_of(request)
    step = request.cookies.get(STEP_COOKIE)
    if not settings.totp_secret or settings.pending.get(
        step, client_ip(request), settings.fingerprint, time.time()
    ) is None:
        return _drop_step(go(f"{ADMIN_PATH}/login"))
    return render(request, "login_code.html", step)


@router.post("/login/code")
async def code(request: Request):
    settings = settings_of(request)
    db = ctx_of(request).db
    ip = client_ip(request)
    now = time.time()
    step = request.cookies.get(STEP_COOKIE)
    form = await read_form(request)

    pending = None
    if settings.totp_secret:
        pending = settings.pending.get(step, ip, settings.fingerprint, now)
    if pending is None or step is None:
        return _drop_step(go(f"{ADMIN_PATH}/login"))
    if field(form, "csrf") != csrf_token(step):
        raise HTTPException(403, "this form is out of date; reload the page and try again")

    wait = settings.throttle.locked_for(ip, now)
    if wait:
        settings.pending.close(step)
        return _drop_step(render(request, "login.html", None, next=pending.landing,
                                 error=_paused(wait), status_code=429))
    settings.throttle.begin(ip, now)

    # The last step used survives a restart, so a code seen once can't be
    # replayed after one.
    stored = int(await db.get_setting(TOTP_STEP_KEY, "-1") or -1)
    last = max(settings.last_totp_counter, stored)
    counter = check_totp(settings.totp_secret, field(form, "code"), now, last)
    if counter is None:
        pending.tries += 1
        # Logged apart from a wrong password: it means the password is known.
        await db.log_admin("sign_in_code_failed", ip=ip)
        if pending.tries >= STEP_MAX_TRIES:
            settings.pending.close(step)
            return _drop_step(render(request, "login.html", None, next=pending.landing,
                                     error="That code didn't match. Start again.",
                                     status_code=401))
        return render(request, "login_code.html", step,
                      error="That code didn't match.", status_code=401)

    settings.throttle.succeed(ip, now)
    settings.last_totp_counter = counter
    await db.set_setting(TOTP_STEP_KEY, str(counter))
    settings.pending.close(step)
    return _drop_step(await _signed_in(request, pending.landing))


async def _signed_in(request: Request, landing: str) -> Response:
    """Start a session: a fresh token (never one the browser brought), its
    hash stored, the cookie scoped to /admin."""
    settings = settings_of(request)
    db = ctx_of(request).db
    ip = client_ip(request)
    now = time.time()
    token = new_session_token()
    await db.prune_admin_sessions(now - SESSION_MAX_S, now - SESSION_IDLE_S)
    await db.create_admin_session(token_hash(token), ip, settings.fingerprint)
    await db.log_admin("sign_in", ip=ip)
    response = go(safe_next(landing))
    response.set_cookie(
        ADMIN_COOKIE, token, max_age=SESSION_MAX_S, path=ADMIN_PATH,
        httponly=True, samesite="strict", secure=https(request),
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    token, _form = await checked_form(request)
    db = ctx_of(request).db
    await db.end_admin_session(token_hash(token))
    await db.log_admin("sign_out", ip=client_ip(request))
    response = go(f"{ADMIN_PATH}/login")
    response.delete_cookie(ADMIN_COOKIE, path=ADMIN_PATH)
    return response
