import time

import pytest
from helpers import make_pdf, upload, wait_for
from music21 import converter, stream

from omr_service.jobs import JobStore, QueueFull


def test_converts_a_two_page_pdf(client):
    res = upload(client, make_pdf(2))
    assert res.status_code == 202
    job_id = res.json()["id"]
    assert res.headers["location"] == f"/omr/{job_id}"

    body = wait_for(client, job_id)
    assert body["status"] == "done", body
    assert body["pages_done"] == 2 and body["pages_total"] == 2
    assert body["warnings"] == []

    xml = client.get(f"/omr/{job_id}/musicxml")
    assert xml.status_code == 200
    assert xml.headers["content-type"].startswith("application/vnd.recordare.musicxml+xml")
    assert 'filename="Sonata.musicxml"' in xml.headers["content-disposition"]
    measures = converter.parse(xml.content, format="musicxml").parts[0].getElementsByClass(stream.Measure)
    assert [m.number for m in measures] == [1, 2]


def test_skips_a_page_homr_cannot_read(client, monkeypatch):
    monkeypatch.setenv("FAKE_HOMR_FAIL", "p01")
    body = wait_for(client, upload(client, make_pdf(2)).json()["id"])
    assert body["status"] == "done"
    assert body["warnings"] == ["page 1 skipped: no staffs found"]


def test_fails_when_no_page_can_be_read(client, monkeypatch):
    monkeypatch.setenv("FAKE_HOMR_FAIL", "p01,p02")
    job_id = upload(client, make_pdf(2)).json()["id"]
    body = wait_for(client, job_id)
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


def test_unknown_job(client):
    assert client.get("/omr/nope").status_code == 404
    assert client.get("/omr/nope/musicxml").status_code == 404


def test_cors_allows_only_the_site(client):
    preflight = {"Access-Control-Request-Method": "POST"}
    ok = client.options("/omr", headers={"Origin": "https://site.example", **preflight})
    assert ok.headers.get("access-control-allow-origin") == "https://site.example"
    other = client.options("/omr", headers={"Origin": "https://other.example", **preflight})
    assert "access-control-allow-origin" not in other.headers


def test_queue_is_bounded(settings):
    store = JobStore(settings)  # worker not started, so jobs stay queued
    store.submit("a", "a", 1)
    store.submit("b", "b", 1)
    with pytest.raises(QueueFull):
        store.submit("c", "c", 1)


def test_finished_jobs_expire(settings):
    store = JobStore(settings)
    job = store.submit("a", "a", 1)
    job.status, job.finished = "done", time.time() - settings.result_ttl_s - 1
    assert store.get("a") is None
