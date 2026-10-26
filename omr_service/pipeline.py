"""PDF -> MusicXML: render each page with pdftoppm, run homr on it, join the pages."""

import os
import subprocess
from pathlib import Path

from .join import join


class ConversionError(Exception):
    pass


def page_count(pdf):
    proc = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True)
    for line in proc.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[-1])
    raise ConversionError("not a readable PDF")


def render_page(pdf, page, out_png, dpi):
    subprocess.run(
        ["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(dpi), "-gray", "-png", "-singlefile",
         str(pdf), str(out_png.with_suffix(""))],
        check=True, capture_output=True,
    )


def run_homr(png, homr_cmd, timeout):
    """Run homr as a separate process; it writes <image>.musicxml next to the image."""
    env = dict(os.environ, CUDA_VISIBLE_DEVICES="", PYTHONWARNINGS="ignore")
    try:
        proc = subprocess.run(homr_cmd + [str(png)], cwd=png.parent, env=env, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ConversionError(f"timed out after {timeout} s")
    output = png.with_suffix(".musicxml")
    if proc.returncode != 0 or not output.exists():
        last = (proc.stderr.strip().splitlines() or ["no output"])[-1]
        raise ConversionError(last[:200])
    return output


def convert(pdf, work_dir, settings, on_page=None):
    """Returns (musicxml_bytes, warnings). Pages homr can't read are skipped with a warning;
    the conversion fails only if no page can be read."""
    pages = page_count(pdf)
    done, warnings = [], []
    for page in range(1, pages + 1):
        page_dir = Path(work_dir) / f"p{page:02d}"
        page_dir.mkdir(parents=True, exist_ok=True)
        png = page_dir / f"p{page:02d}.png"
        try:
            render_page(pdf, page, png, settings.render_dpi)
            done.append((page, run_homr(png, settings.homr_cmd, settings.page_timeout_s)))
        except (ConversionError, subprocess.CalledProcessError) as ex:
            warnings.append(f"page {page} skipped: {ex}")
        if on_page:
            on_page(page, pages)
    if not done:
        raise ConversionError("no music found on any page")
    score, join_warnings = join(done)
    out_path = Path(work_dir) / "result.musicxml"
    score.write("musicxml", fp=str(out_path))
    return out_path.read_bytes(), warnings + join_warnings
