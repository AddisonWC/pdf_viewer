"""The viewer's implementation modules; `pdfviewer.py` is the entry point.

Importing this package performs the two process-wide setup steps that must
happen before Qt does anything, which is why they live in `__init__` rather
than in the module each one belongs to: Python runs this file before any
`viewer.*` submodule, so no import order inside the package can bypass them.
"""
import os

# Native Wayland unless the environment already picked a platform. Wayland
# forbids a client from setting its own window position, which is why copy
# windows can only be hinted near their parent. Forcing xcb would restore
# that but softens rendering on mixed-DPI setups; settled, see HISTORY.md.
# pdfviewer_xcb.py forces xcb for comparison.
#
# Only asked for when a Wayland session is actually present. Qt does NOT fall
# back: with no compositor to talk to, "wayland" aborts at startup with
# "Could not load the Qt platform plugin", so an unconditional default makes
# the viewer refuse to start under X11, VNC and SSH X-forwarding rather than
# merely look worse there. With neither variable set, Qt picks for itself.
#
# This must run before the first PySide6 import anywhere in the process: Qt
# reads QT_QPA_PLATFORM when the plugin loads, and setting it afterwards has
# no effect. Nothing above this line may import PySide6.
if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland")

from PySide6.QtCore import QThreadPool  # noqa: E402  (must follow the gate)

# PyMuPDF rendering isn't GIL-free, so simultaneous render workers starve
# the GUI thread instead of adding throughput (measurements in DEVLOG.md).
# Little parallelism is lost: at most one navigation's worth of work is
# ever in flight anyway (_render_inflight/_prefetch_inflight).
QThreadPool.globalInstance().setMaxThreadCount(1)
