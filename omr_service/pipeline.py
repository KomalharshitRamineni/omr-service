"""PDF -> MusicXML: render each page with pdftoppm, run homr on it, join the pages.

homr runs in long-lived processes (homr_server), one per page converted at the same time, so its models are
loaded once per process rather than once per page. With HOMR_CMD set, homr is instead started for every page
with that command (tests, or another homr)."""

import json
import os
import queue
import subprocess
import threading
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


def _homr_env(threads, use_gpu=False):
    env = dict(os.environ, PYTHONWARNINGS="ignore", HOMR_ORT_THREADS=str(threads))
    if not use_gpu:
        env["CUDA_VISIBLE_DEVICES"] = ""
    # homr may run from another folder; let `python -m omr_service...` find this package
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PACKAGE_PARENT), env.get("PYTHONPATH")]))
    return env


def _last_line(text):
    return (text.strip().splitlines() or ["no output"])[-1][:200]


def run_homr(png, homr_cmd, timeout, threads=0, use_gpu=False):
    """Run homr as a separate process for one image; it writes <image>.musicxml next to the image."""
    try:
        proc = subprocess.run(homr_cmd + [str(png)], cwd=png.parent, env=_homr_env(threads, use_gpu),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ConversionError(f"timed out after {timeout} s")
    output = png.with_suffix(".musicxml")
    if proc.returncode != 0 or not output.exists():
        raise ConversionError(_last_line(proc.stderr))
    return output


class HomrServer:
    """One homr_server process, started on first use. A page that times out or crashes it restarts it."""

    def __init__(self, cmd, threads, log_path, use_gpu=False):
        self.cmd, self.threads, self.log_path, self.use_gpu = cmd, threads, Path(log_path), use_gpu
        self.proc = None

    def _start(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
                                     env=_homr_env(self.threads, self.use_gpu), cwd=self.log_path.parent,
                                     text=True, bufsize=1)
        self._replies = queue.Queue()
        threading.Thread(target=self._read, args=(self.proc.stdout, self._replies), daemon=True).start()

    @staticmethod
    def _read(stdout, replies):
        for line in stdout:
            replies.put(line)
        replies.put(None)  # the process has ended

    def convert(self, png, timeout):
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        try:
            self.proc.stdin.write(f"{png}\n")
            self.proc.stdin.flush()
            line = self._replies.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise ConversionError(f"timed out after {timeout} s")
        except BrokenPipeError:
            line = None
        if line is None:
            self.close()
            raise ConversionError(_last_line(self.log_path.read_text(errors="replace")))
        reply = json.loads(line)
        output = png.with_suffix(".musicxml")
        if not reply.get("ok") or not output.exists():
            raise ConversionError(str(reply.get("error") or "no output")[:200])
        return output

    def close(self):
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()
            self.proc = None
            self._log.close()


class Homr:
    """Converts one image per call, for up to `slots` callers at once: a pool of homr_server processes, or homr
    started for every page when HOMR_CMD is set."""

    def __init__(self, settings, slots=None):
        self.settings = settings
        self._idle = queue.LifoQueue()  # the most recently used process first: its models are loaded
        if settings.homr_server_cmd:
            for n in range(slots or settings.page_workers):
                self._idle.put(HomrServer(settings.homr_server_cmd, settings.homr_threads,
                                          settings.work_dir / f"homr-{os.getpid()}-{n}.log", settings.use_gpu))

    def convert(self, png):
        s = self.settings
        if not s.homr_server_cmd:
            return run_homr(png, s.homr_cmd, s.page_timeout_s, s.homr_threads, s.use_gpu)
        server = self._idle.get()
        try:
            return server.convert(png, s.page_timeout_s)
        finally:
            self._idle.put(server)

    def close(self):
        while not self._idle.empty():
            self._idle.get().close()


def convert_page(pdf, page, work_dir, settings, homr):
    """Renders one page and reads it; returns the page's MusicXML file. Raises ConversionError if homr can't."""
    page_dir = Path(work_dir) / f"p{page:02d}"
    page_dir.mkdir(parents=True, exist_ok=True)
    png = page_dir / f"p{page:02d}.png"
    try:
        render_page(pdf, page, png, settings.render_dpi)
    except subprocess.CalledProcessError as ex:
        raise ConversionError(f"could not render the page ({_last_line(ex.stderr.decode(errors='replace'))})")
    return homr.convert(png)


def join_pages(results, failures, out_path):
    """results: {page: MusicXML file}; failures: {page: reason}. Returns (musicxml_bytes, warnings, bars to
    check). Fails only if no page could be read."""
    warnings = [f"page {page} skipped: {failures[page]}" for page in sorted(failures)]
    if not results:
        raise ConversionError("no music found on any page")
    try:
        join_warnings, check = write_joined(sorted(results.items()), out_path)
    except Exception as ex:  # the same pages would fail the same way, so the job is not retried
        raise ConversionError(f"could not join the pages into one score ({type(ex).__name__})") from ex
    return Path(out_path).read_bytes(), warnings + join_warnings, check


def convert(pdf, work_dir, settings, on_page=None, homr=None):
    """A whole PDF in this process, PAGE_WORKERS pages at a time. Returns (musicxml_bytes, warnings, bars to
    check). Pages homr can't read are skipped with a warning."""
    pages = page_count(pdf)
    own = homr is None
    homr = homr or Homr(settings)
    results, failures = {}, {}
    try:
        with ThreadPoolExecutor(max_workers=settings.page_workers) as pool:
            futures = {pool.submit(convert_page, pdf, page, work_dir, settings, homr): page
                       for page in range(1, pages + 1)}
            for finished, future in enumerate(as_completed(futures), 1):
                page = futures[future]
                try:
                    results[page] = future.result()
                except ConversionError as ex:
                    failures[page] = str(ex)
                if on_page:
                    on_page(finished, pages)
    finally:
        if own:
            homr.close()
    return join_pages(results, failures, Path(work_dir) / "result.musicxml")
