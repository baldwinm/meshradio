"""Check that the test count meshradio-architecture.md states matches the
suite, so the docs can't quietly fall behind the code. CI runs it in the
lint job; `--fix` rewrites the count in place.

Run: .venv/bin/python scripts/check_docs.py [--fix]
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "meshradio-architecture.md"
COUNT = re.compile(r"\((\d[\d,]*) tests\)")


def collected(output: str) -> int:
    """The test count from `pytest --collect-only -q`'s closing line."""
    match = re.search(r"^(\d+) tests? collected", output, re.MULTILINE)
    if not match:
        raise ValueError("pytest didn't report a test count")
    return int(match.group(1))


def stated(text: str) -> int:
    """The count the doc gives, as in "(476 tests)"."""
    match = COUNT.search(text)
    if not match:
        raise ValueError(f"no '(N tests)' in {DOC.name}")
    return int(match.group(1).replace(",", ""))


def with_count(text: str, count: int) -> str:
    return COUNT.sub(f"({count} tests)", text, count=1)


def main(argv: list[str]) -> int:
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    actual = collected(run.stdout)
    text = DOC.read_text()
    if stated(text) == actual:
        return 0
    if "--fix" in argv:
        DOC.write_text(with_count(text, actual))
        print(f"{DOC.name}: test count set to {actual}")
        return 0
    print(
        f"{DOC.name} says {stated(text)} tests but the suite has {actual}; "
        "run `python scripts/check_docs.py --fix`"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
