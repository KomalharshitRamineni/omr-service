"""Keeps homr loaded and converts pages one after another: python -m omr_service.homr_server.

Starting homr for every page costs about 2 s (Python, homr's imports, its ONNX models and the title reader) on
a laptop core, more on Azure's. homr keeps its models in module globals after the first page, so a process
that stays alive skips that cost from the second page on. Each line on stdin is the path of one image; homr
writes <image>.musicxml next to it, as `homr <image>` does, with the same settings. Each image gets one JSON
line in reply, {"ok": true} or {"ok": false, "error": "..."}, on this process's original stdout; anything homr
prints goes to stderr. ONNX Runtime's thread count comes from HOMR_ORT_THREADS, as with homr_runner.
"""

import json
import os
import sys

# Replies go to the original stdout; from here on, print() and anything native code writes to stdout land on
# stderr, so homr's output cannot be mistaken for a reply.
_replies = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

from . import homr_runner  # noqa: E402  (sets the ONNX Runtime thread count, then imports homr)

hm = homr_runner.homr_main


def settings():
    """What homr's main() builds for `homr <image>` with no options (homr 0.7.1)."""
    transformer_gpu = hm.cuda_available() or hm.rocm_available()
    segnet_gpu = transformer_gpu or hm.coreml_available()
    hm.download_weights(segnet_gpu, transformer_gpu, False)
    hm.download_ocr_weights()
    hm.ort.set_default_logger_severity(3)
    config = hm.ProcessingConfig(False, False, False, False, -1, transformer_gpu, segnet_gpu, False, True)
    return config, hm.XmlGeneratorArguments(False, None, None)


def main():
    config, xml_args = settings()
    for line in sys.stdin:
        image = line.rstrip("\n")
        if not image:
            continue
        try:
            hm.process_image(image, config, xml_args)
            reply = {"ok": True}
        except Exception as ex:  # one bad page must not stop the process
            reply = {"ok": False, "error": str(ex) or type(ex).__name__}
        _replies.write(json.dumps(reply) + "\n")


if __name__ == "__main__":
    main()
