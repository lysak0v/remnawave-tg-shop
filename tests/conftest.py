import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("BOT_TOKEN", "0:test")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("PANEL_API_URL", "https://panel.example/api")
os.environ.setdefault("PANEL_API_KEY", "test-key")

import pytest  # noqa: E402

from config.settings import Settings  # noqa: E402


@pytest.fixture
def settings():
    return Settings()
