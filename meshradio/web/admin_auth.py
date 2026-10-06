"""Who may use the admin page, and how that's checked.

One operator, one password. The password is never stored: the server holds a
scrypt hash of it (``MESHRADIO_ADMIN_PASSWORD_HASH``, printed by
``meshradio --hash-admin-password``), and without one the admin page doesn't
exist at all. Two-step sign-in adds a time-based code from an authenticator
app (RFC 6238, ``MESHRADIO_ADMIN_TOTP_SECRET``). Everything here is the
standard library: no new dependency for the appliance to carry.

Sign-in is two steps when a code is configured, and the first never says so:
the password page looks the same either way, and only a right password leads
to the code page. That page is held by its own short-lived cookie, so the
code can't be tried without the password having passed first.

A successful sign-in issues a random cookie scoped to ``/admin``; the
database keeps only its hash. Forms carry a CSRF token derived from it, on
top of the origin guard every POST already passes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

ADMIN_COOKIE = "mr_admin"
ADMIN_PATH = "/admin"
# Held between a right password and its code, and sent only to the code page.
STEP_COOKIE = "mr_admin_step"
STEP_PATH = f"{ADMIN_PATH}/login"

# A sign-in lasts at most this long, and ends sooner after this much idle.
SESSION_MAX_S = 12 * 3600
SESSION_IDLE_S = 30 * 60

# Wrong passwords (or codes) from one address before sign-in pauses for it.
MAX_FAILURES = 5
LOCKOUT_S = 15 * 60
# Wrong tries from every address together before sign-in pauses for all of
# them: a guesser spreading over many addresses (or forging its address
# behind a proxy) still meets a ceiling. It's well above what one person
# mistyping makes, and the CLI still works while it holds.
GLOBAL_MAX_FAILURES = 50

# A right password opens the code page for this long, for this many codes.
STEP_TTL_S = 5 * 60
STEP_MAX_TRIES = 3

# scrypt's cost: 16 MiB and about 50 ms a check, which is what OWASP's
# password storage guidance names for scrypt at its lowest recommended setting.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
_SCRYPT_PREFIX = "scrypt"

# Authenticator-app codes: 6 digits from a 30-second step, accepting the step
# either side for a phone whose clock is a little off.
TOTP_STEP_S = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1


# -- passwords ----------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(password: str, *, n: int = SCRYPT_N) -> str:
    """``scrypt$N$r$p$salt$hash``, with everything needed to check it later."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return f"{_SCRYPT_PREFIX}${n}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def password_hash_ok(encoded: str) -> bool:
    """Whether ``encoded`` is a hash this module wrote (shape, not secret)."""
    return _parse_hash(encoded) is not None


def _parse_hash(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    parts = (encoded or "").strip().split("$")
    if len(parts) != 6 or parts[0] != _SCRYPT_PREFIX:
        return None
    try:
        n, r, p = (int(x) for x in parts[1:4])
        salt = base64.b64decode(parts[4], validate=True)
        digest = base64.b64decode(parts[5], validate=True)
    except ValueError:
        return None
    # Bounds keep a hand-edited hash from asking scrypt for gigabytes.
    if not (2 <= n <= 2**20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16):
        return None
    if len(salt) < 8 or len(digest) < 16:
        return None
    return n, r, p, salt, digest


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against a stored hash. A malformed
    hash never matches."""
    parsed = _parse_hash(encoded)
    if parsed is None:
        return False
    n, r, p, salt, digest = parsed
    try:
        candidate = hashlib.scrypt(
            password.encode(), salt=salt, n=n, r=r, p=p, dklen=len(digest),
            maxmem=256 * 1024 * 1024,
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(candidate, digest)


# -- authenticator codes ------------------------------------------------------


def new_totp_secret() -> str:
    """A fresh base32 secret to load into an authenticator app."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_secret_ok(secret: str) -> bool:
    return _secret_bytes(secret) is not None


def _secret_bytes(secret: str) -> bytes | None:
    cleaned = (secret or "").replace(" ", "").upper()
    if not cleaned:
        return None
    try:
        raw = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))
    except ValueError:
        return None
    return raw if len(raw) >= 10 else None


def totp_at(secret: str, counter: int) -> str:
    """The code for one 30-second step (RFC 6238 over RFC 4226's HOTP)."""
    key = _secret_bytes(secret)
    if key is None:
        raise ValueError("not a base32 secret")
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def check_totp(secret: str, code: str, now: float, last_counter: int = -1) -> int | None:
    """The step ``code`` belongs to, or None if it matches none in the window.
    A step at or before ``last_counter`` was already used and is refused, so a
    code read over a shoulder can't be replayed in the same half-minute."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != TOTP_DIGITS or _secret_bytes(secret) is None:
        return None
    current = int(now // TOTP_STEP_S)
    for counter in range(current - TOTP_WINDOW, current + TOTP_WINDOW + 1):
        if counter > last_counter and hmac.compare_digest(totp_at(secret, counter), code):
            return counter
    return None


def otpauth_uri(secret: str, label: str = "MeshRadio admin") -> str:
    """What an authenticator app scans (or accepts pasted)."""
    return (
        f"otpauth://totp/{quote(label)}?secret={secret}&issuer=MeshRadio"
        f"&digits={TOTP_DIGITS}&period={TOTP_STEP_S}"
    )


# -- sessions -----------------------------------------------------------------


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def key_fingerprint(password_hash: str, totp_secret: str) -> str:
    """Stamped on each sign-in: changing the password or the two-step secret
    changes it, which signs every browser out."""
    return hashlib.sha256(f"{password_hash}\n{totp_secret}".encode()).hexdigest()[:16]


def csrf_token(session_token: str) -> str:
    """The form token for one sign-in. Derived rather than stored, so it
    dies with the sign-in and needs no table of its own."""
    return hmac.new(session_token.encode(), b"meshradio-admin-csrf", hashlib.sha256).hexdigest()


class LoginThrottle:
    """Failed sign-ins in memory: per address, and across all of them. Five
    from one address inside the lockout window pause that address for the
    rest of it; ``GLOBAL_MAX_FAILURES`` from everywhere pause everyone. A
    restart forgets — the rate limiter and scrypt's cost still bound a
    guesser across one.

    An attempt counts against the limits as it starts (``begin``), not when
    it fails: scrypt runs off the event loop, so a burst of parallel guesses
    would otherwise all pass the check before the first one was counted.
    ``passed`` takes back an attempt that turned out right."""

    GLOBAL = "*"

    def __init__(
        self,
        max_failures: int = MAX_FAILURES,
        window_s: float = LOCKOUT_S,
        global_max: int = GLOBAL_MAX_FAILURES,
    ) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self.global_max = global_max
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, key: str, now: float) -> deque[float]:
        times = self._failures.get(key, deque())
        while times and now - times[0] >= self.window_s:
            times.popleft()
        return times

    def _wait(self, key: str, limit: int, now: float) -> float:
        times = self._recent(key, now)
        if len(times) < limit:
            return 0.0
        return max(0.0, self.window_s - (now - times[-limit]))

    def locked_for(self, ip: str, now: float) -> float:
        """Seconds until ``ip`` may try again; 0 when it may now."""
        return max(
            self._wait(ip, self.max_failures, now),
            self._wait(self.GLOBAL, self.global_max, now),
        )

    def begin(self, ip: str, now: float) -> None:
        """Count an attempt as a failure until it proves otherwise."""
        for key in (ip, self.GLOBAL):
            times = self._recent(key, now)
            times.append(now)
            self._failures[key] = times
        if len(self._failures) > 10_000:
            self._failures = {
                k: v for k, v in self._failures.items() if v and now - v[-1] < self.window_s
            }

    def passed(self, ip: str, now: float) -> None:
        """The attempt ``begin`` counted at ``now`` was right: uncount it.
        The address's earlier failures stand, so a right password doesn't
        buy fresh tries at the code."""
        for key in (ip, self.GLOBAL):
            times = self._failures.get(key)
            if times and now in times:
                times.remove(now)

    def succeed(self, ip: str, now: float) -> None:
        """Fully signed in: the address starts clean (the global count
        keeps everyone else's failures)."""
        self.passed(ip, now)
        self._failures.pop(ip, None)


@dataclass
class PendingSignIn:
    """A right password waiting for its code."""

    created: float
    ip: str
    landing: str
    fingerprint: str
    tries: int = 0


class PendingSignIns:
    """Code pages opened by a right password, keyed by the hash of the step
    cookie. In memory: a restart just sends the operator back to the
    password, and there's never more than a handful."""

    MAX = 100

    def __init__(self, ttl_s: float = STEP_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._pending: dict[str, PendingSignIn] = {}

    def open(self, ip: str, landing: str, fingerprint: str, now: float) -> str:
        """A new step token for the cookie."""
        self._prune(now)
        while len(self._pending) >= self.MAX:
            self._pending.pop(next(iter(self._pending)))
        token = new_session_token()
        self._pending[token_hash(token)] = PendingSignIn(now, ip, landing, fingerprint)
        return token

    def get(self, token: str | None, ip: str, fingerprint: str, now: float
            ) -> PendingSignIn | None:
        """The waiting sign-in for ``token``, if it's still good for this
        address and this password."""
        if not token:
            return None
        key = token_hash(token)
        pending = self._pending.get(key)
        if pending is None:
            return None
        if (
            now - pending.created > self.ttl_s
            or pending.fingerprint != fingerprint
            or pending.ip != ip
            or pending.tries >= STEP_MAX_TRIES
        ):
            self._pending.pop(key, None)
            return None
        return pending

    def close(self, token: str | None) -> None:
        if token:
            self._pending.pop(token_hash(token), None)

    def _prune(self, now: float) -> None:
        self._pending = {
            k: v for k, v in self._pending.items() if now - v.created <= self.ttl_s
        }


@dataclass
class AdminSettings:
    """What the admin page needs beyond the web app's own context.

    ``config`` is the running configuration (for backups, feeds, the cache
    and the config view); ``relay`` the home node's pusher when it runs one.
    Tests build these directly."""

    password_hash: str
    totp_secret: str = ""
    config: Any = None
    relay: Any = None
    throttle: LoginThrottle = field(default_factory=LoginThrottle)
    pending: PendingSignIns = field(default_factory=PendingSignIns)
    last_totp_counter: int = -1

    @property
    def fingerprint(self) -> str:
        return key_fingerprint(self.password_hash, self.totp_secret)
