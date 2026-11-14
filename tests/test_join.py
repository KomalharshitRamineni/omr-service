from helpers import make_pdf, run_worker, upload
from music21 import converter, meter, stream

from omr_service import jobs, pipeline
from omr_service.join import write_joined

QUAVER = "<note><pitch><step>{}</step><octave>4</octave></pitch><duration>1</duration><type>eighth</type></note>"
WHOLE_REST = "<note><rest/><duration>8</duration><type>whole</type></note>"  # as homr reads a whole-bar rest


def page(path, time_signature, bars):
    """A page of bars in 2/4 (divisions 2: a quaver is 1); like homr, only the first page states the time signature."""
    time = "<time><beats>2</beats><beat-type>4</beat-type></time>" if time_signature else ""
    attributes = f"<attributes><divisions>2</divisions><key><fifths>0</fifths></key>{time}" \
                 "<clef><sign>G</sign><line>2</line></clef></attributes>"
    measures = "".join(f'<measure number="{i}">{attributes if i == 1 else ""}{bar}</measure>'
                       for i, bar in enumerate(bars, 1))
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><score-partwise version="4.0">'
        '<part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>'
        f'<part id="P1">{measures}</part></score-partwise>'
    )
    return path


def quavers(steps):
    return "".join(QUAVER.format(step) for step in steps)


def test_joined_pages_are_beamed(tmp_path):
    pages = [(1, page(tmp_path / "p01.musicxml", True, [quavers("CDEF")])),
             (2, page(tmp_path / "p02.musicxml", False, [quavers("GABC")]))]
    out = tmp_path / "result.musicxml"
    assert write_joined(pages, out) == ([], [])
    score = converter.parse(out)
    assert [m.number for m in score.parts[0].getElementsByClass(stream.Measure)] == [1, 2]
    assert out.read_text().count("<beam ") == 8  # two beamed pairs per bar, begin and end
    assert len(list(score.recurse().getElementsByClass(meter.TimeSignature))) == 1


def homr071_page(path, time, printed, bars):
    """A page as homr 0.7.1 writes it: a time signature it read comes with the clef and key; one it estimated
    (none printed on the page) comes first, with <divisions>."""
    time_xml = f"<time><beats>{time[0]}</beats><beat-type>{time[1]}</beat-type></time>"
    first = f"<attributes><divisions>2</divisions>{'' if printed else time_xml}</attributes>" \
            f"<attributes><clef><sign>G</sign><line>2</line></clef><key><fifths>0</fifths></key>" \
            f"{time_xml if printed else ''}</attributes>"
    measures = "".join(f'<measure number="{i}">{first if i == 1 else ""}{bar}</measure>' for i, bar in enumerate(bars, 1))
    path.write_text('<?xml version="1.0" encoding="UTF-8"?><score-partwise version="4.0">'
                    '<part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>'
                    f'<part id="P1">{measures}</part></score-partwise>')
    return path


def test_an_estimated_time_signature_on_a_later_page_is_dropped(tmp_path):
    pages = [(1, homr071_page(tmp_path / "p01.musicxml", (6, 8), True, [quavers("CDEFGA")])),
             (2, homr071_page(tmp_path / "p02.musicxml", (3, 4), False, [quavers("GABCDE")]))]
    out = tmp_path / "result.musicxml"
    write_joined(pages, out)
    assert [t.ratioString for t in converter.parse(out).recurse().getElementsByClass(meter.TimeSignature)] == ["6/8"]


def test_a_printed_time_signature_change_on_a_later_page_is_kept(tmp_path):
    pages = [(1, homr071_page(tmp_path / "p01.musicxml", (6, 8), True, [quavers("CDEFGA")])),
             (2, homr071_page(tmp_path / "p02.musicxml", (3, 4), True, [quavers("GABCDE")]))]
    out = tmp_path / "result.musicxml"
    write_joined(pages, out)
    assert [t.ratioString for t in converter.parse(out).recurse().getElementsByClass(meter.TimeSignature)] == ["6/8", "3/4"]


def test_lists_bars_with_more_beats_than_the_time_signature(tmp_path):
    pages = [(1, page(tmp_path / "p01.musicxml", True, [quavers("CDEF"), WHOLE_REST])),
             (2, page(tmp_path / "p02.musicxml", False, [quavers("GABCD"), quavers("EF")]))]
    out = tmp_path / "result.musicxml"
    warnings, check = write_joined(pages, out)
    assert check == [3]  # 5 quavers in 2/4; the whole-bar rest (bar 2) and the short last bar are not listed
    assert len(converter.parse(out).parts[0].getElementsByClass(stream.Measure)) == 4


def test_a_bar_that_cannot_be_beamed_is_kept(tmp_path, monkeypatch):
    def fails(self, *args, **kwargs):  # as music21 does on a bar homr over-filled
        raise meter.MeterException("cannot access qLenPos 6.5 when total duration is 2.0")

    monkeypatch.setattr(stream.Measure, "makeBeams", fails)
    pages = [(1, page(tmp_path / "p01.musicxml", True, [quavers("CDEF")])),
             (2, page(tmp_path / "p02.musicxml", False, [quavers("GABC")]))]
    out = tmp_path / "result.musicxml"
    assert write_joined(pages, out) == ([], [])
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


def test_status_lists_bars_to_check(client, store, settings):
    job_id = upload(client, make_pdf(2)).json()["id"]
    assert client.get(f"/omr/{job_id}").json()["check_bars"] == []
    run_worker(store, settings)
    assert client.get(f"/omr/{job_id}").json()["check_bars"] == []  # the fake homr's bars are full, not over-full


def test_jobs_saved_before_check_bars_still_load(client, store):
    job_id = upload(client, make_pdf(1)).json()["id"]
    status = jobs.load(store, job_id)
    del status["check_bars"]
    jobs.save(store, status)
    assert client.get(f"/omr/{job_id}").json()["check_bars"] == []
