import re

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


def homr071_piano_page(path, time, printed, bars):
    """A two-staff piano page as homr 0.7.1 writes it: one <part> with <staves>2</staves>, which music21 reads as
    two staves (PartStaff). Each bar is (treble steps, bass steps) in quavers; ("", "") is an empty bar."""
    time_xml = f"<time><beats>{time[0]}</beats><beat-type>{time[1]}</beat-type></time>"
    first = f"<attributes><divisions>2</divisions><staves>2</staves>{'' if printed else time_xml}</attributes>" \
            "<attributes><clef number=\"1\"><sign>G</sign><line>2</line></clef>" \
            "<clef number=\"2\"><sign>F</sign><line>4</line></clef><key><fifths>0</fifths></key>" \
            f"{time_xml if printed else ''}</attributes>"

    def staff(steps, number):
        return "".join(f"<note><pitch><step>{s}</step><octave>{4 if number == 1 else 3}</octave></pitch>"
                       f"<duration>1</duration><type>eighth</type><staff>{number}</staff></note>" for s in steps)

    measures = "".join(f'<measure number="{i}">{first if i == 1 else ""}{staff(top, 1)}'
                       f'{f"<backup><duration>{len(top)}</duration></backup>" if top else ""}{staff(bottom, 2)}</measure>'
                       for i, (top, bottom) in enumerate(bars, 1))
    path.write_text('<?xml version="1.0" encoding="UTF-8"?><score-partwise version="4.0">'
                    '<part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>'
                    f'<part id="P1">{measures}</part></score-partwise>')
    return path


def time_signatures_by_staff(path):
    return [[t.ratioString for t in part.recurse().getElementsByClass(meter.TimeSignature)]
            for part in converter.parse(path).parts]


def test_an_estimated_time_signature_on_a_later_piano_page_is_dropped_from_both_staves(tmp_path):
    # 2026-11-23: homr's guess for page 2 was dropped from the treble staff only, so the bass staff changed
    # metre on its own and the change was written partway through page 2's first bar
    pages = [(1, homr071_piano_page(tmp_path / "p01.musicxml", (4, 4), True, [("CDEFGABC", "CDEFGABC")])),
             (2, homr071_piano_page(tmp_path / "p02.musicxml", (3, 4), False, [("CDEFGA", "CDEFGA")]))]
    out = tmp_path / "result.musicxml"
    write_joined(pages, out)
    assert time_signatures_by_staff(out) == [["4/4"], ["4/4"]]
    assert out.read_text().count("<time") == 1


def test_a_printed_time_signature_change_on_a_later_piano_page_starts_the_bar_in_both_staves(tmp_path):
    pages = [(1, homr071_piano_page(tmp_path / "p01.musicxml", (4, 4), True, [("CDEFGABC", "CDEFGABC")])),
             (2, homr071_piano_page(tmp_path / "p02.musicxml", (3, 4), True, [("CDEFGA", "CDEFGA")]))]
    out = tmp_path / "result.musicxml"
    write_joined(pages, out)
    assert time_signatures_by_staff(out) == [["4/4", "3/4"], ["4/4", "3/4"]]
    second_bar = re.split(r'<measure[^>]* number="2"[^>]*>', out.read_text())[1].split("</measure>")[0]
    assert second_bar.index("<time") < second_bar.index("<note")  # at the start of the bar, before any note


def homr071_song_page(path, time, printed, bars):
    """A voice and piano page as homr 0.7.1 writes it: the voice is one <part> on one staff, the piano a second
    <part> on two staves, so music21 reads three staves. Each bar is the quaver steps of all three staves."""
    time_xml = f"<time><beats>{time[0]}</beats><beat-type>{time[1]}</beat-type></time>"
    est = "" if printed else time_xml
    shown = time_xml if printed else ""

    def notes(steps, staff=None):
        tag = f"<staff>{staff}</staff>" if staff else ""
        return "".join(f"<note><pitch><step>{s}</step><octave>4</octave></pitch><duration>1</duration>"
                       f"<type>eighth</type>{tag}</note>" for s in steps)

    voice_first = f"<attributes><divisions>2</divisions>{est}</attributes>" \
                  f"<attributes><clef><sign>G</sign><line>2</line></clef><key><fifths>0</fifths></key>{shown}</attributes>"
    piano_first = f"<attributes><divisions>2</divisions><staves>2</staves>{est}</attributes>" \
                  "<attributes><clef number=\"1\"><sign>G</sign><line>2</line></clef>" \
                  f"<clef number=\"2\"><sign>F</sign><line>4</line></clef><key><fifths>0</fifths></key>{shown}</attributes>"
    voice = "".join(f'<measure number="{i}">{voice_first if i == 1 else ""}{notes(steps)}</measure>'
                    for i, steps in enumerate(bars, 1))
    piano = "".join(f'<measure number="{i}">{piano_first if i == 1 else ""}{notes(steps, 1)}'
                    f'<backup><duration>{len(steps)}</duration></backup>{notes(steps, 2)}</measure>'
                    for i, steps in enumerate(bars, 1))
    path.write_text('<?xml version="1.0" encoding="UTF-8"?><score-partwise version="4.0"><part-list>'
                    '<score-part id="P1"><part-name>Voice</part-name></score-part>'
                    '<score-part id="P2"><part-name>Piano</part-name></score-part></part-list>'
                    f'<part id="P1">{voice}</part><part id="P2">{piano}</part></score-partwise>')
    return path


def test_an_estimated_time_signature_on_a_later_song_page_is_dropped_from_all_three_staves(tmp_path):
    # 2026-11-27, MUSI1031 songs: D. 808 changed to homr's 3/4 partway through bar 13 and "Mignon" to 4/4 in bar 19
    pages = [(1, homr071_song_page(tmp_path / "p01.musicxml", (6, 8), True, ["CDEFGA"])),
             (2, homr071_song_page(tmp_path / "p02.musicxml", (3, 4), False, ["CDEFGA"]))]
    out = tmp_path / "result.musicxml"
    assert write_joined(pages, out) == ([], [])
    assert time_signatures_by_staff(out) == [["6/8"], ["6/8"], ["6/8"]]


def test_an_empty_first_bar_is_dropped_and_its_clefs_key_and_time_move_on(tmp_path):
    # 2026-11-23, Joplin page: homr wrote a bar holding only clef, key and time before the bar with the repeat sign
    pages = [(1, homr071_piano_page(tmp_path / "p01.musicxml", (2, 4), True, [("", ""), ("CDEF", "CDEF")])),
             (2, homr071_piano_page(tmp_path / "p02.musicxml", (2, 4), False, [("", ""), ("GABC", "GABC")]))]
    out = tmp_path / "result.musicxml"
    assert write_joined(pages, out) == ([], [])
    score = converter.parse(out)
    for part in score.parts:
        bars = list(part.getElementsByClass(stream.Measure))
        assert [m.number for m in bars] == [1, 2]
        assert all(len(list(m.recurse().notes)) == 4 for m in bars)
        assert bars[0].timeSignature.ratioString == "2/4" and bars[0].clef is not None
    assert [p.getElementsByClass(stream.Measure).first().clef.sign for p in score.parts] == ["G", "F"]


def test_a_first_bar_with_music_in_one_staff_is_kept(tmp_path):
    pages = [(1, homr071_piano_page(tmp_path / "p01.musicxml", (2, 4), True, [("CDEF", ""), ("CDEF", "CDEF")]))]
    out = tmp_path / "result.musicxml"
    write_joined(pages, out)
    assert len(converter.parse(out).parts[0].getElementsByClass(stream.Measure)) == 2


def test_a_guessed_time_signature_on_the_first_page_is_fitted_to_the_bars(tmp_path):
    # 2026-11-27, Polish scans: homr guessed 1/4 for bars of 1.5 and 3 beats, so every bar was listed to check
    bars = [("CDEFGA", "CDEFGA")] * 4 + [("CDE", "CDE")] * 4
    pages = [(1, homr071_piano_page(tmp_path / "p01.musicxml", (1, 4), False, bars)),
             (2, homr071_piano_page(tmp_path / "p02.musicxml", (3, 8), False, bars))]
    out = tmp_path / "result.musicxml"
    warnings, check = write_joined(pages, out)
    assert time_signatures_by_staff(out) == [["3/4"], ["3/4"]]
    assert check == []
    assert warnings == ["no time signature is printed at the start, so 3/4 is a guess: check it first"]


def test_a_guessed_time_signature_that_fits_is_kept(tmp_path):
    pages = [(1, homr071_piano_page(tmp_path / "p01.musicxml", (6, 8), False, [("CDEFGA", "CDEFGA")] * 3))]
    out = tmp_path / "result.musicxml"
    warnings, check = write_joined(pages, out)
    assert time_signatures_by_staff(out) == [["6/8"], ["6/8"]]  # same bar length as 3/4, so homr's 6/8 stays
    assert warnings == ["no time signature is printed at the start, so 6/8 is a guess: check it first"]


def test_bars_that_do_not_fit_the_fitted_time_signature_are_still_listed(tmp_path):
    bars = [("CDEFGA", "CDEFGA")] * 9 + [("CDEFGABC", "CDEFGABC")]  # 9 bars of 3 beats, 1 of 4
    pages = [(1, homr071_piano_page(tmp_path / "p01.musicxml", (1, 4), False, bars))]
    out = tmp_path / "result.musicxml"
    warnings, check = write_joined(pages, out)
    assert time_signatures_by_staff(out) == [["3/4"], ["3/4"]]
    assert check == [10]
    assert len(warnings) == 1


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
