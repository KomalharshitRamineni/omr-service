"""Settings, read once from environment variables."""

import os
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SITE_ORIGIN = "https://kind-island-00d8a3f03.2.azurestaticapps.net"


def _int(name, default):
    return int(os.environ.get(name, default))


def _homr_cmd():
    # homr runs from each page's folder, so a relative path to it must be made absolute here
    cmd = shlex.split(os.environ.get("HOMR_CMD", "homr"))
    if os.sep in cmd[0]:
        cmd[0] = str(Path(cmd[0]).resolve())
    return cmd


@dataclass(frozen=True)
class Settings:
    homr_cmd: list[str] = field(default_factory=_homr_cmd)
    allowed_origins: list[str] = field(
        default_factory=lambda: [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", SITE_ORIGIN).split(",") if o.strip()]
    )
    max_upload_mb: int = field(default_factory=lambda: _int("MAX_UPLOAD_MB", 20))
    max_pages: int = field(default_factory=lambda: _int("MAX_PAGES", 20))
    max_queued_jobs: int = field(default_factory=lambda: _int("MAX_QUEUED_JOBS", 10))
    result_ttl_s: int = field(default_factory=lambda: _int("RESULT_TTL_SECONDS", 3600))
    page_timeout_s: int = field(default_factory=lambda: _int("PAGE_TIMEOUT_SECONDS", 300))
    render_dpi: int = field(default_factory=lambda: _int("RENDER_DPI", 300))
    work_dir: Path = field(default_factory=lambda: Path(os.environ.get("WORK_DIR", tempfile.gettempdir())) / "omr-jobs")
