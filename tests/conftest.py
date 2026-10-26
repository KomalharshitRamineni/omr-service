import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omr_service.app import create_app
from omr_service.config import Settings
from omr_service.storage import LocalStore

FAKE_HOMR = [sys.executable, str(Path(__file__).with_name("fake_homr.py"))]


@pytest.fixture
def settings(tmp_path):
    return Settings(homr_cmd=FAKE_HOMR, allowed_origins=["https://site.example"], max_pages=3,
                    max_queued_jobs=2, work_dir=tmp_path / "work", store_dir=tmp_path / "store",
                    storage_account="", render_dpi=20)


@pytest.fixture
def store(settings):
    return LocalStore(settings.store_dir)


@pytest.fixture
def client(settings, store):
    with TestClient(create_app(settings, store)) as c:
        yield c
