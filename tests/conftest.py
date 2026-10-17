import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from broweye.app import BrowEye  # noqa: E402
from broweye.lab import start_lab  # noqa: E402


@pytest.fixture
async def lab():
    lab, runner, base = await start_lab()
    lab.base = base
    yield lab
    await runner.cleanup()


@pytest.fixture
async def app(tmp_path):
    a = BrowEye(tmp_path / "project", headless=True)
    await a.start()
    yield a
    await a.stop()


@pytest.fixture
async def app_nobrowser(tmp_path):
    a = BrowEye(tmp_path / "project", with_browser=False)
    yield a
    await a.stop()
