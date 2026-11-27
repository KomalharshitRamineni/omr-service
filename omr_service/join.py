"""Join per-page MusicXML files into one score (parts matched by position, bars renumbered).

homr reads one page at a time, so each page's bars start again at 1. The first page's parts
are kept; later pages' measures are appended to the part in the same position. A clef, key or
time signature that only repeats the one already in force at a page start is dropped, so the
join does not add symbols the printed score does not have. So is a time signature homr estimated for a
later page that prints none (homr 0.7.1 writes one from the page's median bar length, e.g. 3/4 on the
pages after a 6/8 first page): the time signature already in force is kept.

If the first page prints no time signature either, homr's estimate can be far off (1/4 on two old Polish
scans whose bars hold 1.5 or 3 beats, 2026-11-27, so every bar was listed to check). The join then writes the
shortest usual time signature that at least FIT_SHARE of the bars fit in (3/4 there), keeps homr's estimate if
it has the same bar length, and warns that the time signature is a guess.

A page whose first bar holds no notes or rests in any staff loses that bar, and its clef, key and time
signature move to the next bar: homr writes such a bar before a bar that opens with a repeat sign, and the
printed page has no bar there (Joplin page, 2026-11-23).

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

# Time signatures the join may write when the first page prints none, shortest bar first. One that leaves
# more than 1 bar in 5 over-full is not used (2026-11-27, Polish scans: 3/4 leaves 6 of 52 bars over-full,
# 2/4 and 3/8 about half).
USUAL_TIMES = ("3/8", "2/4", "3/4", "4/4", "5/4", "6/4")
FIT_SHARE = 0.8


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
    """For each staff (in the order music21 lists them): did homr estimate the page's opening time signature
    rather than read it? homr 0.7.1 writes a time signature it read with the clef and key, and one it estimated
    (no time signature on the page) in the first <attributes> of the first bar, with <divisions> and without a
    clef. music21 reads a <part> with <staves>2</staves> (piano) as two staves, so the part's answer is repeated
    for each of its staves; with one answer per part, the bass staff kept homr's estimate (2026-11-23)."""
    try:
        parts = ET.parse(path).getroot().findall("part")
    except (ET.ParseError, OSError):
        return []
    estimated = []
    for part in parts:
        bar = part.find("measure")
        first = bar.find("attributes") if bar is not None else None
        guessed = (first is not None and first.find("divisions") is not None
                   and first.find("time") is not None and first.find("clef") is None)
        staves = bar.findtext("attributes/staves") if bar is not None else None
        estimated.extend([guessed] * (int(staves) if staves and staves.strip().isdigit() else 1))
    return estimated


def _first_bar_empty(path):
    """Does the page's first bar hold no notes or rests in any part? Read from homr's file, because music21 fills
    an empty bar with a whole-bar rest when it reads it."""
    try:
        parts = ET.parse(path).getroot().findall("part")
    except (ET.ParseError, OSError):
        return False
    bars = [part.find("measure") for part in parts]
    return bool(bars) and all(bar is not None and bar.find("note") is None for bar in bars)


def _drop_empty_first_bar(parts, path):
    """Removes a page's first bar if homr wrote no notes or rests in it; its clef, key and time signature move to
    the next bar unless that bar has its own."""
    bars = [list(part.getElementsByClass(stream.Measure)) for part in parts]
    if not bars or any(len(b) < 2 for b in bars) or not _first_bar_empty(path):
        return
    for part, (empty, following) in zip(parts, (b[:2] for b in bars)):
        for cls in (clef.Clef, key.KeySignature, meter.TimeSignature):
            if any(o.offset == 0 for o in following.recurse().getElementsByClass(cls)):
                continue
            for obj in list(empty.recurse().getElementsByClass(cls)):
                if obj.offset == 0:
                    obj.activeSite.remove(obj)
                    following.insert(0, obj)
        part.remove(empty, shiftOffsets=True)


def _fitted_time(parts):
    """The shortest of USUAL_TIMES that at least FIT_SHARE of the bars under the opening time signature fit in
    (bars up to the first change; bars holding a single rest are skipped), or None."""
    lengths = []
    for part in parts:
        for number, m in enumerate(part.getElementsByClass(stream.Measure)):
            if number and m.timeSignature is not None:
                break
            items = list(m.recurse().notesAndRests)
            if items and not (len(items) == 1 and isinstance(items[0], note.Rest)):
                lengths.append(m.highestTime)
    for ratio in USUAL_TIMES:
        bar = meter.TimeSignature(ratio).barDuration.quarterLength
        if lengths and sum(length <= bar + 1e-6 for length in lengths) >= FIT_SHARE * len(lengths):
            return ratio
    return None


def _replace_guessed_time(parts, warnings):
    """The first page printed no time signature: write one that fits the bars (see _fitted_time) and warn."""
    firsts = (part.getElementsByClass(stream.Measure).first() for part in parts)
    opening = [m.timeSignature for m in firsts if m is not None]  # an empty Measure is falsy
    guess = next((t.ratioString for t in opening if t is not None), None)
    fitted = _fitted_time(parts)
    if fitted and (guess is None or meter.TimeSignature(guess).barDuration.quarterLength
                   != meter.TimeSignature(fitted).barDuration.quarterLength):
        for part in parts:
            first = part.getElementsByClass(stream.Measure).first()
            if first is None:
                continue
            if first.timeSignature is not None:
                first.replace(first.timeSignature, meter.TimeSignature(fitted))
            else:
                first.insert(0, meter.TimeSignature(fitted))
        guess = fitted
    if guess:
        warnings.append(f"no time signature is printed at the start, so {guess} is a guess: check it first")


def join(pages):
    """pages: list of (page_number, path). Returns (music21 score, warnings)."""
    warnings = []
    (first_no, first_path), rest = pages[0], pages[1:]
    out = _parse(first_path)
    parts = list(out.parts)
    _drop_empty_first_bar(parts, first_path)
    first_guessed = any(_estimated_time(first_path))
    for page_no, path in rest:
        page_parts = list(_parse(path).parts)
        _drop_empty_first_bar(page_parts, path)
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
    if first_guessed:
        _replace_guessed_time(parts, warnings)
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
