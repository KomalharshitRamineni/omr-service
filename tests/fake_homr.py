"""Stand-in for homr in tests: writes a one-bar MusicXML next to the image, like homr does.
Fails for images whose stem is listed in FAKE_HOMR_FAIL (comma-separated, e.g. "p01,p02")."""

import os
import sys
from pathlib import Path

MUSICXML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" "http://www.musicxml.org/dtds/partwise.dtd">
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>1</divisions><key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note>
    </measure>
  </part>
</score-partwise>
"""

image = Path(sys.argv[-1])
if image.stem in os.environ.get("FAKE_HOMR_FAIL", "").split(","):
    sys.exit("no staffs found")
image.with_suffix(".musicxml").write_text(MUSICXML)
