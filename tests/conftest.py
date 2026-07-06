import pytest

from meshradio.bus import EventBus
from meshradio.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def bus():
    return EventBus()
