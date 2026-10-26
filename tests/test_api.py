import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import make_pdf, run_worker, upload
from music21 import converter, stream

from omr_service import jobs
from omr_service.app import create_app
from omr_service.storage import LocalStore


def submit(client, pages=2):
    res = upload(client, make_pdf(pages))
    assert res.status_code == 202, res.json()
    return res.json()["id"]


def test_converts_a_two_page_pdf(client, store, settings):
    res = upload(client, make_pdf(2))
    assert res.status_code == 202
    job_id = res.json()["id"]
    assert res.headers["location"] == f"/omr/{job_id}"
    assert client.get(f"/omr/{job_id}").json()["status"] == "queued"
    assert client.get(f"/omr/{job_id}/musicxml").status_code == 409

    run_worker(store, settings)
    body = client.get(f"/omr/{job_id}").json()
    assert body["status"] == "done", body
    assert body["pages_done"] == 2 and body["pages_total"] == 2
    assert body["warnings"] == []

    xml = client.get(f"/omr/{job_id}/musicxml")
    assert xml.status_code == 200
    assert xml.headers["content-type"].startswith("application/vnd.recordare.musicxml+xml")
    assert 'filename="Sonata.musicxml"' in xml.headers["content-disposition"]
    assert b"<creator" not in xml.content and b"<movement-title" not in xml.content  # no music21 defaults
    measures = converter.parse(xml.content, format="musicxml").parts[0].getElementsByClass(stream.Measure)
    assert [m.number for m in measures] == [1, 2]


def test_skips_a_page_homr_cannot_read(client, store, settings, monkeypatch):
    monkeypatch.setenv("FAKE_HOMR_FAIL", "p01")
    job_id = submit(client)
    run_worker(store, settings)
    body = client.get(f"/omr/{job_id}").json()
    assert body["status"] == "done"
    assert body["warnings"] == ["page 1 skipped: no staffs found"]


def test_fails_when_no_page_can_be_read(client, store, settings, monkeypatch):
    monkeypatch.setenv("FAKE_HOMR_FAIL", "p01,p02")
    job_id = submit(client)
    run_worker(store, settings)
    body = client.get(f"/omr/{job_id}").json()
    assert body["status"] == "failed"
    assert body["error"] == "no music found on any page"
    assert client.get(f"/omr/{job_id}/musicxml").status_code == 409


@pytest.mark.parametrize("data, code", [
    (b"hello", 415),                 # not a PDF
    (make_pdf(4), 413),              # over max_pages (3)
    (b"%PDF-1.4 not really", 422),   # unreadable PDF
])
def test_rejects_bad_uploads(client, data, code):
    res = upload(client, data)
    assert res.status_code == code
    assert "error" in res.json()


@pytest.mark.parametrize("job_id", ["nope", "0" * 32, "..%2F..%2Fetc"])
def test_unknown_job(client, job_id):
    assert client.get(f"/omr/{job_id}").status_code == 404
    assert client.get(f"/omr/{job_id}/musicxml").status_code == 404


def test_cors_allows_only_the_site(client):
    preflight = {"Access-Control-Request-Method": "POST"}
    ok = client.options("/omr", headers={"Origin": "https://site.example", **preflight})
    assert ok.headers.get("access-control-allow-origin") == "https://site.example"
    other = client.options("/omr", headers={"Origin": "https://other.example", **preflight})
    assert "access-control-allow-origin" not in other.headers


def test_queue_is_bounded(client):
    submit(client)
    submit(client)
    res = upload(client, make_pdf(1))
    assert res.status_code == 503
    assert res.headers["retry-after"] == "60"


def test_jobs_survive_a_restart(client, settings):
    job_id = submit(client)
    # a new API and worker with nothing in memory, as after a restart or scaling to zero
    fresh_store = LocalStore(settings.store_dir)
    run_worker(fresh_store, settings)
    with TestClient(create_app(settings, fresh_store)) as fresh:
        assert fresh.get(f"/omr/{job_id}").json()["status"] == "done"
        assert fresh.get(f"/omr/{job_id}/musicxml").status_code == 200


def test_job_is_retried_after_the_worker_stops(client, store, settings):
    job_id = submit(client)
    store.receive(0)  # a worker takes the job and stops before finishing; its lease runs out at once
    run_worker(store, settings)
    assert client.get(f"/omr/{job_id}").json()["status"] == "done"


def test_gives_up_after_max_attempts(client, store, settings):
    job_id = submit(client)
    for _ in range(settings.max_attempts):
        store.receive(0)
    run_worker(store, settings)
    body = client.get(f"/omr/{job_id}").json()
    assert body["status"] == "failed"
    assert body["error"] == f"gave up after {settings.max_attempts} attempts"


def test_finished_jobs_expire(client, store, settings):
    job_id = submit(client)
    run_worker(store, settings)
    status = jobs.load(store, job_id)
    status["finished"] = time.time() - 3601
    jobs.save(store, status)
    assert client.get(f"/omr/{job_id}").status_code == 200  # TTL 0 keeps them
    with TestClient(create_app(replace(settings, result_ttl_s=3600), store)) as expiring:
        assert expiring.get(f"/omr/{job_id}").status_code == 404


def test_job_records_an_owner_field(client, store):
    job_id = submit(client)
    assert jobs.load(store, job_id)["owner"] is None  # set once the website has accounts
