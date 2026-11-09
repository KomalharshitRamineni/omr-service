from helpers import make_pdf, run_worker, upload
from music21 import converter, meter, stream

from omr_service import pipeline
from omr_service.join import write_joined

QUAVERS = "".join(
    f"<note><pitch><step>{step}</step><octave>4</octave></pitch><duration>1</duration><type>eighth</type></note>"
    for step in "CDEF"
)


def page(path, time_signature):
    """A one-bar page of four quavers in 2/4; like homr, only the first page states the time signature."""
    time = "<time><beats>2</beats><beat-type>4</beat-type></time>" if time_signature else ""
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><score-partwise version="4.0">'
        '<part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list><part id="P1">'
        f'<measure number="1"><attributes><divisions>2</divisions><key><fifths>0</fifths></key>{time}'
        f"<clef><sign>G</sign><line>2</line></clef></attributes>{QUAVERS}</measure></part></score-partwise>"
    )
    return path


def test_joined_pages_are_beamed(tmp_path):
    pages = [(1, page(tmp_path / "p01.musicxml", True)), (2, page(tmp_path / "p02.musicxml", False))]
    out = tmp_path / "result.musicxml"
    assert write_joined(pages, out) == []
    measures = converter.parse(out).parts[0].getElementsByClass(stream.Measure)
    assert [m.number for m in measures] == [1, 2]
    assert out.read_text().count("<beam ") == 8  # two beamed pairs per bar, begin and end
    assert len(list(converter.parse(out).recurse().getElementsByClass(meter.TimeSignature))) == 1


def test_a_bar_that_cannot_be_beamed_is_kept(tmp_path, monkeypatch):
    def fails(self, *args, **kwargs):  # as music21 does on a bar homr over-filled
        raise meter.MeterException("cannot access qLenPos 6.5 when total duration is 2.0")

    monkeypatch.setattr(stream.Measure, "makeBeams", fails)
    pages = [(1, page(tmp_path / "p01.musicxml", True)), (2, page(tmp_path / "p02.musicxml", False))]
    out = tmp_path / "result.musicxml"
    warnings = write_joined(pages, out)
    assert warnings == ["2 bar(s) hold more beats than the time signature (probably misread); left unbeamed"]
    assert len(converter.parse(out).parts[0].getElementsByClass(stream.Measure)) == 2


def test_a_join_failure_fails_the_job_at_once(client, store, settings, monkeypatch):
    def fails(pages, path):
        raise KeyError("3")  # what music21 raised on 2026-11-04, after all 183 pages had been read

    monkeypatch.setattr(pipeline, "write_joined", fails)
    job_id = upload(client, make_pdf(2)).json()["id"]
    run_worker(store, settings)
    body = client.get(f"/omr/{job_id}").json()
    assert body["status"] == "failed"
    assert body["error"] == "could not join the pages into one score (KeyError)"
    assert store.receive(0) is None  # not left on the queue to be converted again
