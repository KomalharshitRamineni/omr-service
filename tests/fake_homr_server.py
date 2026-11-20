"""Stand-in for omr_service.homr_server in tests: one image path per line, one JSON reply per image.
Images whose stem is in FAKE_HOMR_FAIL get an error reply, in FAKE_HOMR_HANG never get a reply, and in
FAKE_HOMR_CRASH stop the process (with a message on stderr). Each MusicXML records this process's id."""

import json
import os
import sys
import time
from pathlib import Path

from fake_homr import MUSICXML


def names(var):
    return os.environ.get(var, "").split(",")


for line in sys.stdin:
    image = Path(line.strip())
    if image.stem in names("FAKE_HOMR_CRASH"):
        sys.exit("homr crashed")
    if image.stem in names("FAKE_HOMR_HANG"):
        time.sleep(3600)
    if image.stem in names("FAKE_HOMR_FAIL"):
        reply = {"ok": False, "error": "no staffs found"}
    else:
        image.with_suffix(".musicxml").write_text(MUSICXML.replace("<score-partwise", f"<!-- pid {os.getpid()} -->\n<score-partwise", 1))
        reply = {"ok": True}
    print(json.dumps(reply), flush=True)
