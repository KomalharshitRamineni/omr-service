"""Pages as separate queue messages, joined by whoever finishes the last page; homr kept loaded between pages."""

import sys
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import make_pdf, run_worker, upload

from omr_service import jobs
from omr_service.pipeline import ConversionError, Homr
from omr_service.worker import join_if_complete, process_next

FAKE_SERVER = [sys.executable, str(Path(__file__).with_name("fake_homr_server.py"))]


def submit(client, pages):
    res = upload(client, make_pdf(pages))
    assert res.status_code == 202, res.json()
    return res.json()["id"]


def test_each_page_is_its_own_message(client, store):
    job_id = submit(client, 3)
    texts = []
    while (message := store.receive(60)):
        texts.append(jobs.read_message(message.text))
    assert sorted(texts) == [(job_id, 1), (job_id, 2), (job_id, 3)]


def test_progress_while_pages_are_read(client, store, settings):
    job_id = submit(client, 2)
    assert process_next(store, settings)  # one worker reads one page
    body = client.get(f"/omr/{job_id}").json()
    assert body["status"] == "running" and body["pages_done"] == 1
    assert store.get(jobs.blob(job_id, "join.lock")) is None  # not joined while a page is missing
    assert process_next(store, settings)
    body = client.get(f"/omr/{job_id}").json()
    assert body["status"] == "done" and body["pages_done"] == 2


def test_pages_are_joined_once(client, store, settings):
    job_id = submit(client, 2)
    run_worker(store, settings)
    first = store.get(jobs.blob(job_id, "status.json"))
    status = jobs.load(store, job_id)
    status["status"] = "running"  # as if a second worker finished the last page at the same moment
    jobs.save(store, status)
    join_if_complete(store, settings, job_id)
    assert jobs.load(store, job_id)["status"] == "running"  # the lock stopped a second join
    assert first


def test_a_whole_job_message_is_split_into_pages(client, store, settings):
    job_id = submit(client, 2)
    while (message := store.receive(60)):
        store.delete(message)  # drop the page messages ...
    store.send(job_id)  # ... and queue the job as the previous version did
    run_worker(store, settings)
    assert client.get(f"/omr/{job_id}").json()["status"] == "done"


def test_worker_with_homr_kept_loaded(client, store, settings, monkeypatch):
    monkeypatch.setenv("FAKE_HOMR_FAIL", "p02")
    job_id = submit(client, 3)
    run_worker(store, replace(settings, homr_server_cmd=FAKE_SERVER))
    body = client.get(f"/omr/{job_id}").json()
    assert body["status"] == "done", body
    assert body["warnings"] == ["page 2 skipped: no staffs found"]


@pytest.fixture
def homr(settings):
    pool = Homr(replace(settings, homr_server_cmd=FAKE_SERVER, page_timeout_s=2), slots=1)
    yield pool
    pool.close()


def image(tmp_path, stem):
    path = tmp_path / stem / f"{stem}.png"
    path.parent.mkdir()
    path.write_bytes(b"")
    return path


def pid(xml):
    return xml.read_text().split("pid ")[1].split()[0]


def test_one_homr_process_reads_many_pages(homr, tmp_path):
    first, second = homr.convert(image(tmp_path, "p01")), homr.convert(image(tmp_path, "p02"))
    assert pid(first) == pid(second)


def test_a_page_homr_cannot_read(homr, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOMR_FAIL", "p01")
    with pytest.raises(ConversionError, match="no staffs found"):
        homr.convert(image(tmp_path, "p01"))
    homr.convert(image(tmp_path, "p02"))


def test_a_page_that_hangs_restarts_homr(homr, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOMR_HANG", "p02")
    before = pid(homr.convert(image(tmp_path, "p01")))
    with pytest.raises(ConversionError, match="timed out after 2 s"):
        homr.convert(image(tmp_path, "p02"))
    assert pid(homr.convert(image(tmp_path, "p03"))) != before


def test_a_crash_is_reported_and_homr_restarts(homr, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOMR_CRASH", "p01")
    with pytest.raises(ConversionError, match="homr crashed"):
        homr.convert(image(tmp_path, "p01"))
    assert homr.convert(image(tmp_path, "p02")).exists()


def test_a_slot_survives_an_unexpected_error(store, settings, monkeypatch):
    from omr_service import worker
    calls = []

    def flaky(*args):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("storage unavailable")
        return False  # then the queue is empty

    monkeypatch.setattr(worker, "process_next", flaky)
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    worker.drain(store, settings, None)
    assert len(calls) == 2
