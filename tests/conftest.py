import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omr_service.app import create_app
from omr_service.config import Settings

FAKE_HOMR = [sys.executable, str(Path(__file__).with_name("fake_homr.py"))]


@pytest.fixture
def settings(tmp_path):
    return Settings(homr_cmd=FAKE_HOMR, allowed_origins=["https://site.example"], max_pages=3,
                    max_queued_jobs=2, work_dir=tmp_path / "jobs", render_dpi=20)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c
