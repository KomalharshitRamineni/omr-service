import os
import sys
from dataclasses import replace

from helpers import make_pdf, run_worker, upload
from music21 import converter, stream

from omr_service.config import Settings, available_cpus
from omr_service.pipeline import run_homr


def test_pages_converted_at_once_keep_their_order(client, store, settings, monkeypatch):
    monkeypatch.setenv("FAKE_HOMR_FAIL", "p02")
    job_id = upload(client, make_pdf(3)).json()["id"]
    run_worker(store, replace(settings, page_workers=2))
    body = client.get(f"/omr/{job_id}").json()
    assert body["status"] == "done", body
    assert body["pages_done"] == 3 and body["pages_total"] == 3
    assert body["warnings"] == ["page 2 skipped: no staffs found"]
    xml = client.get(f"/omr/{job_id}/musicxml").content
    measures = converter.parse(xml, format="musicxml").parts[0].getElementsByClass(stream.Measure)
    assert [m.number for m in measures] == [1, 2]


def test_available_cpus_reads_the_container_limit(tmp_path):
    proc = tmp_path / "proc-cgroup"
    proc.write_text("0::/\n")
    (tmp_path / "cpu.max").write_text("400000 100000\n")  # v2, inside a container
    assert available_cpus(tmp_path, proc) == 4
    (tmp_path / "cpu.max").write_text("150000 100000\n")
    assert available_cpus(tmp_path, proc) == 2
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert available_cpus(tmp_path, proc) == len(os.sched_getaffinity(0))


def test_available_cpus_finds_the_limit_of_its_own_cgroup(tmp_path):
    scope = tmp_path / "user.slice" / "run-1.scope"
    scope.mkdir(parents=True)
    (scope / "cpu.max").write_text("200000 100000\n")
    (tmp_path / "user.slice" / "cpu.max").write_text("max 100000\n")
    proc = tmp_path / "proc-cgroup"
    proc.write_text("0::/user.slice/run-1.scope\n")
    assert available_cpus(tmp_path, proc) == 2


def test_available_cpus_reads_cgroup_v1(tmp_path):
    (tmp_path / "cpu").mkdir()
    (tmp_path / "cpu" / "cpu.cfs_quota_us").write_text("400000\n")
    (tmp_path / "cpu" / "cpu.cfs_period_us").write_text("100000\n")
    assert available_cpus(tmp_path, tmp_path / "missing") == 4


def test_homr_threads_default_to_cpus_per_page_worker(monkeypatch):
    monkeypatch.setattr("omr_service.config.available_cpus", lambda: 4)
    monkeypatch.setenv("PAGE_WORKERS", "2")
    monkeypatch.delenv("HOMR_THREADS", raising=False)
    assert Settings().homr_threads == 2
    monkeypatch.setenv("HOMR_THREADS", "3")
    assert Settings().homr_threads == 3


def test_homr_runs_through_the_runner_by_default(monkeypatch):
    monkeypatch.delenv("HOMR_CMD", raising=False)
    assert Settings().homr_cmd == [sys.executable, "-m", "omr_service.homr_runner"]


def test_homr_gets_its_thread_count(tmp_path):
    png = tmp_path / "p01.png"
    png.write_bytes(b"")
    writes_env = [sys.executable, "-c",
                  "import os, sys, pathlib; pathlib.Path(sys.argv[-1]).with_suffix('.musicxml')"
                  ".write_text(os.environ['HOMR_ORT_THREADS'])"]
    assert run_homr(png, writes_env, timeout=30, threads=3).read_text() == "3"
