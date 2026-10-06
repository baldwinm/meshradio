"""Who may use the admin page, and how that's checked.

One operator, one password. The password is never stored: the server holds a
scrypt hash of it (``MESHRADIO_ADMIN_PASSWORD_HASH``, printed by
``meshradio --hash-admin-password``), and without one the admin page doesn't
exist at all. Two-step sign-in adds a time-based code from an authenticator
app (RFC 6238, ``MESHRADIO_ADMIN_TOTP_SECRET``). Everything here is the
standard library: no new dependency for the appliance to carry.

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

# A sign-in lasts at most this long, and ends sooner after this much idle.
SESSION_MAX_S = 12 * 3600
SESSION_IDLE_S = 30 * 60

# Wrong passwords (or codes) from one address before sign-in pauses for it.
MAX_FAILURES = 5
LOCKOUT_S = 15 * 60

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
    """Failed sign-ins per address, in memory. Five inside the lockout window
    pause that address for the rest of it. A restart forgets — the rate
    limiter and scrypt's cost still bound a guesser across one."""

    def __init__(self, max_failures: int = MAX_FAILURES, window_s: float = LOCKOUT_S) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, ip: str, now: float) -> deque[float]:
        times = self._failures.get(ip, deque())
        while times and now - times[0] >= self.window_s:
            times.popleft()
        return times

    def locked_for(self, ip: str, now: float) -> float:
        """Seconds until ``ip`` may try again; 0 when it may now."""
        times = self._recent(ip, now)
        if len(times) < self.max_failures:
            return 0.0
        return max(0.0, self.window_s - (now - times[-self.max_failures]))

    def fail(self, ip: str, now: float) -> None:
        times = self._recent(ip, now)
        times.append(now)
        self._failures[ip] = times
        if len(self._failures) > 10_000:
            self._failures = {
                k: v for k, v in self._failures.items() if v and now - v[-1] < self.window_s
            }

    def succeed(self, ip: str) -> None:
        self._failures.pop(ip, None)


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
    last_totp_counter: int = -1

    @property
    def fingerprint(self) -> str:
        return key_fingerprint(self.password_hash, self.totp_secret)
