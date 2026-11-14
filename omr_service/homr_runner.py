"""Runs homr on one image with a fixed number of ONNX Runtime threads: python -m omr_service.homr_runner <image>.

homr creates its ONNX Runtime sessions without a thread count, so ONNX Runtime starts one thread per core it
can see. In a container that may use fewer CPUs than the host has, those threads compete for the container's
share and every page takes longer (2026-11-13, with homr under a 2-CPU limit: 5 pages in 143 s with the default
threads, 87 s with 2 threads, the same MusicXML). This runner gives every session homr creates
intra_op_num_threads = HOMR_ORT_THREADS (and one inter-op thread). homr itself is not modified.
"""

import os
import sys

import onnxruntime as ort

THREADS = int(os.environ.get("HOMR_ORT_THREADS", "0"))
_Session = ort.InferenceSession


class _ThreadLimitedSession(_Session):
    def __init__(self, path_or_bytes, sess_options=None, providers=None, provider_options=None, **kwargs):
        if THREADS > 0:
            sess_options = sess_options or ort.SessionOptions()
            sess_options.intra_op_num_threads = THREADS
            sess_options.inter_op_num_threads = 1
        super().__init__(path_or_bytes, sess_options, providers, provider_options, **kwargs)


ort.InferenceSession = _ThreadLimitedSession

from homr import main as homr_main  # noqa: E402  (imported after the patch, so homr's sessions use it)

if __name__ == "__main__":
    sys.exit(homr_main.main())
