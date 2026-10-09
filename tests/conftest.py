import os

import pytest

from meshradio.bus import EventBus
from meshradio.db import Database
from meshradio.runtime import clear_errors

# The browser tests need Playwright and Chromium, which the dev install
# doesn't carry; CI runs them in a job of their own (see tests/browser).
collect_ignore = [] if os.environ.get("MESHRADIO_BROWSER_TESTS") else ["browser"]


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture(autouse=True)
def _no_carried_errors():
    """/healthz counts failures process-wide; start each test from none, so
    one test's deliberate crash can't show up in another's count."""
    clear_errors()
    yield
    clear_errors()
