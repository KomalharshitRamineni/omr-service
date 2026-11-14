"""Settings, read once from environment variables."""

import math
import os
import shlex
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SITE_ORIGIN = "https://kind-island-00d8a3f03.2.azurestaticapps.net"


def _int(name, default):
    return int(os.environ.get(name, default))


def _homr_cmd():
    # homr runs from each page's folder, so a relative path to it must be made absolute here.
    # The default runs homr through homr_runner, which sets its ONNX Runtime thread count.
    if "HOMR_CMD" not in os.environ:
        return [sys.executable, "-m", "omr_service.homr_runner"]
    cmd = shlex.split(os.environ["HOMR_CMD"])
    if os.sep in cmd[0]:
        cmd[0] = str(Path(cmd[0]).resolve())
    return cmd


def _quota(path, period_path=None):
    """CPUs allowed by one cgroup file, or None: v2 cpu.max ("400000 100000" = 4, "max 100000" = none)
    or v1 cpu.cfs_quota_us (-1 = none) with cpu.cfs_period_us."""
    try:
        if period_path is None:
            quota, period = path.read_text().split()
        else:
            quota, period = path.read_text().strip(), period_path.read_text().strip()
        if quota in ("max", "-1"):
            return None
        return int(quota) / int(period)
    except (OSError, ValueError):
        return None


def available_cpus(root=Path("/sys/fs/cgroup"), proc_cgroup=Path("/proc/self/cgroup")):
    """CPUs this process may use: the tightest CPU limit of its cgroup (a container's vCPU limit) and the
    cgroups above it, if any; otherwise the CPUs it may run on. In a container /sys/fs/cgroup is usually the
    container's own cgroup; outside one, the process's cgroup path comes from /proc/self/cgroup."""
    limits = [_quota(root / "cpu.max"), _quota(root / "cpu/cpu.cfs_quota_us", root / "cpu/cpu.cfs_period_us")]
    try:
        for line in proc_cgroup.read_text().splitlines():
            if line.startswith("0::"):  # cgroup v2: check the process's cgroup and every parent
                path = root / line[3:].strip().lstrip("/")
                while path != root and root in path.parents:
                    limits.append(_quota(path / "cpu.max"))
                    path = path.parent
    except OSError:
        pass
    limits = [limit for limit in limits if limit]
    if limits:
        return max(1, math.ceil(min(limits)))
    return len(os.sched_getaffinity(0))


def _homr_threads():
    threads = _int("HOMR_THREADS", 0)
    if threads > 0:
        return threads
    return max(1, available_cpus() // max(1, _int("PAGE_WORKERS", 1)))


@dataclass(frozen=True)
class Settings:
    homr_cmd: list[str] = field(default_factory=_homr_cmd)
    allowed_origins: list[str] = field(
        default_factory=lambda: [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", SITE_ORIGIN).split(",") if o.strip()]
    )
    # generous defaults while testing; tighten them later through the environment
    max_upload_mb: int = field(default_factory=lambda: _int("MAX_UPLOAD_MB", 100))
    max_pages: int = field(default_factory=lambda: _int("MAX_PAGES", 200))
    max_queued_jobs: int = field(default_factory=lambda: _int("MAX_QUEUED_JOBS", 100))
    # 0 keeps finished jobs
    result_ttl_s: int = field(default_factory=lambda: _int("RESULT_TTL_SECONDS", 0))
    page_timeout_s: int = field(default_factory=lambda: _int("PAGE_TIMEOUT_SECONDS", 900))
    retry_after_s: int = field(default_factory=lambda: _int("RETRY_AFTER_SECONDS", 60))
    # deliveries of a job's message before the worker gives up on it
    max_attempts: int = field(default_factory=lambda: _int("MAX_ATTEMPTS", 3))
    render_dpi: int = field(default_factory=lambda: _int("RENDER_DPI", 300))
    # pages converted at the same time, each by its own homr process
    page_workers: int = field(default_factory=lambda: max(1, _int("PAGE_WORKERS", 1)))
    # ONNX Runtime threads per homr process; by default the CPUs available divided by PAGE_WORKERS
    homr_threads: int = field(default_factory=_homr_threads)
    # temporary files while converting
    work_dir: Path = field(default_factory=lambda: Path(os.environ.get("WORK_DIR", tempfile.gettempdir())) / "omr-jobs")
    # jobs live in this Azure Storage account; if it is empty, in a local folder instead
    storage_account: str = field(default_factory=lambda: os.environ.get("STORAGE_ACCOUNT", ""))
    blob_container: str = field(default_factory=lambda: os.environ.get("BLOB_CONTAINER", "jobs"))
    queue_name: str = field(default_factory=lambda: os.environ.get("QUEUE_NAME", "omr-jobs"))
    store_dir: Path = field(default_factory=lambda: Path(os.environ.get("STORE_DIR", Path(tempfile.gettempdir()) / "omr-store")))
