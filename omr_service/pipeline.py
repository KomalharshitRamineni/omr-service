"""PDF -> MusicXML: render each page with pdftoppm, run homr on it, join the pages.
With PAGE_WORKERS > 1, that many pages are converted at the same time, each by its own homr process."""

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .join import write_joined


PACKAGE_PARENT = Path(__file__).resolve().parent.parent


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


def run_homr(png, homr_cmd, timeout, threads=0):
    """Run homr as a separate process; it writes <image>.musicxml next to the image."""
    env = dict(os.environ, CUDA_VISIBLE_DEVICES="", PYTHONWARNINGS="ignore", HOMR_ORT_THREADS=str(threads))
    # homr runs from the page's folder; let `python -m omr_service.homr_runner` find this package
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PACKAGE_PARENT), env.get("PYTHONPATH")]))
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
    """Returns (musicxml_bytes, warnings, bars to check). Pages homr can't read are skipped with a warning;
    the conversion fails only if no page can be read."""
    pages = page_count(pdf)

    def convert_page(page):
        page_dir = Path(work_dir) / f"p{page:02d}"
        page_dir.mkdir(parents=True, exist_ok=True)
        png = page_dir / f"p{page:02d}.png"
        render_page(pdf, page, png, settings.render_dpi)
        return run_homr(png, settings.homr_cmd, settings.page_timeout_s, settings.homr_threads)

    results, failures = {}, {}
    with ThreadPoolExecutor(max_workers=settings.page_workers) as pool:
        futures = {pool.submit(convert_page, page): page for page in range(1, pages + 1)}
        for finished, future in enumerate(as_completed(futures), 1):
            page = futures[future]
            try:
                results[page] = future.result()
            except (ConversionError, subprocess.CalledProcessError) as ex:
                failures[page] = f"page {page} skipped: {ex}"
            if on_page:
                on_page(finished, pages)
    done = sorted(results.items())
    warnings = [failures[page] for page in sorted(failures)]
    if not done:
        raise ConversionError("no music found on any page")
    out_path = Path(work_dir) / "result.musicxml"
    try:
        join_warnings, check = write_joined(done, out_path)
    except Exception as ex:  # the same pages would fail the same way, so the job is not retried
        raise ConversionError(f"could not join the pages into one score ({type(ex).__name__})") from ex
    return out_path.read_bytes(), warnings + join_warnings, check
