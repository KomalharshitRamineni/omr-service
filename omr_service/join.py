"""Join per-page MusicXML files into one score (parts matched by position, bars renumbered).

homr reads one page at a time, so each page's bars start again at 1. The first page's parts
are kept; later pages' measures are appended to the part in the same position. A clef, key or
time signature that only repeats the one already in force at a page start is dropped, so the
join does not add symbols the printed score does not have.

write_joined() writes the joined score without music21's own clean-up at write time. That clean-up
ties a note that runs past its bar line into the next bar and beams whole parts at once; on a bar
that homr over-filled (more beats than the time signature) it adds notes that are not in the score,
or stops with an error (MeterException, KeyError), and the job fails after every page has been read.
Beams and tuplet brackets are made bar by bar instead, and a bar that cannot be beamed is left as read.
"""

from music21 import clef, converter, defaults, key, metadata, meter, stream
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
        for target, source in zip(parts, page_parts):
            measures = list(source.getElementsByClass(stream.Measure))
            if not measures:
                continue
            first = measures[0]
            for cls in (clef.Clef, key.KeySignature, meter.TimeSignature):
                current = target.recurse().getElementsByClass(cls).last()
                for obj in list(first.recurse().getElementsByClass(cls)):
                    if obj.offset == 0 and _same(obj, current):
                        obj.activeSite.remove(obj)
            for m in measures:
                target.append(m)
    for part in parts:
        for number, m in enumerate(part.getElementsByClass(stream.Measure), 1):
            m.number = number
    out.metadata = metadata.Metadata()
    return out, warnings


def _notate(score):
    """Accidentals, beams and tuplet brackets, bar by bar. Returns the number of bars left unbeamed."""
    unbeamed = 0
    for part in score.parts:
        part.makeAccidentals(inPlace=True)
        for m in part.getElementsByClass(stream.Measure):
            try:
                m.makeBeams(inPlace=True)
            except Exception:  # an over-filled bar: keep it as homr read it
                unbeamed += 1
            for container in [m, *m.voices]:
                makeNotation.makeTupletBrackets(container, inPlace=True)
    return unbeamed


def write_joined(pages, path):
    """Joins the pages and writes one MusicXML file to path. Returns warnings."""
    score, warnings = join(pages)
    unbeamed = _notate(score)
    score.write("musicxml", fp=str(path), makeNotation=False)
    if unbeamed:
        warnings.append(f"{unbeamed} bar(s) hold more beats than the time signature (probably misread); left unbeamed")
    return warnings
