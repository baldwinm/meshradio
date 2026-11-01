"""Lint rules the ruff config can't enforce on its own."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_function_is_exempt_from_the_complexity_cap():
    # The C901 backlog was cleared in November 2026; a `noqa` would let a
    # function grow past the cap again without anyone deciding to split it.
    marker = "noqa: " + "C901"
    offenders = [
        f"{path.relative_to(ROOT)}:{n}"
        for folder in ("meshradio", "scripts")
        for path in sorted((ROOT / folder).rglob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if marker in line
    ]
    assert offenders == [], f"split these instead of exempting them: {offenders}"
