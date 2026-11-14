"""Join per-page MusicXML files into one score (parts matched by position, bars renumbered).

homr reads one page at a time, so each page's bars start again at 1. The first page's parts
are kept; later pages' measures are appended to the part in the same position. A clef, key or
time signature that only repeats the one already in force at a page start is dropped, so the
join does not add symbols the printed score does not have. So is a time signature homr estimated for a
later page that prints none (homr 0.7.1 writes one from the page's median bar length, e.g. 3/4 on the
pages after a 6/8 first page): the time signature already in force is kept.

write_joined() writes the joined score without music21's own clean-up at write time. That clean-up
ties a note that runs past its bar line into the next bar and beams whole parts at once; on a bar
that homr over-filled (more beats than the time signature) it adds notes that are not in the score,
or stops with an error (MeterException, KeyError), and the job fails after every page has been read.
Beams and tuplet brackets are made bar by bar instead, and a bar that cannot be beamed is left as read.

check_bars() lists the bars that hold more beats than their time signature, so a teacher knows where to
look first. On three pieces checked against the original Sibelius files (2026-11-09) all 6 bars it listed
were misread; it misses errors that do not over-fill a bar.
"""

import xml.etree.ElementTree as ET

from music21 import clef, converter, defaults, key, metadata, meter, note, stream
from music21.stream import makeNotation

# No made-up header in the output: without these, music21 titles a score after its first page's
# file ("p01.musicxml") or "Music21 Fragment", and names "Music21" as the composer.
defaults.title = ""
defaults.author = ""


def _same(a, b):
    if a is None or b is None:
        return False
    if isinstance(a, clef.Clef):
        return type(a) is type(b) and a.sign == b.sign and a.line == b.line and a.octaveChange == b.octaveChange
    if isinstance(a, key.KeySignature):
        return a.sharps == b.sharps
    if isinstance(a, meter.TimeSignature):
        return a.ratioString == b.ratioString
    return False


def _parse(path):
    score = converter.parse(path)
    if isinstance(score, stream.Opus):
        score = score.scores[0]
    return score


def _estimated_time(path):
    """For each part (in order): did homr estimate the page's opening time signature rather than read it?
    homr 0.7.1 writes a time signature it read with the clef and key, and one it estimated (no time signature
    on the page) in the first <attributes> of the first bar, with <divisions> and without a clef."""
    try:
        parts = ET.parse(path).getroot().findall("part")
    except (ET.ParseError, OSError):
        return []
    estimated = []
    for part in parts:
        bar = part.find("measure")
        first = bar.find("attributes") if bar is not None else None
        estimated.append(first is not None and first.find("divisions") is not None
                         and first.find("time") is not None and first.find("clef") is None)
    return estimated


def join(pages):
    """pages: list of (page_number, path). Returns (music21 score, warnings)."""
    warnings = []
    (first_no, first_path), rest = pages[0], pages[1:]
    out = _parse(first_path)
    parts = list(out.parts)
    for page_no, path in rest:
        page_parts = list(_parse(path).parts)
        if len(page_parts) != len(parts):
            warnings.append(f"page {page_no} has {len(page_parts)} part(s), page {first_no} has {len(parts)}")
        estimated = _estimated_time(path)
        for index, (target, source) in enumerate(zip(parts, page_parts)):
            measures = list(source.getElementsByClass(stream.Measure))
            if not measures:
                continue
            first = measures[0]
            for cls in (clef.Clef, key.KeySignature, meter.TimeSignature):
                current = target.recurse().getElementsByClass(cls).last()
                guessed = cls is meter.TimeSignature and current is not None \
                    and index < len(estimated) and estimated[index]
                for obj in list(first.recurse().getElementsByClass(cls)):
                    if obj.offset == 0 and (_same(obj, current) or guessed):
                        obj.activeSite.remove(obj)
            for m in measures:
                target.append(m)
    for part in parts:
        for number, m in enumerate(part.getElementsByClass(stream.Measure), 1):
            m.number = number
    out.metadata = metadata.Metadata()
    return out, warnings


def _notate(score):
    """Accidentals, beams and tuplet brackets, bar by bar."""
    for part in score.parts:
        part.makeAccidentals(inPlace=True)
        for m in part.getElementsByClass(stream.Measure):
            try:
                m.makeBeams(inPlace=True)
            except Exception:  # an over-filled bar: keep it as homr read it
                pass
            for container in [m, *m.voices]:
                makeNotation.makeTupletBrackets(container, inPlace=True)


def check_bars(score):
    """Numbers of the bars that hold more beats than their time signature in any part. A bar holding only
    one rest is skipped: a whole-bar rest is drawn the same in every metre, and homr reads it as 4 beats."""
    bars = set()
    for part in score.parts:
        for m in part.getElementsByClass(stream.Measure):
            items = list(m.recurse().notesAndRests)
            if len(items) == 1 and isinstance(items[0], note.Rest):
                continue
            time = m.timeSignature or m.getContextByClass(meter.TimeSignature)
            if time and m.highestTime > time.barDuration.quarterLength + 1e-6:
                bars.add(m.number)
    return sorted(bars)


def write_joined(pages, path):
    """Joins the pages and writes one MusicXML file to path. Returns (warnings, bars to check)."""
    score, warnings = join(pages)
    _notate(score)
    bars = check_bars(score)
    score.write("musicxml", fp=str(path), makeNotation=False)
    return warnings, bars
