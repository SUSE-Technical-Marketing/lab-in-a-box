"""
Thin wrapper around the optional `qrencode` CLI (zypper/apt package: qrencode) for turning a URL into a
QR code: ASCII block art for a terminal/log (setup_lab.py's end-of-run summary) or inline SVG markup for
a web page (lab-builder-helper's lab-create answer, rendered by the webui). Used instead of a hand-rolled
or vendored QR encoder so both call sites share one battle-tested implementation; neither caller needs a
Python or JS QR library of its own.

Every function returns None, never raises, when qrencode is missing or fails — callers degrade to
showing the plain link, which is always correct on its own.
"""
import shutil
import subprocess

_BINARY = "qrencode"


def available():
    """True when the qrencode binary can be found on PATH."""
    return shutil.which(_BINARY) is not None


def render_ascii(text):
    """`text` (a URL) as UTF-8 half-block art sized for a terminal, or None."""
    if not text or not available():
        return None
    try:
        r = subprocess.run([_BINARY, "-t", "UTF8", "-m", "2", "--", text],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
    except (subprocess.CalledProcessError, OSError):
        return None
    return r.stdout.decode("utf-8", "replace").rstrip("\n") or None


def render_svg(text):
    """`text` (a URL) as standalone SVG markup, safe to drop straight into a page's DOM, or None."""
    if not text or not available():
        return None
    try:
        r = subprocess.run([_BINARY, "-t", "SVG", "-o", "-", "--", text],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
    except (subprocess.CalledProcessError, OSError):
        return None
    return r.stdout.decode("utf-8", "replace") or None
