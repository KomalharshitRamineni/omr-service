"""Join per-page MusicXML files into one score (parts matched by position, bars renumbered).

homr reads one page at a time, so each page's bars start again at 1. The first page's parts
are kept; later pages' measures are appended to the part in the same position. A clef, key or
time signature that only repeats the one already in force at a page start is dropped, so the
join does not add symbols the printed score does not have.
"""

from music21 import clef, converter, key, meter, stream


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
    return out, warnings
