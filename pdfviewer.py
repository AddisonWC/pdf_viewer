#!/usr/bin/env python3
"""Grid PDF viewer for reading dense math papers and books.

Shows an MxN grid of pages and slides it forward one page at a time, so a
few pages stay visible together. See README.md and FEATURES.md for what it
is and why; docs/ARCHITECTURE.md for how it is built.

Usage: pdfviewer.py FILE.pdf [--rows R] [--cols C]
"""
import argparse
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import namedtuple, OrderedDict
from urllib.parse import quote_plus

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
if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland")

import fitz  # PyMuPDF
import numpy as np  # dark mode's per-pixel hue-preserving transform
from PySide6.QtCore import (
    QElapsedTimer,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)

# PyMuPDF rendering isn't GIL-free, so simultaneous render workers starve
# the GUI thread instead of adding throughput (measurements in DEVLOG.md).
# Little parallelism is lost: at most one navigation's worth of work is
# ever in flight anyway (_render_inflight/_prefetch_inflight).
QThreadPool.globalInstance().setMaxThreadCount(1)

from PySide6.QtGui import (
    QBrush,
    QClipboard,
    QColor,
    QDesktopServices,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

# How long to wait after a resize before re-rendering at full resolution.
# Keeps interactive window-dragging smooth instead of rendering every frame.
RESIZE_DEBOUNCE_MS = 60

# Slide-animation duration for a single-page advance (h/l, wheel, or a
# search jump one page away), and the animation clock's tick rate.
# Addison set the duration explicitly.
PAGE_SLIDE_DURATION_MS = 120
PAGE_SLIDE_TICK_MS = 8

# Multiplicative step for one press/notch of cursor-anchored zoom (-, =,
# Ctrl+wheel). ~1.15 squared, so one press now moves as far as two used to.
ZOOM_STEP = 1.32

# A trackpad pinch scales by its own reported delta, which is tiny per
# event; this amplifies it. Addison asked for the pinch rate to be raised,
# so treat the current value as chosen rather than arbitrary.
PINCH_GAIN = 3.0

# Continuous zoom fires dozens of events a second, and a full re-render per
# event is what made pinch laggy. The crop updates immediately; the
# expensive re-render coalesces onto this cadence.
ZOOM_RENDER_THROTTLE_MS = 30

# Debounce before an incremental (vim-style) search actually runs, so typing
# a query doesn't kick off a full-document search on every keystroke.
SEARCH_DEBOUNCE_MS = 120

# How often the main thread checks whether the background read of the PDF has
# finished (see _DocumentSource and _adopt_buffer). Only ticks until it has.
BUFFER_WATCH_MS = 150

# ~248/255 of white. Applied to both the cells and the central widget behind
# the grid spacing, so the gaps between pages match the cells exactly instead
# of showing a seam of the widget's default background color.
BACKGROUND_COLOR = "#f8f8f8"
# Used instead of BACKGROUND_COLOR for the same two spots when dark mode
# (D) is on, so the gaps around dark-inverted pages don't sit inside a
# jarring bright-white grid.
DARK_BACKGROUND_COLOR = "#1e1e1e"

# Drag-box colors: blue for text selection, orange for the zoom box, both
# with a light (low-alpha) fill so the underlying page stays readable while
# dragging, and a more solid border for definition.
SELECTION_FILL = QColor(40, 110, 235, 55)
SELECTION_BORDER = QColor(40, 110, 235, 200)
ZOOM_FILL = QColor(255, 140, 0, 55)
ZOOM_BORDER = QColor(255, 140, 0, 200)

# Busy indicator, drawn as a suffix on the page counter. Addison asked for it
# to sit to the right of the page numbers so that it can't move them, and to
# change every half second. Frames are the four quadrant-filled circles, which
# read as one spinning disc; all four are the same advance width in the fonts
# Qt falls back to, so the card doesn't twitch as it turns.
BUSY_FRAMES = ("\u25d0", "\u25d3", "\u25d1", "\u25d2")
BUSY_INTERVAL_MS = 500

# Search highlight colors, vim-style: a saturated yellow for the one specific
# match currently landed on (via search/n/N), a lower-saturation yellow for
# every other match anywhere else visible right now.
SEARCH_CURRENT_FILL = QColor(235, 198, 0, 150)
SEARCH_OTHER_FILL = QColor(255, 236, 100, 135)

# Row background painted on the TOC entry that keyboard navigation last
# landed on (see _apply_toc_highlight) -- solid yellow, since the tree keeps
# the default (light) palette regardless of the page dark-mode toggle.
TOC_HIGHLIGHT_COLOR = QColor(255, 225, 77)

# Translucent yellow drawn over the section header ON THE PAGE that TOC
# navigation jumped to (see get_toc_text_highlights) -- the in-document
# counterpart of the TOC row highlight, so a jump points at the header itself.
TOC_TEXT_HIGHLIGHT_FILL = QColor(255, 225, 77, 120)

# Leading section-number / label prefix of a TOC title ("3.2 ", "Chapter 4:",
# "A.1 "), stripped when the full title doesn't match on the page so the bare
# header text ("Convergence Analysis") can still be found and highlighted.
_TOC_NUM_PREFIX = re.compile(
    r"^\s*(?:chapter|section|appendix|part)?\s*[0-9A-Z]+(?:\.[0-9]+)*[.:]?\s+",
    re.IGNORECASE,
)

# The start of a numbered bibliography entry, e.g. "[12]" (the number is
# captured). Used to snap a citation's web-search extraction to whole reference
# entries, and to match the clicked citation's number to its entry (see
# _reference_text_at / _citation_number).
_REF_MARKER = re.compile(r"^\s*\[(\d+)\]")

# Per-item TOC data roles. Qt.UserRole already holds each entry's target
# page (set in _populate_toc); these carry the entry's tree depth and its
# position in the flattened reading-order list (both for U/I/O/Y navigation),
# plus the destination's y-coordinate on the target page (top-left origin,
# from get_toc(simple=False)), used to place the in-document header highlight
# precisely instead of guessing from the title text.
_TOC_ROLE_LEVEL = Qt.UserRole + 1
_TOC_ROLE_INDEX = Qt.UserRole + 2
_TOC_ROLE_DEST_Y = Qt.UserRole + 3

# The floating translucent cards -- page counter, go-to-page, search, ask --
# drawn on top of the pages without taking any layout space. Input fields
# inside get an opaque style so typed text stays crisp; the search field
# swaps to FIELD_STYLE_NO_MATCH when a query hits nothing.
OVERLAY_BOX_STYLE = (
    "background-color: rgba(20, 20, 20, 150); color: #f0f0f0;"
    " border-radius: 5px; padding: 2px 7px;"
)
OVERLAY_LABEL_STYLE = "background: transparent; color: #f0f0f0;"
FIELD_STYLE = (
    "background-color: #ffffff; color: #101010; border: none;"
    " border-radius: 3px; padding: 1px 3px;"
)
FIELD_STYLE_NO_MATCH = (
    "background-color: #e24d4d; color: #ffffff; border: none;"
    " border-radius: 3px; padding: 1px 3px;"
)

# Strong references to every window opened as "a copy of the current
# document" (Ctrl+N, or center-click on a link), so Qt doesn't garbage
# collect them the moment the local variable that created them goes away.
# Each window removes itself in closeEvent, so this only ever holds windows
# that are actually still open.
_open_windows = []


# ---------- last-read-page persistence ----------
#
# Addison asked the viewer to remember where he was in each document. Just
# the page: grid size and crop describe how this window happens to be set
# up, not the document. Best-effort throughout -- a missing or corrupt
# state file must never break opening or closing a document.

def _state_file_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "pdfviewer", "last_page.json")


def _load_state():
    try:
        with open(_state_file_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _load_last_page(path):
    return _load_state().get(os.path.abspath(path))


def _save_last_page(path, first_page):
    state_path = _state_file_path()
    data = _load_state()
    data[os.path.abspath(path)] = first_page
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

# ---------- "Ask an agent about this PDF" launcher ----------
#
# Fire-and-forget by design: open an interactive session in a terminal,
# seeded only with this document's path, and never talk to it again. The
# viewer's whole job is handing off which file. pdf_helper_prompt.txt (next
# to this script) is appended to the session's system prompt so it reads the
# *page images* for notation questions -- math symbols don't survive text
# extraction. Model/effort are per-session flags, so no other session is
# affected. The terminal and CLI program are hard-wired here; FEATURES.md
# says they should be the user's choice, which is intent, not yet true.

def _launch_claude_helper(pdf_path, context=""):
    """Open gnome-terminal running `claude` on this PDF. `context`, if given,
    becomes the first prompt; if empty, Claude is told to stand by. Returns
    True if a terminal was launched."""
    pdf_path = os.path.abspath(pdf_path)
    workdir = os.path.dirname(pdf_path)
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "pdf_helper_prompt.txt")
    context = context.strip()
    if context:
        initial = f"I'm reading the PDF at {pdf_path}. {context}"
    else:
        initial = (f"You're going to answer questions about the PDF at {pdf_path}. "
                   "Wait for my instructions.")
    # Terminal, CLI program and model flags are hard-wired here. FEATURES.md
    # says the choice is the user's; that is a statement of intent, not yet
    # true, and making it true means making all four configurable.
    claude_argv = [
        "claude",
        "--model", "sonnet",
        "--effort", "high",
        "--append-system-prompt-file", helper,
        initial,
    ]
    term = shutil.which("gnome-terminal")
    if not term:
        print("Ask Claude: gnome-terminal not found on PATH", file=sys.stderr)
        return False
    # Run via a login shell so `claude` resolves on PATH; keep the window open
    # after it exits (exec bash) so the last answer stays readable.
    inner = " ".join(shlex.quote(a) for a in claude_argv) + "; exec bash"
    try:
        subprocess.Popen(
            [term, f"--working-directory={workdir}", "--", "bash", "-lc", inner],
            cwd=workdir, start_new_session=True,
        )
    except Exception as exc:  # never let a launch failure crash the viewer
        print(f"Ask Claude: failed to launch terminal: {exc}", file=sys.stderr)
        return False
    return True


# Ctrl+H documentation, shown in place of the grid until Escape. Hand-written
# rather than generated from the bind() table so it can group related actions
# and explain why. It drifts silently: update it in the same edit as any key
# change.
HELP_HTML = """
<h2>PDF Viewer -- Help</h2>
<p><i>Press Escape to close this and go back to reading.</i></p>

<h3>Turning pages</h3>
<table cellspacing="6">
<tr><td><b>L / Right / Down / Space / Page Down</b></td><td>Advance one page (whole grid slides forward)</td></tr>
<tr><td><b>H / Left / Up / Backspace / Page Up</b></td><td>Go back one page</td></tr>
<tr><td><b>J / K</b></td><td>Down / up by a whole grid row at a time (in a multi-column grid, a non-overlapping row of pages)</td></tr>
<tr><td><b>Mouse wheel</b></td><td>One page per notch</td></tr>
<tr><td><b>Home / End</b></td><td>Jump to the first / last page</td></tr>
<tr><td><b>G</b>, then type a page number, <b>Enter</b></td><td>Go to a specific page (a "Go to page" box appears top-left while active), with the usual page-turn animation</td></tr>
<tr><td><b>Shift+G</b>, then type a page number, <b>Enter</b></td><td>Same, but skips the animation</td></tr>
</table>

<h3>Grid layout</h3>
<table cellspacing="6">
<tr><td><b>, / .</b></td><td>Fewer / more columns</td></tr>
<tr><td><b>Shift+, / Shift+.</b></td><td>Fewer / more rows</td></tr>
</table>

<h3>Zoom &amp; crop</h3>
<table cellspacing="6">
<tr><td><b>Right-click-drag</b> on a page</td><td>Draw a box to crop/zoom to that region (all pages)</td></tr>
<tr><td><b>Right-drag a box, then click <i>left</i></b></td><td>Zoom into that box and collapse to a single page; the previous grid is remembered (see below). Right-click then left-click without dragging a box does the same collapse without zooming</td></tr>
<tr><td><b>Left-drag, then click <i>right</i></b> before releasing</td><td>Content-crop (same as C)</td></tr>
<tr><td><b>- / = (or +)</b>, <b>Ctrl+wheel</b>, trackpad <b>pinch</b></td><td>Zoom out / in toward the mouse cursor; magnifies all pages without changing the grid layout</td></tr>
<tr><td><b>0</b>, <b>B</b>, or <b>Escape</b></td><td>Back to the full, uncropped page (Escape does this once there's no search box, Ask box, or help page open to close first)</td></tr>
<tr><td><b>C</b> or <b>M</b></td><td>Auto-crop margins/headers/footers, sampled across several pages (robust default)</td></tr>
<tr><td><b>Shift+C</b> or <b>Shift+M</b></td><td>Same, but detected from only the current page</td></tr>
<tr><td><b>Mouse "Back" side button</b></td><td>Same as C -- re-crop to detected content</td></tr>
<tr><td colspan="2"><i>Any zoom-out (0/B/Escape) or recrop (C/M, Back button) also restores the grid saved by a box-zoom-to-single-page collapse.</i></td></tr>
</table>

<h3>Search</h3>
<table cellspacing="6">
<tr><td><b>/</b>, then type</td><td>Incremental search: runs as you type and jumps to the first match from where you were. The box (top-right) stays open and turns red if nothing matches</td></tr>
<tr><td><b>Enter</b></td><td>Commit: keeps the box and highlights up but hands focus back to the document, so keys act on the pages again instead of typing into the box</td></tr>
<tr><td><b>N / Shift+N</b></td><td>After Enter, step to the next / previous match</td></tr>
<tr><td><b>Escape</b></td><td>Close the search box, drop the highlighting, and forget the query</td></tr>
</table>

<h3>Ask Claude about this PDF</h3>
<table cellspacing="6">
<tr><td><b>?</b> (Shift+/)</td><td>Open a box to start an interactive Claude Code session about this document in a terminal. Type optional context for the first prompt, or leave it blank to just drop into a session that's standing by</td></tr>
<tr><td><b>Enter</b></td><td>Launch the terminal (the PDF's path is handed off automatically, so you don't have to find the file again)</td></tr>
<tr><td><b>Escape</b></td><td>Cancel without launching</td></tr>
</table>

<h3>Selecting &amp; copying text</h3>
<table cellspacing="6">
<tr><td><b>Left-drag</b> over text</td><td>Select it. On release the selection goes to the X/Wayland <i>primary selection</i>, so you can paste it straight into another window with a middle-click. Your ordinary clipboard is left alone</td></tr>
<tr><td><b>Ctrl+left-drag</b></td><td>Add another, separate selection (e.g. across pages) instead of replacing the current one</td></tr>
<tr><td><b>Ctrl+C</b></td><td>Copy the current selection to the ordinary clipboard, for pasting with Ctrl+V</td></tr>
<tr><td><b>Escape</b></td><td>Clear the current selection (and, in the same press, zoom back out -- see Zoom &amp; crop)</td></tr>
</table>

<h3>Links</h3>
<table cellspacing="6">
<tr><td><i>Cursor becomes a pointing hand over any link.</i></td><td></td></tr>
<tr><td><b>Click</b> a link</td><td>Follow it in this window (internal links jump to the target page; external links open in your browser)</td></tr>
<tr><td><b>Center-click (middle button)</b> a link</td><td>For an internal link: open a copy of the document, jumped to that link's target, positioned near this window (native Wayland can't place a window exactly, only hint that it belongs near its parent). An external link opens in your browser, same as a normal click. Does nothing if there's no link under the cursor</td></tr>
<tr><td><b>Center-drag</b> (while zoomed in)</td><td>Pan the zoomed view -- grab the page and slide it under the cursor. Only pans when zoomed in; a plain center-click (no drag) still follows the link</td></tr>
<tr><td><b>Center-hold, then left-click</b> a citation</td><td>Look the cited work up on Google Scholar in your browser instead of jumping to the bibliography: opens a search for the reference entry the citation points at</td></tr>
</table>

<h3>History &amp; windows</h3>
<table cellspacing="6">
<tr><td><b>Alt+Left / Alt+Right</b></td><td>Back / forward through pages you've jumped to (via links, "go to page", or a search that moved you -- a whole search folds into one Back step, returning to where you opened it)</td></tr>
<tr><td><b>Ctrl+N</b></td><td>Open a copy of this window at the same page and grid (the copy re-detects its own content crop, so its zoom may differ)</td></tr>
<tr><td><b>Mouse "Forward" side button, held</b></td><td>Chord modifier: scroll wheel changes column count, right-click resets zoom, while held</td></tr>
</table>

<h3>Table of contents</h3>
<table cellspacing="6">
<tr><td><b>T</b> or <b>;</b></td><td>Toggle the table-of-contents sidebar (closed by default, opens collapsed to the top level). Click an entry to jump there. Shows a placeholder if the document has none</td></tr>
<tr><td><b>U / I</b></td><td>Page down / up through entries at the current level, flowing across section boundaries: paging down off a section's last subsection lands on the next section, then descends into its subsections; paging up does the reverse. The jumped-to entry is highlighted yellow, its header is highlighted on the page, and the view follows</td></tr>
<tr><td><b>O</b></td><td>Dive in: open the current entry's sublevel, move to its first child, and make that the level U/I now page at (on a bottom-level entry it just sets the level to here)</td></tr>
<tr><td><b>Y</b></td><td>Climb out: if this section's subsections are open, first just close them and set the level to this section; a second Y then moves to the parent, folding the level being left. Either way the level U/I pages at follows where you land</td></tr>
<tr><td colspan="2"><i>Y/U/I/O sit one keyboard row above H/J/K/L -- a vim motion shape shifted up. Pressing any of them opens the sidebar first if it's closed; closing the sidebar removes the header highlight from the page.</i></td></tr>
</table>

<h3>Other</h3>
<table cellspacing="6">
<tr><td><b>D</b></td><td>Toggle dark mode -- inverts each pixel's lightness while keeping its hue, so text goes white-on-dark but figures keep their colors</td></tr>
<tr><td><b>Ctrl+H</b></td><td>Show this help</td></tr>
<tr><td><b>Ctrl+Q / Q</b></td><td>Quit</td></tr>
</table>
<p><i>The page counter (top-left overlay, shown as first-last/total) has a small spinner to its right that turns while background work (loading, page rendering, search, or content detection) is running.</i></p>
"""


def _union_rects(rects):
    return fitz.Rect(
        min(r.x0 for r in rects),
        min(r.y0 for r in rects),
        max(r.x1 for r in rects),
        max(r.y1 for r in rects),
    )


def _rect_gap(a, b):
    """Euclidean gap between two rects; 0 if they touch or overlap."""
    dx = max(a.x0 - b.x1, b.x0 - a.x1, 0.0)
    dy = max(a.y0 - b.y1, b.y0 - a.y1, 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _point_rect_dist(pt, r):
    """Euclidean distance from a point to a rect; 0 if the point is inside."""
    dx = max(r.x0 - pt[0], pt[0] - r.x1, 0.0)
    dy = max(r.y0 - pt[1], pt[1] - r.y1, 0.0)
    return (dx * dx + dy * dy) ** 0.5


# Boilerplate that publishers/repositories stamp directly on top of the
# content -- these can't be separated from the body by position (a SIAM
# copyright stamp, for example, tiles across the *entire* page, fully
# overlapping the real text), so they're recognized by their wording
# instead and dropped before any spatial reasoning happens.
_BOILERPLATE_KEYWORDS = (
    "arxiv:",
    "copyright",
    "©",
    "all rights reserved",
    "unauthorized reproduction",
    "downloaded",
    "redistribution subject",
    "licensed to",
    "this content downloaded",
    "terms and conditions",
    "for personal use only",
    "preprint submitted to",
)


def _is_boilerplate(text):
    lowered = text.lower()
    return any(kw in lowered for kw in _BOILERPLATE_KEYWORDS)


def _pad_within_page(rect, page_rect):
    """Breathing room around a detected content rect. Addison wants mild
    padding: a crop pulled tight against the glyphs reads as cramped, and
    under-cropping is much better than over-cropping."""
    pad_x, pad_y = page_rect.width * 0.01, page_rect.height * 0.01
    return fitz.Rect(
        max(page_rect.x0, rect.x0 - pad_x),
        max(page_rect.y0, rect.y0 - pad_y),
        min(page_rect.x1, rect.x1 + pad_x),
        min(page_rect.y1, rect.y1 + pad_y),
    )


# ---------- graphics, for pages that are more figure than text ----------
#
# get_text("blocks") is text-only and never reports vector art, so a crop
# detected from text alone treats a figure as empty space. get_bboxlog()
# lists every painted mark's bbox and is the cheapest way to see the rest,
# but it is unbounded in page complexity and holds the GIL for much of its
# run -- hence the budgets both callers impose.
#
# The kind strings are MuPDF's own device-call names and must be spelled
# exactly. A raster figure logs as "fill-image" (or "fill-imgmask"), never
# "image"; that misspelling silently matched nothing for weeks. Text kinds
# are deliberately absent: text arrives via get_text("blocks"), which is
# what the boilerplate filter runs on, so admitting it here would smuggle
# watermarks past that filter.
_GRAPHIC_BBOX_KINDS = frozenset(
    ("fill-path", "stroke-path", "fill-image", "fill-imgmask", "fill-shade")
)

# Marks covering this much of the page are backgrounds and frames (a
# page-sized white fill under the content, a ruled border), not content to
# crop to -- keeping them would defeat the crop on every page of such a
# document. The bar is high because a genuine full-width figure plus its
# margins can still reach ~0.75.
_GRAPHIC_PAGE_COVER_MAX = 0.9

# A vector figure arrives as thousands of separate strokes and the
# clustering below is O(n^2) in blocks. Bucketing each mark into a coarse
# grid cell by its centre and unioning per cell bounds the input at
# _GRAPHIC_GRID^2 rects while keeping the spatial separation clustering
# relies on: a lone mark in the margin still lands in its own cell and
# still loses on area.
_GRAPHIC_GRID = 8


def _graphic_rects(page):
    """Bounding rects of the page's images and vector art, coarsely
    bucketed so a stroke-heavy figure can't blow up the clustering.

    Vectorized deliberately. The per-mark cost here is not incidental
    book-keeping: a dense vector figure (a filled contour/colormap plot,
    or a mesh) is emitted as tens of thousands of individual marks --
    64929 on one page of vilar--subcell.pdf -- and a Python loop building
    a fitz.Rect per mark ran ~17us each, i.e. 1.4 SECONDS on that page,
    all of it holding the GIL, so the whole app froze while a sampled
    content-crop worked through it. Every step below (normalize, clip to
    the page, drop covers/degenerates, bucket, union per bucket) is the
    same arithmetic done once over numpy arrays: ~40ms for that page,
    against the 285ms MuPDF itself spends handing the list over, which is
    the floor here and can't be got under.
    """
    try:
        bboxlog = page.get_bboxlog()
    except Exception:
        return []  # older/newer PyMuPDF without it: text-only detection
    pr = page.rect
    page_area = pr.width * pr.height
    if page_area <= 0:
        return []
    boxes = [box for kind, box in bboxlog if kind in _GRAPHIC_BBOX_KINDS]
    if not boxes:
        return []
    b = np.asarray(boxes, dtype=np.float64).reshape(-1, 4)

    # normalize (a mark's corners can arrive in either order) and clip to
    # the page -- marks can be drawn partly off-page.
    x0 = np.clip(np.minimum(b[:, 0], b[:, 2]), pr.x0, pr.x1)
    y0 = np.clip(np.minimum(b[:, 1], b[:, 3]), pr.y0, pr.y1)
    x1 = np.clip(np.maximum(b[:, 0], b[:, 2]), pr.x0, pr.x1)
    y1 = np.clip(np.maximum(b[:, 1], b[:, 3]), pr.y0, pr.y1)
    w, h = x1 - x0, y1 - y0
    keep = (w > 0) & (h > 0) & (w * h < page_area * _GRAPHIC_PAGE_COVER_MAX)
    if not keep.any():
        return []
    x0, y0, x1, y1 = x0[keep], y0[keep], x1[keep], y1[keep]

    col = np.minimum(
        (((x0 + x1) / 2 - pr.x0) / pr.width * _GRAPHIC_GRID).astype(np.intp),
        _GRAPHIC_GRID - 1,
    )
    row = np.minimum(
        (((y0 + y1) / 2 - pr.y0) / pr.height * _GRAPHIC_GRID).astype(np.intp),
        _GRAPHIC_GRID - 1,
    )

    # Union per occupied cell: sort by cell id, then reduce each run. (A
    # 64-slot np.minimum.at would be simpler to read but ufunc.at is an
    # unbuffered element-at-a-time loop in C -- slower than the sort.)
    cell = row * _GRAPHIC_GRID + col
    order = np.argsort(cell, kind="stable")
    starts = np.concatenate(([0], np.flatnonzero(np.diff(cell[order])) + 1))
    gx0 = np.minimum.reduceat(x0[order], starts)
    gy0 = np.minimum.reduceat(y0[order], starts)
    gx1 = np.maximum.reduceat(x1[order], starts)
    gy1 = np.maximum.reduceat(y1[order], starts)
    return [fitz.Rect(*c) for c in zip(gx0.tolist(), gy0.tolist(),
                                       gx1.tolist(), gy1.tolist())]


# ---------- scanned pages, which have no text to detect from ----------
#
# A scan is one full-page image: no text blocks, no vector art, so every
# rule above returns None. The ink is still visible in the raster, so
# detection falls back to rendering the page small and bounding dark pixels.

# Enough to resolve a margin to ~1.5pt; the bbox doesn't need more, and the
# render is the expensive part (~8ms/page here, ~90ms for a big book scan).
_RASTER_DETECT_DPI = 50

# Ink is judged relative to the page's own paper colour: older scans
# photograph as ~210 grey rather than white, so a fixed cut-off either
# misses their ink or counts their paper as ink.
_RASTER_PAPER_QUANTILE = 0.9
_RASTER_INK_DROP = 0.2      # ink is this much darker than paper...
_RASTER_INK_DROP_MIN = 40.0  # ...but at least this many levels darker

# Book scans often include the dark surround of the scanner bed as a solid
# band along one or more edges. A line that is this inked is that band, not
# content, so it gets peeled off before the bbox is measured -- but never
# more than a quarter of the page, so a genuinely dark page can't be eaten.
_RASTER_FRAME_INK = 0.6
_RASTER_FRAME_MAX_TRIM = 0.25

# A row/column needs this fraction of its length inked to count as content;
# it filters the single-pixel dirt that scanners sprinkle along the border.
_RASTER_LINE_INK = 0.006

# ...and dirt that survives that is dropped by the same reasoning the text
# pass uses on page numbers and margin stamps: an outermost run of inked
# lines that is negligible or only a hair thick, and separated from
# everything else by more than a normal line gap, is not body content. A
# 2px smear of speckle down one edge was otherwise holding the crop out to
# the full page width.
_RASTER_ISOLATED_GAP = 0.035
_RASTER_ISOLATED_SHARE = 0.02
_RASTER_ISOLATED_EXTENT = 0.03


def _raster_trim_frame(ink):
    """Peel solid scanner-bed bands off the four edges of an ink mask."""
    h, w = ink.shape
    rows, cols = ink.sum(1), ink.sum(0)
    top, bottom, left, right = 0, h, 0, w
    max_v, max_h = int(h * _RASTER_FRAME_MAX_TRIM), int(w * _RASTER_FRAME_MAX_TRIM)
    while top < max_v and rows[top] >= w * _RASTER_FRAME_INK:
        top += 1
    while bottom > h - max_v and rows[bottom - 1] >= w * _RASTER_FRAME_INK:
        bottom -= 1
    while left < max_h and cols[left] >= h * _RASTER_FRAME_INK:
        left += 1
    while right > w - max_h and cols[right - 1] >= h * _RASTER_FRAME_INK:
        right -= 1
    if bottom - top < 8 or right - left < 8:
        return 0, h, 0, w  # nothing survived; the page is dark all over
    return top, bottom, left, right


def _raster_span(profile, threshold, gap, length):
    """First and last inked line of `profile`, after discarding isolated
    specks at either end. Returns a half-open (start, stop) or None."""
    inked = np.flatnonzero(profile >= threshold)
    if inked.size == 0:
        return None
    groups = np.split(inked, np.flatnonzero(np.diff(inked) > gap) + 1)
    weights = [float(profile[g].sum()) for g in groups]
    total = sum(weights)

    def is_speck(k):
        thin = (groups[k][-1] - groups[k][0] + 1) < length * _RASTER_ISOLATED_EXTENT
        return thin or weights[k] < total * _RASTER_ISOLATED_SHARE

    while len(groups) > 1 and is_speck(0):
        groups.pop(0)
        weights.pop(0)
    while len(groups) > 1 and is_speck(-1):
        groups.pop()
        weights.pop()
    return int(groups[0][0]), int(groups[-1][-1]) + 1


def _raster_content_rect(page):
    """Bound the ink on a page with no extractable content (a scan)."""
    try:
        pm = page.get_pixmap(dpi=_RASTER_DETECT_DPI, colorspace=fitz.csGRAY)
    except Exception:
        return None
    h, w = pm.height, pm.width
    if w < 8 or h < 8:
        return None
    # stride, not width: MuPDF pads each row out to a word boundary.
    img = np.frombuffer(pm.samples, dtype=np.uint8).reshape(h, pm.stride)[:, :w]
    paper = float(np.quantile(img, _RASTER_PAPER_QUANTILE))
    ink = img < paper - max(_RASTER_INK_DROP_MIN, paper * _RASTER_INK_DROP)

    top, bottom, left, right = _raster_trim_frame(ink)
    body = ink[top:bottom, left:right]
    bh, bw = body.shape
    gap = max(bh, bw) * _RASTER_ISOLATED_GAP
    ys = _raster_span(body.sum(1), max(2, round(bw * _RASTER_LINE_INK)), gap, bh)
    xs = _raster_span(body.sum(0), max(2, round(bh * _RASTER_LINE_INK)), gap, bw)
    if ys is None or xs is None:
        return None  # blank page
    pr = page.rect
    sx, sy = pr.width / w, pr.height / h
    return _pad_within_page(
        fitz.Rect(
            pr.x0 + (left + xs[0]) * sx,
            pr.y0 + (top + ys[0]) * sy,
            pr.x0 + (left + xs[1]) * sx,
            pr.y0 + (top + ys[1]) * sy,
        ),
        pr,
    )


def detect_content_rect(page, allow_raster=True, allow_graphics=True):
    """Guess the "real" content area of a page, excluding running
    headers/footers, page numbers, and margin watermarks/copyright stamps
    (e.g. arXiv's vertical stamp, SIAM's tiled copyright overlay) -- the
    things journal-article crops usually want cut.

    Two passes over the page's text blocks and figures:
      1. Drop any text block whose wording matches typical watermark/
         copyright/download-stamp boilerplate, regardless of its size or
         position -- needed because some publisher stamps are tiled
         across the whole page and can't be told apart from real content
         by geometry alone.
      2. Among what's left, agglomeratively cluster blocks that sit close
         together (small gap) into groups, and keep the group with the
         most actual text/figure area as "the body", plus any other group
         holding a comparable share of it. A page number or rotated side
         watermark almost always sits well clear of the body's whitespace
         margin, so it forms its own small group and gets left out; a
         paragraph that merely runs close to the page edge stays part of
         the body's group instead.
    A page with nothing extractable at all -- a scan -- falls through to
    _raster_content_rect, which bounds the ink in a small rendering of it.
    That costs a page render, so callers sampling many pages can turn it
    off with allow_raster=False and retry only if nothing turned up.

    allow_graphics=False likewise drops the figure pass (see
    _graphic_rects): its get_bboxlog() call is the one step here whose
    cost is unbounded in the page's complexity, so a caller working
    through many pages can stop paying it once it has spent enough.

    Not expected to be perfect -- just usually right for that kind of
    document.
    """
    pw, ph = page.rect.width, page.rect.height
    if pw <= 0 or ph <= 0:
        return None

    blocks = []
    for b in page.get_text("blocks"):
        x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
        if isinstance(text, str):
            if not text.strip():
                continue
            if _is_boilerplate(text):
                continue
        if x1 <= x0 or y1 <= y0:
            continue
        r = fitz.Rect(x0, y0, x1, y1)
        blocks.append((r, r.width * r.height))
    # Figures are weighed by the area of the grid cell they were bucketed
    # into, not by the summed area of the rects in it: a plot's strokes
    # overlap each other many times over, and counting that raw would let
    # one figure outweigh a full page of text and win the body vote alone.
    cell_area = (pw / _GRAPHIC_GRID) * (ph / _GRAPHIC_GRID)
    if allow_graphics:
        for r in _graphic_rects(page):
            blocks.append((r, min(r.width * r.height, cell_area)))
    if not blocks:
        return _raster_content_rect(page) if allow_raster else None

    # Agglomerative clustering by proximity: merge any two groups whose
    # rects are closer than ~3.5% of the page size (comfortably larger
    # than normal inter-line/paragraph spacing, comfortably smaller than a
    # real page margin) until nothing more can merge.
    gap_threshold = max(pw, ph) * 0.035
    clusters = [{"rect": r, "area": a} for r, a in blocks]
    merged = True
    while merged and len(clusters) > 1:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if _rect_gap(clusters[i]["rect"], clusters[j]["rect"]) < gap_threshold:
                    clusters[i] = {
                        "rect": _union_rects([clusters[i]["rect"], clusters[j]["rect"]]),
                        "area": clusters[i]["area"] + clusters[j]["area"],
                    }
                    del clusters[j]
                    merged = True
                    break
            if merged:
                break

    # The body is the biggest group *and* every group of comparable weight.
    # Two substantial groups are a figure and its text, or two columns, or an
    # abstract above the body -- content either way. Under-cropping beats
    # over-cropping (docs/ARCHITECTURE.md); anything genuinely stray is orders
    # of magnitude smaller than the body, not a quarter of it.
    biggest = max(c["area"] for c in clusters)
    body_rect = _union_rects(
        [c["rect"] for c in clusters if c["area"] >= biggest * 0.25]
    )

    return _pad_within_page(body_rect, page.rect)


# ---------- background page rendering ----------
#
# Each page render goes to Qt's global thread pool rather than the GUI
# thread, so turning the page never blocks on rasterization. Results are
# applied per cell the moment they land, rather than the whole grid waiting
# on its slowest page. A small LRU also keeps recently-shown and just-
# ahead-of-where-you-are pages ready, so paging forward often hits cache
# instead of re-rendering at all.

# Cache eviction is by total byte size, not entry count: one entry ranges
# from a thumbnail-sized cell to tens of MB for a full-screen HiDPI page, so
# a fixed entry count either wastes the budget or balloons past a gigabyte.
# The budget itself was an agent's pick, not a measured one -- a later agent
# should feel free to revisit it (note it is per window).
_PIXMAP_CACHE_BUDGET_BYTES = 256 * 1024 * 1024

_RenderResult = namedtuple(
    "_RenderResult", "kind generation cell_index page_idx w h crop_gen dark dpr image error"
)


def _pixmap_bytes(pixmap):
    return pixmap.width() * pixmap.height() * pixmap.depth() // 8


def _dpr_key(dpr):
    # Rounded so float noise can't split what's physically the same scale
    # factor into distinct cache keys.
    return round(dpr * 100)


def _pix_to_qimage(pix, dark):
    """QImage from a fitz pixmap, optionally dark-mode transformed.

    Dark mode inverts each pixel's HSL *lightness* while preserving hue and
    saturation. Addison asked for this after seeing plain RGB inversion turn
    colour figures into photo negatives -- it rotates every hue 180 degrees,
    so blue plots came out orange. The transform reduces to
    v' = v + 255 - max(r,g,b) - min(r,g,b) per channel, because inversion
    (255 - v) followed by an exact 180-degree hue rotation back
    ((max+min) - v, which preserves HSL L and S by construction) composes
    to that single expression. For grayscale pixels max == min == v, so it
    degenerates to plain inversion -- black text still becomes white text.
    The result is always in [0, 255] (v <= max and v >= min), no clamping
    needed.
    """
    if not dark:
        return QImage(
            pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888
        ).copy()
    h, w = pix.height, pix.width
    arr = np.frombuffer(pix.samples, dtype=np.uint8)
    arr = arr.reshape(h, pix.stride)[:, : w * 3].reshape(h, w, 3).astype(np.int16)
    adjust = 255 - arr.max(axis=2, keepdims=True) - arr.min(axis=2, keepdims=True)
    out = (arr + adjust).astype(np.uint8)
    return QImage(out.tobytes(), w, h, w * 3, QImage.Format_RGB888).copy()


def _qround(x):
    """Qt's qRound: round half AWAY from zero. Python's round() is
    half-to-even and disagrees on exact .5 ties, which are common here and
    are exactly where the round trip below decides sharp vs soft."""
    return int(x + 0.5)


# QLabel paints a DPR-tagged pixmap by computing qRound(device / dpr) and
# converting back with qRound(logical * dpr). At fractional DPR that round
# trip can exceed the pixmap's real size, and Qt then resamples the whole
# page -- visibly soft text, seemingly at random. Sizes below are chosen to
# survive the round trip. Full explanation in docs/GOTCHAS.md.

def _render_targets(w, h, dpr):
    """The device-pixel render target for a w x h logical cell:
    qRound(w * dpr), which always survives the round trip above
    (qRound(target / dpr) recovers exactly w for dpr >= 1) and so always
    draws as a sharp 1:1 blit."""
    return max(1, _qround(w * dpr)), max(1, _qround(h * dpr))


def _largest_safe_extent(d, dpr):
    """Largest device-pixel extent <= d that survives QLabel's logical
    round trip (see the comment block above)."""
    logical = _qround(d / dpr)
    safe = _qround(logical * dpr)
    if safe > d:
        safe = _qround((logical - 1) * dpr)
    return max(1, safe)


def _clamp_image_size(image, dpr, target_w, target_h):
    """Trim a rendered page image to round-trip-safe device dimensions.

    _render_targets is safe by construction, but fitz rounds the transformed
    clip outward, so get_pixmap can return 1-2 device pixels more than
    targeted and those sizes are safe only by luck. Cropping back to target
    costs at most ~2 px of edge and guarantees a 1:1 blit."""
    safe_w = min(image.width(), target_w, _largest_safe_extent(image.width(), dpr))
    safe_h = min(image.height(), target_h, _largest_safe_extent(image.height(), dpr))
    if image.width() > safe_w or image.height() > safe_h:
        return image.copy(0, 0, safe_w, safe_h)
    return image


class EncryptedPdfError(Exception):
    """An encrypted document nobody supplied a working password for."""


def _prompt_for_password(path, again):
    """Modal password prompt; None if the user cancels.

    This runs before the main window exists, which is fine: main() builds the
    QApplication first, and a parentless dialog is what a startup prompt
    wants anyway."""
    label = ("Wrong password. Try again:" if again else
             f"{os.path.basename(path)} is password-protected.\n\nPassword:")
    password, ok = QInputDialog.getText(
        None, "PDF Viewer", label, QLineEdit.EchoMode.Password)
    return password if ok else None


def _resolve_password(doc, path, ask):
    """Ask until the password authenticates, or the user gives up.

    Encrypted files need this because MuPDF opens one happily and only fails
    once a page is actually parsed -- so without a check here an encrypted
    PDF used to open a normal-looking window that filled with render errors.

    Files with an empty user password never reach this: MuPDF authenticates
    those itself while opening and leaves needs_pass False."""
    again = False
    while True:
        password = ask(path, again)
        if password is None:
            raise EncryptedPdfError(
                "This PDF is password-protected and no password was given.")
        if doc.authenticate(password):
            return password
        again = True


class _DocumentSource:
    """The document's bytes, read once and held for the reader's lifetime.

    Every fitz.Document -- the main thread's, one per worker thread, and any
    copy window's -- comes from THIS buffer. Once it is up the path is never
    touched again, so the file can be moved, renamed, deleted or rewritten
    under an open reader with no effect.

    Opening by path was fragile in a non-obvious way: MuPDF parses lazily, so
    fitz.open(path) keeps reading from the file all session. An already-open
    Document survives a rename (its fd follows the inode), which is why the
    bug looked intermittent -- but this viewer opens a FRESH Document per
    render thread and per copy window, and those later opens go through the
    path, so the first page rendered on a cold thread after a move died.
    Rewrite-in-place (LaTeX recompiling the paper you are reading) was worse:
    no error, just a Document reading objects at byte offsets that had moved.

    Per-thread Documents stay separate objects, since one Document is not
    safe to render from concurrently, but they all wrap the same buffer.
    PyMuPDF does not copy stream data when the buffer is READ-ONLY -- it
    points MuPDF's fz_stream at it in place -- so N Documents cost one copy
    of the file in total. Read-only is the load-bearing word: a plain
    bytearray is copied per Document. Hence readinto a bytearray while
    loading, published as a read-only memoryview when complete, which also
    keeps peak RAM at one copy rather than the two b"".join would need.

    The read runs in the BACKGROUND: a synchronous slurp would freeze
    startup for tens of seconds on the large scans this is pointed at. The
    reader opens by path and is usable immediately; a daemon thread fills the
    buffer, and Documents handed out afterwards come off it (_generation
    bumps, retiring the per-thread Documents). Until the swap lands, behaviour
    is what it was before this class existed.

    It is CHUNKED WITH A BREATHER rather than one read(), because a single
    huge read monopolises the device queue and every small page read the
    viewer needs lands behind it -- enough to make startup ~8x slower.
    Chunking costs the slurp nothing measurable, so there is nothing to tune
    here; both ends are best-case. Numbers in docs/DEVLOG.md.

    The read comes off a handle opened in __init__ and held, NOT off the
    path, so a move or delete during the load cannot make it fail: the handle
    keeps the inode alive. New opens inside that window are covered too (see
    open()).
    """

    _READ_CHUNK = 1 << 20
    _READ_PAUSE_S = 0.010

    def __init__(self, path, ask_password=None):
        self.path = path
        # MuPDF picks its parser from `filetype` for stream opens (there's no
        # filename to sniff). The extension is right ~always; "pdf" covers
        # oddities like a .pdf.part download name.
        self._filetype = os.path.splitext(path)[1].lstrip(".").lower() or "pdf"
        # Opened now and held for the whole load: this handle, not the path,
        # is what the background read consumes.
        self._fh = open(path, "rb")
        self._data = None       # written exactly once, by _read_all
        self._ready = threading.Event()
        # Bumped when _data lands, so per-thread Documents opened by path
        # during the load get retired and reopened off the buffer.
        self._generation = 0
        self._local = threading.local()
        # Encryption is a property of the FILE, so every Document opened later
        # -- per-thread, copy window, off the buffer or off the path -- has to
        # be unlocked again. Ask once, here, and replay it in _open.
        self._password = None
        probe = fitz.open(path)  # a bad path or a corrupt file raises here, as before
        try:
            if probe.needs_pass:
                self._password = _resolve_password(
                    probe, path, ask_password or _prompt_for_password)
        finally:
            probe.close()
        threading.Thread(target=self._read_all, name="pdf-slurp", daemon=True).start()

    @property
    def ready(self):
        """True once the document is fully RAM-resident."""
        return self._data is not None

    def _read_all(self):
        try:
            size = os.fstat(self._fh.fileno()).st_size
            buf = bytearray(size)
            view = memoryview(buf)
            pos = 0
            while pos < size:
                got = self._fh.readinto(view[pos:pos + self._READ_CHUNK])
                if not got:
                    break        # file shrank mid-read; keep what's really there
                pos += got
                time.sleep(self._READ_PAUSE_S)
            view.release()
            if pos < size:
                del buf[pos:]
            else:
                buf += self._fh.read()   # ...or grew
            # Read-only view: this is what makes PyMuPDF share the buffer
            # across Documents instead of copying it for each one.
            self._data = memoryview(buf).toreadonly()
            self._generation += 1        # ordered after _data: readers see both or neither
        except Exception:
            print(f"pdfviewer: could not buffer {self.path!r}, staying on the file:\n"
                  f"{traceback.format_exc()}", file=sys.stderr)
        finally:
            try:
                self._fh.close()
            except Exception:
                pass
            self._ready.set()

    def open(self):
        """A new independent Document -- off the buffer once it's up."""
        data = self._data
        if data is None:
            try:
                return self._open(self.path)
            except Exception:
                # The path went away mid-load. The held handle still points
                # at the inode, and /proc/self/fd exposes that as a path MuPDF
                # can open even once the name is gone. Failing that, block
                # until the background read hands over the buffer.
                doc = self._open_via_handle()
                if doc is not None:
                    return doc
                self._ready.wait()
                data = self._data
                if data is None:
                    raise
        return self._open(stream=data, filetype=self._filetype)

    def _open(self, *args, **kwargs):
        """fitz.open, plus the password if the document wants one.

        Authentication lives on the Document rather than on the file, so each
        new one starts locked and has to be let in again."""
        doc = fitz.open(*args, **kwargs)
        if doc.needs_pass:
            if self._password is None or not doc.authenticate(self._password):
                doc.close()
                raise EncryptedPdfError(f"Could not unlock {self.path!r}.")
        return doc

    def _open_via_handle(self):
        try:
            return self._open(f"/proc/self/fd/{self._fh.fileno()}")
        except Exception:
            return None   # not Linux, or the read already finished and closed it

    def get(self):
        """The calling worker thread's own Document, created on first use."""
        generation = self._generation
        entry = getattr(self._local, "entry", None)
        if entry is None or entry[0] != generation:
            # Dropping the old Document (rather than closing it) lets
            # refcounting close it once nothing is still reading from it.
            entry = (generation, self.open())
            self._local.entry = entry
        return entry[1]


class _RenderSignals(QObject):
    done = Signal(object)  # _RenderResult


class _RenderTask(QRunnable):
    def __init__(self, doc_source, kind, page_idx, clip_rect, w, h, dpr,
                 generation, cell_index, crop_gen, dark, signals):
        super().__init__()
        self._source = doc_source
        self._kind = kind
        self._page_idx = page_idx
        self._clip_rect = clip_rect
        self._w = w
        self._h = h
        self._dpr = dpr
        self._generation = generation
        self._cell_index = cell_index
        self._crop_gen = crop_gen
        self._dark = dark
        self._signals = signals

    def run(self):
        image = QImage()
        error = None
        try:
            doc = self._source.get()
            page = doc.load_page(self._page_idx)
            target_w, target_h = _render_targets(self._w, self._h, self._dpr)
            zoom_x = target_w / self._clip_rect.width
            zoom_y = target_h / self._clip_rect.height
            zoom = max(min(zoom_x, zoom_y), 0.05)
            matrix = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=matrix, clip=self._clip_rect, alpha=False)
            image = _clamp_image_size(_pix_to_qimage(pix, self._dark), self._dpr, target_w, target_h)
        except Exception:
            image = QImage()
            error = traceback.format_exc()
        self._signals.done.emit(_RenderResult(
            self._kind, self._generation, self._cell_index, self._page_idx,
            self._w, self._h, self._crop_gen, self._dark, self._dpr, image, error,
        ))


# ---------- background search ----------
#
# A full-document search is a few hundred ms even on a short paper, so it
# runs on the thread pool with the same generation-guard shape as rendering.

_SearchResult = namedtuple("_SearchResult", "generation matches")


class _SearchSignals(QObject):
    done = Signal(object)  # _SearchResult


class _SearchTask(QRunnable):
    def __init__(self, doc_source, query, page_count, generation, signals):
        super().__init__()
        self._source = doc_source
        self._query = query
        self._page_count = page_count
        self._generation = generation
        self._signals = signals

    def run(self):
        matches = []
        try:
            doc = self._source.get()
            for i in range(self._page_count):
                rects = sorted(doc.load_page(i).search_for(self._query), key=lambda r: (r.y0, r.x0))
                matches.extend((i, r) for r in rects)
        except Exception:
            print(f"pdfviewer: search failed:\n{traceback.format_exc()}", file=sys.stderr)
        self._signals.done.emit(_SearchResult(self._generation, matches))


# ---------- background content detection ----------
#
# Detection clusters every text block on a page in an O(n^2) loop, which
# across the sampled pages is a visible freeze on a dense document, so it
# runs on the thread pool. The result is the *fractional* union of the
# sampled pages' content rects: fractions let differently-sized pages
# compose, and keep the worker away from main-thread state.
#
# Scans are handled in a second pass, and only if the first found nothing
# anywhere, because detecting them means rasterizing -- and get_pixmap
# holds the GIL for essentially its whole duration. Rasterizing the whole
# sample would freeze startup on a big scan; a scan's margins barely vary
# page to page, so a few pages is enough.
_MAX_RASTER_DETECT_PAGES = 4

# The figure pass is the one part of detection whose cost is unbounded in
# page complexity: a filled-contour plot is tens of thousands of marks, and
# get_bboxlog holds the GIL for much of the time it spends handing them
# over. One such page is fine; an all-figures document would multiply that
# by the whole sample, so once a task has spent this long on figures it
# finishes the rest text-only.
#
# Degrades gently: the result is a union, so pages that did get the figure
# pass still contribute their full extent. The check happens before each
# page, so the first page always gets the figure pass and single-page
# Shift+C is never degraded.
_GRAPHIC_DETECT_BUDGET_S = 0.5

_DetectResult = namedtuple("_DetectResult", "generation fractions")


class _DetectSignals(QObject):
    done = Signal(object)  # _DetectResult


class _ContentDetectTask(QRunnable):
    def __init__(self, doc_source, page_indices, generation, signals):
        super().__init__()
        self._source = doc_source
        self._page_indices = page_indices
        self._generation = generation
        self._signals = signals

    def run(self):
        fractions = None
        try:
            doc = self._source.get()
            fx0, fy0, fx1, fy1 = 1.0, 1.0, 0.0, 0.0
            found_any = False
            graphics_spent = 0.0

            def accumulate(indices, allow_raster):
                nonlocal fx0, fy0, fx1, fy1, found_any, graphics_spent
                for idx in indices:
                    page = doc.load_page(idx)
                    allow_graphics = graphics_spent < _GRAPHIC_DETECT_BUDGET_S
                    started = time.monotonic()
                    rect = detect_content_rect(
                        page, allow_raster=allow_raster,
                        allow_graphics=allow_graphics,
                    )
                    if allow_graphics:
                        # The whole call is charged, not just the figure
                        # pass -- the text pass is negligible beside a dense
                        # page's figures. The raster retry can charge a
                        # render here too; harmless, since a scan has no
                        # vector art and that pass is separately bounded.
                        graphics_spent += time.monotonic() - started
                    if rect is None:
                        continue
                    pr = page.rect
                    fx0 = min(fx0, (rect.x0 - pr.x0) / pr.width)
                    fy0 = min(fy0, (rect.y0 - pr.y0) / pr.height)
                    fx1 = max(fx1, (rect.x1 - pr.x0) / pr.width)
                    fy1 = max(fy1, (rect.y1 - pr.y0) / pr.height)
                    found_any = True

            accumulate(self._page_indices, allow_raster=False)
            if not found_any:
                # Nothing extractable on any sampled page: a scan. Fall back
                # to reading the ink off a rendering of the first few.
                accumulate(self._page_indices[:_MAX_RASTER_DETECT_PAGES],
                           allow_raster=True)
            if found_any:
                fractions = (fx0, fy0, fx1, fy1)
        except Exception:
            print(f"pdfviewer: content detection failed:\n{traceback.format_exc()}", file=sys.stderr)
        self._signals.done.emit(_DetectResult(self._generation, fractions))


class PageCell(QLabel):
    """Displays a single rendered PDF page, always filling its cell.

    Rendering is driven by this cell's own size, not the size at the moment
    a page was assigned. That way, a grid-dimension change or a window
    resize always ends with every visible page re-rendered crisply at
    whatever size it actually ends up occupying.
    """

    def __init__(self, viewer):
        super().__init__()
        self.viewer = viewer
        self.page_idx = None
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(1, 1)
        # So mouseMoveEvent fires even with no button held, to swap in a
        # pointing-hand cursor while hovering a link.
        self.setMouseTracking(True)
        self._update_background()
        self._page_pixmap = None  # full-res QPixmap currently loaded

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(RESIZE_DEBOUNCE_MS)
        self._render_timer.timeout.connect(self._render_full)

        # Drag state, shared by both buttons: which one is down (or None),
        # and the widget-space press/current points for drawing the
        # in-progress zoom box. Text selection can be made of several
        # non-contiguous segments (built with Ctrl-drag), in any mix of
        # panels: self.selections holds the committed ones, self._pending
        # the one currently being dragged (not yet committed).
        self._drag_button = None
        self._drag_start = None
        self._drag_current = None
        self.selections = []
        self._pending = None

        # Single-page slide animation (driven by the viewer's shared clock):
        # while active, paintEvent draws _anim_old_pixmap and the current
        # _page_pixmap at a horizontal offset instead of the plain resting
        # frame QLabel would otherwise draw.
        self._anim_old_pixmap = None
        self._anim_direction = 0
        self._anim_progress = None

    # ---------- left/right-drag: text selection vs. zoom box ----------

    def mousePressEvent(self, event):
        button = event.button()
        # A second mouse button pressed while the first is still held mid-drag
        # triggers the two-button gestures (see _handle_two_button_gesture):
        # right-drag then left = zoom the box into a single page; left-drag
        # then right = content-crop; center held then left = follow the citation
        # under the cursor out to a web search. Checked before the plain-button
        # branches so the chord wins over starting a fresh drag.
        if (self._drag_button is not None and button != self._drag_button
                and self.page_idx is not None
                and button in (Qt.LeftButton, Qt.RightButton)):
            self._handle_two_button_gesture(button)
            event.accept()
            return
        if button == Qt.MiddleButton:
            # Center-press no longer acts immediately: it starts a potential
            # pan (when zoomed in) and defers the decision to release, so a
            # drag can pan the zoomed view and a plain click still follows the
            # link. See mouseMoveEvent (pan) and mouseReleaseEvent (click).
            if self.page_idx is None:
                super().mousePressEvent(event)
                return
            self._drag_button = button
            self._drag_start = event.position().toPoint()
            self._drag_current = self._drag_start
            if self.viewer.crop_rect is not None:
                self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        # Back/Forward and chord+right-click are handled by an application-
        # wide event filter (see PdfGridViewer.eventFilter) instead of here,
        # since they need to survive a grid rebuild that can destroy and
        # replace *this* cell mid-gesture (e.g. chord+scroll changing the
        # column count while the chord button is still held down).
        if self.page_idx is None or button not in (Qt.LeftButton, Qt.RightButton):
            super().mousePressEvent(event)
            return
        self._drag_button = button
        self._drag_start = event.position().toPoint()
        self._drag_current = self._drag_start
        if self._drag_button == Qt.LeftButton:
            self._pending = None
            if not (event.modifiers() & Qt.ControlModifier):
                # No modifier always means "start a new selection" -- clear
                # every panel's existing selection first, so this behaves
                # identically whether the previous selection was in this
                # same panel or a different one. Ctrl held means "continue
                # the existing (possibly non-contiguous) selection" instead,
                # so nothing is cleared in that case.
                self.viewer.clear_all_selections()
        self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        # Keep the viewer's zoom focus on whatever page point is under the
        # cursor, so a cursor-anchored zoom (pinch / Ctrl+wheel / -,=) knows
        # where to zoom toward even between gestures.
        self.viewer.update_zoom_focus(self, event.position().toPoint())
        if self._drag_button is None:
            self._update_hover_cursor(event.position().toPoint())
            super().mouseMoveEvent(event)
            return
        if self._drag_button == Qt.MiddleButton:
            # Center-drag pans the zoomed crop by the incremental cursor delta
            # (a no-op when not zoomed in). Applied per move so the page tracks
            # the cursor; the re-render is coalesced by the same zoom throttle.
            prev = self._drag_current
            self._drag_current = event.position().toPoint()
            delta = self._drag_current - prev
            self.viewer.pan_crop(self, delta.x(), delta.y())
            event.accept()
            return
        self._drag_current = event.position().toPoint()
        if self._drag_button == Qt.LeftButton:
            self._pending = self._compute_selection(self._drag_start, self._drag_current)
        self.update()
        event.accept()

    def _cancel_drag(self):
        self._drag_button = None
        self._drag_start = None
        self._drag_current = None
        self._pending = None
        self.update()

    def _handle_two_button_gesture(self, new_button):
        # Called from mousePressEvent when a second button goes down during a
        # drag. Captures what it needs, cancels the in-progress drag (so the
        # later button releases are no-ops), and hands the actual work to the
        # viewer, which defers any grid rebuild to the event loop.
        page_idx = self.page_idx
        if self._drag_button == Qt.RightButton and new_button == Qt.LeftButton:
            box = QRect(self._drag_start, self._drag_current).normalized()
            moved = (self._drag_current - self._drag_start).manhattanLength() >= 6
            self._cancel_drag()
            if moved:
                self.viewer.zoom_box_to_single_page(page_idx, box, self)
            else:
                self.viewer.collapse_to_single_page_no_zoom(page_idx)
        elif self._drag_button == Qt.LeftButton and new_button == Qt.RightButton:
            self._cancel_drag()
            QTimer.singleShot(0, self.viewer.zoom_to_content_sampled)
        elif self._drag_button == Qt.MiddleButton and new_button == Qt.LeftButton:
            # Center held, then left-click: follow the citation under where the
            # center press landed out to a web search instead of jumping to its
            # target inside the document.
            end = self._drag_current
            self._cancel_drag()
            pt = self._widget_point_to_page_point(end)
            link = self.viewer.link_at(page_idx, pt) if pt is not None else None
            if link is not None:
                self.viewer.follow_link_to_web_search(link, page_idx)
        else:
            # Any other second-button combination during a drag: abandon the
            # in-progress drag cleanly rather than leaving it half-open.
            self._cancel_drag()

    def mouseReleaseEvent(self, event):
        if self._drag_button != event.button() or self._drag_button is None:
            super().mouseReleaseEvent(event)
            return
        button = self._drag_button
        start, end = self._drag_start, event.position().toPoint()
        self._drag_button = None
        self._drag_start = None
        MIN_DRAG_PX = 6
        moved_enough = (end - start).manhattanLength() >= MIN_DRAG_PX
        if button == Qt.MiddleButton:
            # A drag was a pan (already applied live in mouseMoveEvent); a plain
            # click follows the link under the cursor in a new copy, matching the
            # old center-click behaviour.
            self._drag_current = None
            if not moved_enough:
                pt = self._widget_point_to_page_point(end)
                link = self.viewer.link_at(self.page_idx, pt) if pt is not None else None
                if link is not None:
                    self.viewer.follow_link(link, self.page_idx, in_new_copy=True)
            self._update_hover_cursor(end)
            self.update()
            event.accept()
            return
        if button == Qt.RightButton:
            if moved_enough:
                self.viewer.zoom_to_box(self.page_idx, QRect(start, end).normalized(), self)
        else:
            if moved_enough:
                segment = self._compute_selection(start, end)
                if segment is not None:
                    self.selections.append(segment)
            else:
                pt = self._widget_point_to_page_point(end)
                link = self.viewer.link_at(self.page_idx, pt) if pt is not None else None
                if link is not None:
                    self.viewer.follow_link(link, self.page_idx)
            self._pending = None
            self.viewer.on_selection_changed()
        self.update()
        event.accept()

    def _update_hover_cursor(self, widget_pt):
        link = None
        if self.page_idx is not None:
            pt = self._widget_point_to_page_point(widget_pt)
            if pt is not None:
                link = self.viewer.link_at(self.page_idx, pt)
        self.setCursor(Qt.PointingHandCursor if link is not None else Qt.ArrowCursor)

    def _compute_selection(self, start_widget, end_widget):
        p1 = self._widget_point_to_page_point(start_widget)
        p2 = self._widget_point_to_page_point(end_widget)
        if p1 is None or p2 is None:
            return None
        return self.viewer.compute_text_selection(self.page_idx, p1, p2)

    def _widget_point_to_clip_fraction(self, widget_pt):
        # (fx, fy) in [0, 1] of where widget_pt falls within the displayed
        # page image (the current clip), or None if nothing is displayed.
        displayed = self.pixmap()
        if self.page_idx is None or displayed is None or displayed.isNull():
            return None
        disp = displayed.deviceIndependentSize()
        disp_w, disp_h = disp.width(), disp.height()
        if disp_w <= 0 or disp_h <= 0:
            return None
        offset_x = (self.width() - disp_w) / 2
        offset_y = (self.height() - disp_h) / 2
        fx = max(0.0, min(1.0, (widget_pt.x() - offset_x) / disp_w))
        fy = max(0.0, min(1.0, (widget_pt.y() - offset_y) / disp_h))
        return (fx, fy)

    def _widget_point_to_page_point(self, widget_pt):
        frac = self._widget_point_to_clip_fraction(widget_pt)
        if frac is None:
            return None
        page = self.viewer.doc.load_page(self.page_idx)
        clip = self.viewer._effective_clip_rect(page)
        return (clip.x0 + frac[0] * clip.width, clip.y0 + frac[1] * clip.height)

    def _page_rect_to_widget_rect(self, page_rect, clip, offset_x, offset_y, disp_w, disp_h):
        ix0, iy0 = max(page_rect.x0, clip.x0), max(page_rect.y0, clip.y0)
        ix1, iy1 = min(page_rect.x1, clip.x1), min(page_rect.y1, clip.y1)
        if ix1 <= ix0 or iy1 <= iy0:
            return None
        fx0 = (ix0 - clip.x0) / clip.width
        fy0 = (iy0 - clip.y0) / clip.height
        fx1 = (ix1 - clip.x0) / clip.width
        fy1 = (iy1 - clip.y0) / clip.height
        return QRectF(
            offset_x + fx0 * disp_w, offset_y + fy0 * disp_h,
            (fx1 - fx0) * disp_w, (fy1 - fy0) * disp_h,
        )

    def begin_slide(self, old_pixmap, direction):
        self._anim_old_pixmap = old_pixmap
        self._anim_direction = direction
        self._anim_progress = 0.0
        self.update()

    def set_slide_progress(self, progress):
        self._anim_progress = progress
        self.update()

    def end_slide(self):
        if self._anim_progress is not None:
            self._anim_old_pixmap = None
            self._anim_progress = None
            self.update()

    def _is_sliding(self):
        # Note: don't also require _anim_old_pixmap here. A cell that was
        # previously blank (off the start/end of the document) has no old
        # pixmap to slide out, but its new content must still slide in --
        # requiring both used to make that specific case skip straight to
        # super().paintEvent(), popping the final page in instantly instead
        # of animating it.
        return self._anim_progress is not None

    def _draw_slide_layer(self, painter, pixmap, x_offset):
        if pixmap is None or pixmap.isNull():
            return
        logical = pixmap.deviceIndependentSize()
        x = (self.width() - logical.width()) / 2 + x_offset
        y = (self.height() - logical.height()) / 2
        painter.drawPixmap(QPointF(x, y), pixmap)

    def paintEvent(self, event):
        if self._is_sliding():
            # Departing (old) content slides fully off the leading edge
            # while arriving (new) content slides in from the trailing
            # edge -- forward moves everything leftward, backward rightward.
            painter = QPainter(self)
            progress = self._anim_progress
            direction = self._anim_direction
            cell_w = self.width()
            old_offset = -direction * progress * cell_w
            new_offset = direction * (1 - progress) * cell_w
            self._draw_slide_layer(painter, self._anim_old_pixmap, old_offset)
            self._draw_slide_layer(painter, self._page_pixmap, new_offset)
            painter.end()
        else:
            super().paintEvent(event)
        if self.page_idx is None:
            return

        painter = QPainter(self)

        # Search highlights and the committed text selection are anchored
        # to PAGE coordinates, so they need the current clip + displayed
        # image geometry to place on screen; the in-progress zoom box below
        # is transient and already in widget coordinates.
        displayed = self.pixmap()
        if displayed is not None and not displayed.isNull():
            disp = displayed.deviceIndependentSize()
            disp_w, disp_h = disp.width(), disp.height()
            if disp_w > 0 and disp_h > 0:
                offset_x = (self.width() - disp_w) / 2
                offset_y = (self.height() - disp_h) / 2
                page = self.viewer.doc.load_page(self.page_idx)
                clip = self.viewer._effective_clip_rect(page)

                toc_rects = self.viewer.get_toc_text_highlights(self.page_idx)
                if toc_rects:
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(TOC_TEXT_HIGHLIGHT_FILL)
                    for r in toc_rects:
                        wr = self._page_rect_to_widget_rect(r, clip, offset_x, offset_y, disp_w, disp_h)
                        if wr is not None:
                            painter.drawRect(wr)

                matches = self.viewer.get_search_highlights(self.page_idx)
                if matches:
                    painter.setPen(Qt.NoPen)
                    for r, is_current in matches:
                        wr = self._page_rect_to_widget_rect(r, clip, offset_x, offset_y, disp_w, disp_h)
                        if wr is not None:
                            painter.setBrush(SEARCH_CURRENT_FILL if is_current else SEARCH_OTHER_FILL)
                            painter.drawRect(wr)

                painter.setPen(QPen(SELECTION_BORDER, 1))
                painter.setBrush(SELECTION_FILL)
                segments = self.selections if self._pending is None else self.selections + [self._pending]
                for seg in segments:
                    for r in seg["rects"]:
                        wr = self._page_rect_to_widget_rect(r, clip, offset_x, offset_y, disp_w, disp_h)
                        if wr is not None:
                            painter.drawRect(wr)

        if self._drag_button == Qt.RightButton and self._drag_start is not None:
            painter.setPen(QPen(ZOOM_BORDER, 1))
            painter.setBrush(ZOOM_FILL)
            painter.drawRect(QRectF(QRect(self._drag_start, self._drag_current).normalized()))

    def set_page(self, page_idx):
        """Assigns this cell a new page without rendering it -- the viewer
        dispatches the actual render (synchronously, or via a background
        task) and calls apply_rendered_pixmap once it's ready, so changing
        pages never blocks here on PyMuPDF rasterization."""
        self.page_idx = page_idx
        self.selections = []
        self._pending = None
        if page_idx is None:
            self._page_pixmap = None
            self.clear()

    def apply_rendered_pixmap(self, pixmap):
        self._page_pixmap = pixmap
        self.setPixmap(pixmap)

    def show_render_error(self, page_idx):
        # The traceback itself already went to stderr (see _on_render_done)
        # -- this is just enough for the user to notice something's wrong
        # and know what to paste back when reporting it.
        self._page_pixmap = None
        self.clear()
        self.setWordWrap(True)
        self.setText(f"⚠ Failed to render page {page_idx + 1}\n(see terminal output for details)")

    def _update_background(self):
        color = DARK_BACKGROUND_COLOR if self.viewer.dark_mode else BACKGROUND_COLOR
        self.setStyleSheet(f"background-color: {color};")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._page_pixmap is not None:
            # Immediate low-cost feedback while the debounce settles.
            self._apply_scaled()
        if self.page_idx is not None:
            self._render_timer.start()

    def _render_full(self, size_hint=None):
        if self.page_idx is None:
            return
        # Right after a grid change, this widget's own size() may not yet
        # reflect the geometry Qt is about to apply (that lands a moment
        # later via resizeEvent). Accepting a precomputed size_hint lets the
        # caller render at the correct resolution immediately instead of
        # waiting on that round trip -- the resizeEvent debounce below will
        # still self-correct if the real applied size differs at all.
        size = size_hint if size_hint is not None else self.size()
        self._page_pixmap = self.viewer.render_page(self.page_idx, size)
        # render_page already renders at exactly the resolution this size
        # needs, so set it directly: an extra QPixmap.scaled() pass here used
        # to soften every multi-cell grid.
        self.setPixmap(self._page_pixmap)

    def _apply_scaled(self):
        # Cheap interim rescale of the last full-res render, shown only
        # while a live window-resize is being debounced. _render_full above
        # replaces this with a pixel-exact render moments later.
        if self._page_pixmap is None:
            return
        # QPixmap.scaled works in physical pixels and keeps the source's
        # DPR, so scaling to self.size() (logical) used to produce an interim
        # image a DPR factor too small on any monitor with scale > 1.
        dpr = self._page_pixmap.devicePixelRatio()
        # Same round-trip-safe target as the real renders, though this
        # interim is smooth-scaled anyway.
        tw, th = _render_targets(self.width(), self.height(), dpr)
        target = QSize(tw, th)
        scaled = self._page_pixmap.scaled(
            target, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        scaled.setDevicePixelRatio(dpr)
        self.setPixmap(scaled)


class PdfGridViewer(QMainWindow):
    def __init__(self, path, rows, cols, source=None, autocrop=True):
        super().__init__()
        self.path = path
        # Read into RAM once per document and never touched on disk again.
        # Copy windows inherit the buffer rather than re-reading -- a re-read
        # is precisely what fails once the file has moved.
        self._source = source if source is not None else _DocumentSource(path)
        self.doc = self._source.open()
        self.page_count = self.doc.page_count
        # self.doc may have opened by path (the buffer is still loading, or
        # this is a copy made during another window's load). Poll for the
        # handover so the main thread ends up on the buffer too -- the worker
        # threads do their own via _DocumentSource.get's generation check.
        self._buffer_watch = QTimer(self)
        self._buffer_watch.setInterval(BUFFER_WATCH_MS)
        self._buffer_watch.timeout.connect(self._adopt_buffer)
        if not self._source.ready:
            self._buffer_watch.start()
        self.rows = rows
        self.cols = cols
        # index (0-based) of the top-left page currently shown -- resumes
        # from wherever this document was last closed, if anywhere.
        saved_page = _load_last_page(path)
        self.first_page = saved_page if isinstance(saved_page, int) else 0
        self.dark_mode = False
        self._wheel_accum = 0
        self._chord_wheel_accum = 0
        self.chord_held = False  # extra mouse "Forward" button, held like Ctrl/Shift
        self._slide_active = False
        self._slide_clock = QElapsedTimer()
        self._slide_timer = QTimer(self)
        self._slide_timer.setInterval(PAGE_SLIDE_TICK_MS)
        self._slide_timer.timeout.connect(self._on_slide_tick)
        # Coalesces the re-render for a burst of continuous-zoom events (see
        # ZOOM_RENDER_THROTTLE_MS); the crop is updated live, this just paces
        # how often the pages are actually re-rasterized.
        self._zoom_render_timer = QTimer(self)
        self._zoom_render_timer.setSingleShot(True)
        self._zoom_render_timer.setInterval(ZOOM_RENDER_THROTTLE_MS)
        self._zoom_render_timer.timeout.connect(self._do_zoom_render)
        self._last_search_query = ""
        self._search_matches = []  # flat [(page_idx, rect), ...] for the current query, reading order
        self._matches_by_page = {}  # page_idx -> [rect, ...], derived from _search_matches
        self._current_match_index = None  # index into _search_matches that n/N are on
        self._search_highlights_visible = True  # Escape hides highlights without forgetting the query
        # A whole live-search session collapses to one Back target (the page
        # the reader opened search from). This flag is True once that session
        # has an open history slot the search's jumps update in place; any
        # non-search navigation closes it. See _navigate_search.
        self._search_history_open = False
        # Search runs on the thread pool with the same generation-guard
        # shape as rendering, so a query typed over a still-running older
        # search doesn't apply stale results.
        self._search_signals = _SearchSignals()
        self._search_signals.done.connect(self._on_search_done)
        self._search_generation = 0
        self._search_inflight = False
        # Content detection runs on the thread pool too. Its generation is
        # bumped by manual crop changes as well, so a crop made while a
        # detection is running isn't stomped by the stale result.
        self._detect_signals = _DetectSignals()
        self._detect_signals.done.connect(self._on_detect_done)
        self._detect_generation = 0
        # Generation of the detection currently running, or None. Separate
        # from _detect_generation, which manual crops bump too -- see
        # _on_detect_done.
        self._detect_pending_gen = None
        self._words_cache = {}  # page_idx -> get_text("words"), for text selection
        self._links_cache = {}  # page_idx -> get_links(), for link hit-testing
        # Background rendering: each page rasterizes on the thread pool via
        # its own fitz.Document and lands in its cell when ready, rather than
        # blocking the page turn. _render_generation bumps on navigation so a
        # superseded render is discarded instead of clobbering a cell that
        # has moved on; _crop_generation bumps on zoom/crop, since a cached
        # render is only valid for the crop it was made under.
        self._render_signals = _RenderSignals()
        self._render_signals.done.connect(self._on_render_done)
        self._render_generation = 0
        self._crop_generation = 0
        self._pixmap_cache = OrderedDict()  # see _cache_key -> QPixmap, byte-budgeted LRU
        self._pixmap_cache_bytes = 0
        # Connected in showEvent (no window handle yet here). Moving to a
        # monitor with a different scale factor changes this window's DPR,
        # and everything on screen was rendered for the old one; without a
        # re-render the compositor just rescales those pixmaps.
        self._screen_watch_connected = False
        self._jump_animate = True  # set by G/Shift+G, read once by _jump_from_input
        # Navigation throttle: caps how many render batches can ever be
        # in flight at once (at most one), so rapid-fire advance/back
        # requests coalesce into a single render of wherever they end up,
        # instead of each queuing its own background work. See _navigate.
        self._render_inflight = 0
        self._prefetch_inflight = 0
        self._nav_pending = False
        self._last_dispatched_first_page = self.first_page
        # Browser-style jump history around link follows: _history holds
        # visited first_page positions, _history_pos is the index of the
        # current one. Back/Forward move the pointer through it instead of
        # popping, so Forward still works after a Back.
        self._history = []
        self._history_pos = -1
        # Crop box, in the PDF-point space of the page it was drawn on
        # (crop_source_rect is that page's rect). Re-applied to every page as
        # a fraction of its own rect, so one crop is shared across pages.
        self.crop_rect = None
        self.crop_source_rect = None
        # Grid (rows, cols, first_page) saved when the box-zoom gesture
        # collapses the view to a single page, so a later zoom-out/recrop can
        # restore the layout the reader had. See _save_grid_state.
        self._saved_grid = None
        # Last page point the cursor was over, as (page_idx, fx, fy) fractions
        # within that page's clip -- the anchor a cursor zoom pulls toward.
        self._zoom_focus = None
        self._help_visible = False

        self.setWindowTitle(f"PDF Viewer - {path}")

        self._build_grid()
        self._build_overlays()
        self._build_help_view()
        self._build_toc()
        self._install_shortcuts()
        # Installed on the application (not a PageCell) so Back/Forward and
        # chord+right-click keep working even when the button is held
        # across a grid rebuild that destroys and replaces every cell --
        # see the note on set_chord_held/eventFilter below.
        QApplication.instance().installEventFilter(self)

        self.resize(1000, 800)
        self._clamp_first_page()
        self._show_current_pages()
        self._update_status()
        # Open already cropped to auto-detected content, using the sampled
        # (multi-page) variant since it's the more reliable default. A caller
        # that already knows the crop it wants (open_copy) passes
        # autocrop=False: detection here would be both wasted work and a
        # *random* resample, so the copy could land on a different crop than
        # the window it duplicates.
        if autocrop:
            self.zoom_to_content_sampled()

    # ---------- UI construction ----------

    def _build_overlays(self):
        # Floating cards drawn ON TOP of the page grid: children of
        # self.central but never added to any layout, so they overlap the
        # pages instead of displacing them. Positioned by hand in
        # _position_overlays, kept above the cells by _raise_overlays.
        self._status_text = ""
        self._loading = False
        self._busy_frame = 0
        # Only runs while there is background work, so an idle window isn't
        # waking up twice a second to repaint a counter that hasn't changed.
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(BUSY_INTERVAL_MS)
        self._busy_timer.timeout.connect(self._advance_busy_frame)

        # Page counter (with a trailing background-work spinner), always
        # visible, pinned top-left. Transparent to mouse events so a text selection or
        # link click on the page underneath still reaches the cell.
        self.status_overlay = QLabel(self.central)
        self.status_overlay.setStyleSheet(OVERLAY_BOX_STYLE)
        self.status_overlay.setTextFormat(Qt.RichText)
        self.status_overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        # "Go to page" field -- hidden until G (or a click) focuses it, shown
        # as its own translucent card just below the counter while active.
        self.goto_overlay = QWidget(self.central)
        self.goto_overlay.setStyleSheet(OVERLAY_BOX_STYLE)
        goto_layout = QHBoxLayout(self.goto_overlay)
        goto_layout.setContentsMargins(6, 2, 6, 2)
        goto_layout.setSpacing(5)
        goto_label = QLabel("Go to page")
        goto_label.setStyleSheet(OVERLAY_LABEL_STYLE)
        goto_layout.addWidget(goto_label)
        self.page_input = QLineEdit()
        self.page_input.setFixedWidth(60)
        # Click-focus only: it should never grab focus on startup or via Tab,
        # only when clicked or explicitly requested with G.
        self.page_input.setFocusPolicy(Qt.ClickFocus)
        self.page_input.setStyleSheet(FIELD_STYLE)
        self.page_input.returnPressed.connect(self._jump_from_input)
        goto_layout.addWidget(self.page_input)
        self.goto_overlay.setVisible(False)

        # Search field -- hidden until / (or a click) focuses it, shown top-
        # right while active. Goes red (FIELD_STYLE_NO_MATCH) when a completed
        # search found nothing; textChanged clears that the moment it's edited.
        self.search_overlay = QWidget(self.central)
        self.search_overlay.setStyleSheet(OVERLAY_BOX_STYLE)
        search_layout = QHBoxLayout(self.search_overlay)
        search_layout.setContentsMargins(6, 2, 6, 2)
        search_layout.setSpacing(5)
        search_label = QLabel("Search")
        search_label.setStyleSheet(OVERLAY_LABEL_STYLE)
        search_layout.addWidget(search_label)
        self.search_input = QLineEdit()
        self.search_input.setFixedWidth(180)
        # Same rationale as page_input: only focus via click or the / shortcut.
        self.search_input.setFocusPolicy(Qt.ClickFocus)
        # Debounce between a keystroke and the incremental search it triggers,
        # so typing doesn't fire a full-document search on every character.
        # Created before textChanged is wired, since the handler arms it.
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_debounce.timeout.connect(self._run_live_search)
        self.search_input.returnPressed.connect(self._on_search_return)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        search_layout.addWidget(self.search_input)
        self.search_overlay.setVisible(False)
        self._update_search_field_style(no_match=False)

        # Ask field -- hidden until ? (Shift+/) shows it, as a wide
        # translucent card near the top-center. Enter launches an interactive
        # Claude Code session on this PDF seeded with whatever's typed (empty
        # is fine -- Claude is told to stand by); Escape cancels. Like the
        # search box (and unlike go-to-page) it isn't tied to focus, so it
        # stays up until Enter or Escape.
        self.ask_overlay = QWidget(self.central)
        self.ask_overlay.setStyleSheet(OVERLAY_BOX_STYLE)
        ask_layout = QHBoxLayout(self.ask_overlay)
        ask_layout.setContentsMargins(6, 2, 6, 2)
        ask_layout.setSpacing(5)
        ask_label = QLabel("Ask Claude")
        ask_label.setStyleSheet(OVERLAY_LABEL_STYLE)
        ask_layout.addWidget(ask_label)
        self.ask_input = QLineEdit()
        self.ask_input.setFixedWidth(360)
        self.ask_input.setPlaceholderText("optional context for the first prompt -- Enter to launch")
        self.ask_input.setFocusPolicy(Qt.ClickFocus)
        self.ask_input.setStyleSheet(FIELD_STYLE)
        self.ask_input.returnPressed.connect(self._on_ask_return)
        ask_layout.addWidget(self.ask_input)
        self.ask_overlay.setVisible(False)

    def _position_overlays(self):
        # Places the three cards over self.central by hand (they're not in any
        # layout). Cheap enough to call on every status update / resize.
        if not hasattr(self, "status_overlay") or self.central is None:
            return
        margin = 8
        self.status_overlay.adjustSize()
        self.status_overlay.move(margin, margin)
        if self.goto_overlay.isVisible():
            self.goto_overlay.adjustSize()
            self.goto_overlay.move(margin, margin + self.status_overlay.height() + 6)
        if self.search_overlay.isVisible():
            self.search_overlay.adjustSize()
            x = max(margin, self.central.width() - self.search_overlay.width() - margin)
            self.search_overlay.move(x, margin)
        if self.ask_overlay.isVisible():
            self.ask_overlay.adjustSize()
            x = max(margin, (self.central.width() - self.ask_overlay.width()) // 2)
            self.ask_overlay.move(x, margin + self.status_overlay.height() + 6)
        self._raise_overlays()

    def _raise_overlays(self):
        # Rebuilding the grid (a column/row change) re-adds cells on top of
        # these, so the overlays have to be re-raised afterward to stay visible.
        for overlay in (
            getattr(self, "status_overlay", None),
            getattr(self, "goto_overlay", None),
            getattr(self, "search_overlay", None),
            getattr(self, "ask_overlay", None),
        ):
            if overlay is not None:
                overlay.raise_()

    def _show_goto_overlay(self, show):
        self.goto_overlay.setVisible(show)
        if show:
            self._position_overlays()
            self.goto_overlay.raise_()

    def _show_search_overlay(self, show):
        self.search_overlay.setVisible(show)
        if show:
            self._position_overlays()
            self.search_overlay.raise_()

    def _show_ask_overlay(self, show):
        self.ask_overlay.setVisible(show)
        if show:
            self._position_overlays()
            self.ask_overlay.raise_()

    def _update_search_field_style(self, no_match):
        self.search_input.setStyleSheet(FIELD_STYLE_NO_MATCH if no_match else FIELD_STYLE)

    def _build_grid(self):
        self.central = QWidget()
        self.setCentralWidget(self.central)
        self.grid_layout = QGridLayout(self.central)
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_layout.setSpacing(2)
        # Track every row/col index the layout has ever used, so a shrink
        # can explicitly zero out stretch on the freed indices below --
        # QGridLayout never forgets a stretch factor on its own, which is
        # what left dead space behind whenever the grid shrank.
        self._max_rows_ever = 0
        self._max_cols_ever = 0
        self.cells = []
        self._rebuild_cells()
        self._apply_background_theme()

    def _rebuild_cells(self):
        for cell in self.cells:
            self.grid_layout.removeWidget(cell)
            cell.deleteLater()
        self.cells = []

        self._max_rows_ever = max(self._max_rows_ever, self.rows)
        self._max_cols_ever = max(self._max_cols_ever, self.cols)
        for r in range(self._max_rows_ever):
            self.grid_layout.setRowStretch(r, 1 if r < self.rows else 0)
        for c in range(self._max_cols_ever):
            self.grid_layout.setColumnStretch(c, 1 if c < self.cols else 0)

        for r in range(self.rows):
            for c in range(self.cols):
                cell = PageCell(self)
                self.grid_layout.addWidget(cell, r, c)
                self.cells.append(cell)

        # Force geometry to be computed synchronously so the very first
        # render below uses each cell's real final size, not a stale
        # pre-layout default -- this is what was causing the persistent
        # blur right after changing the grid size.
        self.grid_layout.activate()

    def _apply_background_theme(self):
        # The central widget's background shows through the grid spacing
        # between cells -- needs to match each cell's own background or
        # dark-inverted pages would sit inside a bright white grid.
        color = DARK_BACKGROUND_COLOR if self.dark_mode else BACKGROUND_COLOR
        self.central.setStyleSheet(f"background-color: {color};")
        for cell in self.cells:
            cell._update_background()
        # _build_grid applies the theme before the help view exists yet;
        # _build_help_view styles itself for that first call.
        if hasattr(self, "help_view"):
            self._apply_help_theme()

    def _build_help_view(self):
        # Built once and kept detached until Ctrl+H swaps it in as the
        # central widget (see show_help/hide_help) -- takeCentralWidget()
        # doesn't delete whichever widget it removes, so self.central
        # survives being swapped out and back in. Styling comes from
        # _apply_background_theme so opening help in dark mode isn't a
        # white flashbang.
        self.help_view = QTextBrowser()
        self.help_view.setOpenExternalLinks(True)
        self.help_view.setHtml(HELP_HTML)
        self._apply_help_theme()

    def _apply_help_theme(self):
        if self.dark_mode:
            self.help_view.setStyleSheet(
                f"background-color: {DARK_BACKGROUND_COLOR}; color: #d0d0d0;"
            )
        else:
            self.help_view.setStyleSheet(
                f"background-color: {BACKGROUND_COLOR}; color: #1a1a1a;"
            )

    def _build_toc(self):
        # Closed by default (T toggles it) -- most documents here don't
        # need it, and a dock that's just sitting there empty most of the
        # time isn't worth the screen space by default.
        self.toc_dock = QDockWidget("Table of Contents", self)
        self.toc_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.toc_tree = QTreeWidget()
        self.toc_tree.setHeaderHidden(True)
        # All keyboard interaction goes through the Y/U/I/O shortcuts on
        # the main window -- keeping the tree unfocusable means clicking it
        # never steals focus, so J/K page turning (and everything else)
        # keeps working right after a TOC click, and the tree's built-in
        # arrow-key handling can't fight the shortcuts.
        self.toc_tree.setFocusPolicy(Qt.NoFocus)
        self.toc_tree.itemClicked.connect(self._on_toc_item_clicked)
        self.toc_dock.setWidget(self.toc_tree)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.toc_dock)
        self.toc_dock.setVisible(False)
        # Closing the panel (;, T, or its own close button) also drops the
        # in-document header highlight -- it's a TOC-navigation cue, so it
        # shouldn't linger on the page once the TOC is put away.
        self.toc_dock.visibilityChanged.connect(self._on_toc_visibility_changed)
        # Depth (1-based) that U/I paging currently works at; only the in/out
        # keys (O/Y) change it. See the Y/U/I/O block below.
        self._actual_toc_level = 1
        self._toc_highlighted_item = None
        # (page_idx, [rects]) for the yellow header highlight drawn on the page
        # itself when TOC navigation jumps somewhere, or None. See
        # get_toc_text_highlights / _compute_toc_text_highlight.
        self._toc_text_highlight = None
        self._toc_flat = []  # every entry in depth-first reading order
        self._populate_toc()

    def _populate_toc(self):
        toc = self.doc.get_toc()
        self._toc_has_entries = bool(toc)
        if not toc:
            placeholder = QTreeWidgetItem(["This document has no table of contents"])
            placeholder.setFlags(placeholder.flags() & ~Qt.ItemIsSelectable)
            self.toc_tree.addTopLevelItem(placeholder)
            return
        # The detailed form carries each entry's destination, including the
        # 'to' point on the target page -- what lets the in-document header
        # highlight land on the header instead of guessing from the title.
        # Same order and length as the simple list.
        try:
            detailed = self.doc.get_toc(simple=False)
        except Exception:
            detailed = []
        # PyMuPDF's get_toc() is a flat [level, title, page] list (level
        # 1-based, and can jump by more than one, e.g. straight from 1 to
        # 3) -- rebuild the tree by tracking the most recently seen item at
        # each level as that level's potential parent, falling back to the
        # nearest shallower level actually seen so far when a level was
        # skipped over.
        parents = {0: self.toc_tree.invisibleRootItem()}
        for i, (level, title, page) in enumerate(toc):
            item = QTreeWidgetItem([title])
            item.setData(0, Qt.UserRole, page)
            item.setData(0, _TOC_ROLE_DEST_Y, self._dest_y_from_detailed(detailed, i))
            parent_level = max((lv for lv in parents if lv < level), default=0)
            parents[parent_level].addChild(item)
            parents[level] = item
            for lv in [lv for lv in parents if lv > level]:
                del parents[lv]
        # Flatten into depth-first reading order, stamping each item with its
        # depth and flat index. U/I walk this flat list rather than an item's
        # siblings, which is what lets them cross section boundaries. Stamp
        # rather than recompute: PySide hands back a fresh Python wrapper per
        # .child()/.parent() call, so a dict keyed on the wrapper is unsafe.
        self._toc_flat = []

        def _flatten(parent_item, level):
            for i in range(parent_item.childCount()):
                child = parent_item.child(i)
                child.setData(0, _TOC_ROLE_LEVEL, level)
                child.setData(0, _TOC_ROLE_INDEX, len(self._toc_flat))
                self._toc_flat.append(child)
                _flatten(child, level + 1)

        _flatten(self.toc_tree.invisibleRootItem(), 1)
        # Addison wants it fully collapsed on open: an unfolded tree is hard
        # to scan, and O/click unfold exactly the branch being read.

    def _on_toc_item_clicked(self, item, column):
        page = item.data(0, Qt.UserRole)
        if page and page >= 1:
            self._apply_toc_highlight(item)
            self._jump_to_toc_target(page - 1, item.text(0), item.data(0, _TOC_ROLE_DEST_Y))

    def toggle_toc(self):
        self.toc_dock.setVisible(not self.toc_dock.isVisible())

    def _on_toc_visibility_changed(self, visible):
        # When the TOC is hidden, clear the in-document header highlight so it
        # doesn't stay painted on the page with no TOC open to explain it.
        # (Guarded so it's a no-op during construction, before cells exist.)
        if not visible and self._toc_text_highlight is not None:
            self._toc_text_highlight = None
            for cell in self.cells:
                cell.update()

    # ---------- TOC keyboard navigation (Y/U/I/O) ----------
    #
    # All of this behaviour is Addison's spec; treat it as a requirement, not
    # an implementation detail. The vim motion shape of the page-turning home
    # row, one keyboard row up: U/I page up/down, O/Y dive in/out.
    #
    #   U (down) / I (up): step to the next/previous entry in reading order,
    #     skipping anything DEEPER than self._actual_toc_level. That is what
    #     makes them flow across section boundaries instead of dead-ending at
    #     the last sibling. They never descend past the level, so they never
    #     change it.
    #
    #   O (in) / Y (out): move to the first child / the parent, and set
    #     _actual_toc_level to the depth that press landed on -- the level
    #     U/I page at is always "wherever O/Y last put me", never derived.
    #     O expands the entry it dives into; Y folds the one it leaves.
    #
    # Any of the four opens the TOC first if it's closed, then acts. They do
    # nothing on a document with no TOC.

    def _begin_toc_nav(self):
        # Any TOC nav key turns the panel on, then runs. Returns whether the
        # document actually has entries to navigate (a TOC-less document still
        # opens the panel, showing its "no table of contents" placeholder).
        if not self.toc_dock.isVisible():
            self.toc_dock.setVisible(True)
        return getattr(self, "_toc_has_entries", False)

    def _apply_toc_highlight(self, item):
        # Paint the just-jumped-to entry's row yellow, clearing the previous
        # one. Kept separate from Qt's own selection highlight (which we clear
        # below) so the yellow is unambiguously "where TOC navigation is".
        prev = self._toc_highlighted_item
        if prev is not None and prev is not item:
            try:
                prev.setBackground(0, QBrush())
            except RuntimeError:
                pass  # the previous item was destroyed (e.g. tree rebuilt)
        item.setBackground(0, QBrush(TOC_HIGHLIGHT_COLOR))
        self._toc_highlighted_item = item

    def _toc_set_current(self, item):
        # Expand ancestors so the target row is actually visible before
        # highlighting/scrolling to it -- U/I can land on an entry whose
        # parent section is still collapsed.
        ancestor = item.parent()
        while ancestor is not None:
            ancestor.setExpanded(True)
            ancestor = ancestor.parent()
        self._apply_toc_highlight(item)
        self.toc_tree.setCurrentItem(item)
        # Keep "current" (for stepping) but drop the blue selection bar, so
        # the yellow row is the single, unambiguous position indicator.
        self.toc_tree.clearSelection()
        self.toc_tree.scrollToItem(item)
        page = item.data(0, Qt.UserRole)
        if page and page >= 1:
            self._jump_to_toc_target(page - 1, item.text(0), item.data(0, _TOC_ROLE_DEST_Y))

    def _jump_to_toc_target(self, page_idx, title, dest_y):
        # Jump there AND highlight the section header on the page itself (the
        # in-document counterpart of the yellow TOC row). The explicit repaint
        # covers the case where the jump doesn't move the view (the section is
        # already on screen), which wouldn't otherwise re-run paintEvent.
        self._toc_text_highlight = self._compute_toc_text_highlight(page_idx, title, dest_y)
        self.jump_to_page(page_idx + 1)
        for cell in self.cells:
            cell.update()

    def _compute_toc_text_highlight(self, page_idx, title, dest_y):
        rects = self._find_header_rects(page_idx, title, dest_y)
        return (page_idx, rects) if rects else None

    @staticmethod
    def _dest_y_from_detailed(detailed, i):
        # The target y (top-left origin) of TOC entry i's destination, or None.
        if not detailed or i >= len(detailed):
            return None
        entry = detailed[i]
        dest = entry[3] if len(entry) > 3 else None
        if isinstance(dest, dict):
            pt = dest.get("to")
            if pt is not None:
                try:
                    return float(pt.y)
                except Exception:
                    return None
        return None

    def _find_header_rects(self, page_idx, title, dest_y):
        # Locate the section header on the page. Two signals: the entry's
        # destination point and the title text. Prefer the title text (so a
        # running header at the page top isn't mistaken for the real one),
        # and use dest_y to disambiguate when the title occurs more than
        # once. Fall back to the line the destination points at. Give up
        # rather than guess wildly.
        if not (0 <= page_idx < self.page_count):
            return []
        page = self.doc.load_page(page_idx)
        title_rects = self._title_search(page, title)
        if title_rects:
            if dest_y is not None and len(title_rects) > 1:
                return [min(title_rects, key=lambda r: self._rect_y_distance(r, dest_y))]
            if dest_y is not None:
                return title_rects
            # No destination to disambiguate: the topmost occurrence is the
            # most likely header.
            return [min(title_rects, key=lambda r: r.y0)]
        if dest_y is not None:
            return self._header_line_at(page, dest_y)
        return []

    def _title_search(self, page, title):
        # Rects of the title on the page: try the full title, then the title
        # with its leading section-number/label prefix stripped (the page often
        # renders that number in a separate span, breaking a full-string match).
        if not title or not title.strip():
            return []
        seen = set()
        for variant in (title.strip(), _TOC_NUM_PREFIX.sub("", title.strip()).strip()):
            if len(variant) < 2 or variant in seen:
                continue
            seen.add(variant)
            try:
                rects = list(page.search_for(variant))
            except Exception:
                rects = []
            if rects:
                return rects
        return []

    @staticmethod
    def _rect_y_distance(rect, y):
        if rect.y0 <= y <= rect.y1:
            return 0.0
        return min(abs(rect.y0 - y), abs(rect.y1 - y))

    def _header_line_at(self, page, dest_y):
        # The single text line nearest the destination y (the header the
        # outline scrolls to), or [] if nothing is close enough to trust.
        try:
            words = page.get_text("words")
        except Exception:
            words = []
        if not words:
            return []
        lines = {}
        for w in words:
            lines.setdefault((w[5], w[6]), []).append(w)  # key: (block, line)
        best_rect, best_dist, best_h = None, None, None
        for ws in lines.values():
            lx0 = min(w[0] for w in ws)
            ly0 = min(w[1] for w in ws)
            lx1 = max(w[2] for w in ws)
            ly1 = max(w[3] for w in ws)
            dist = 0.0 if ly0 <= dest_y <= ly1 else min(abs(ly0 - dest_y), abs(ly1 - dest_y))
            if best_dist is None or dist < best_dist:
                best_rect, best_dist, best_h = fitz.Rect(lx0, ly0, lx1, ly1), dist, (ly1 - ly0)
        if best_rect is None or best_dist > max(24.0, (best_h or 12.0) * 2):
            return []  # nearest line too far from the destination -> don't trust it
        return [best_rect]

    def get_toc_text_highlights(self, page_idx):
        """Rects to draw the yellow TOC header highlight on page_idx, if any."""
        hl = self._toc_text_highlight
        if hl is not None and hl[0] == page_idx:
            return hl[1]
        return []

    def _toc_current_or_start(self):
        """The tree's current item; if there is none yet (TOC never
        touched), selects and returns the first top-level entry -- so the
        first U/I/O press lands somewhere visible instead of doing
        nothing."""
        item = self.toc_tree.currentItem()
        if item is None:
            item = self.toc_tree.topLevelItem(0)
            if item is not None:
                self._actual_toc_level = 1
                self._toc_set_current(item)
                return None  # this press was consumed by the initial selection
        return item

    def _toc_page(self, step):
        # Walk the flattened reading-order list from the current entry,
        # stopping on the first entry no deeper than _actual_toc_level.
        item = self._toc_current_or_start()
        if item is None:
            return
        idx = item.data(0, _TOC_ROLE_INDEX)
        if idx is None:
            return
        j = idx + step
        while 0 <= j < len(self._toc_flat):
            candidate = self._toc_flat[j]
            if candidate.data(0, _TOC_ROLE_LEVEL) <= self._actual_toc_level:
                self._toc_set_current(candidate)
                return
            j += step

    def toc_nav_next(self):  # U (down)
        if not self._begin_toc_nav():
            return
        self._toc_page(1)

    def toc_nav_previous(self):  # I (up)
        if not self._begin_toc_nav():
            return
        self._toc_page(-1)

    def toc_nav_descend(self):  # O (in)
        if not self._begin_toc_nav():
            return
        item = self._toc_current_or_start()
        if item is None:
            return
        if item.childCount() > 0:
            item.setExpanded(True)
            child = item.child(0)
            self._actual_toc_level = child.data(0, _TOC_ROLE_LEVEL)
            self._toc_set_current(child)
        else:
            # Bottom of this branch: nowhere deeper to dive, but an in/out
            # press always (re)commits the actual level to where it landed --
            # here, this entry's own level.
            self._actual_toc_level = item.data(0, _TOC_ROLE_LEVEL)

    def toc_nav_ascend(self):  # Y (out)
        if not self._begin_toc_nav():
            return
        item = self._toc_current_or_start()
        if item is None:
            return
        if item.childCount() > 0 and item.isExpanded():
            # This section's subsections are open: a first "out" just closes
            # them and drops the actual level to this section's own level,
            # staying put. Only a second "out" (subsections now closed)
            # actually climbs to the parent below.
            item.setExpanded(False)
            self._actual_toc_level = item.data(0, _TOC_ROLE_LEVEL)
            return
        parent = item.parent()
        if parent is None:
            # Top level with nothing open to close: no move, but still commit
            # the actual level to this entry's level.
            item.setExpanded(False)
            self._actual_toc_level = item.data(0, _TOC_ROLE_LEVEL)
            return
        parent.setExpanded(False)
        self._actual_toc_level = parent.data(0, _TOC_ROLE_LEVEL)
        self._toc_set_current(parent)

    def toggle_dark_mode(self):
        self.dark_mode = not self.dark_mode
        self._apply_background_theme()
        self._show_current_pages()

    def _install_shortcuts(self):
        def bind(keys, fn):
            for key in keys:
                sc = QShortcut(QKeySequence(key), self)
                sc.activated.connect(fn)

        # "PageUp"/"PageDown" parse to Key_unknown and bind to nothing; the
        # accepted spelling is "PgUp"/"PgDown" (docs/GOTCHAS.md).
        #
        # Page turning, vim home row: h/l step one page; j/k jump a whole
        # grid row, so a multi-column grid advances non-overlapping.
        bind(["Right", "Down", "Space", "L", "PgDown"], self.advance_forward)
        bind(["Left", "Up", "Backspace", "H", "PgUp"], self.advance_backward)
        bind(["J"], self.advance_forward_row)
        bind(["K"], self.advance_backward_row)
        bind(["Home"], self.jump_to_first)
        bind(["End"], self.jump_to_last)
        bind(["G"], self.focus_jump_input)
        bind(["Shift+G"], self.focus_jump_input_no_anim)
        bind([","], self.decrease_cols)
        bind(["."], self.increase_cols)
        bind(["Shift+,"], self.decrease_rows)
        bind(["Shift+."], self.increase_rows)
        bind(["/"], self.focus_search_input)
        # '?' is NOT a QShortcut: Shift+/ delivers Key_Question *with*
        # ShiftModifier, which a no-modifier QKeySequence("?") never matches
        # on Wayland. Handled in eventFilter instead (docs/GOTCHAS.md).
        bind(["N"], self.search_next)
        bind(["Shift+N"], self.search_previous)
        # Cursor-anchored zoom (also on Ctrl+wheel and trackpad pinch).
        bind(["-"], self.zoom_out_step)
        bind(["=", "+"], self.zoom_in_step)
        # Reset zoom (0, b) / content-crop (c, m); both also restore a grid
        # saved by the box-zoom gesture. Addison asked for Escape to reset
        # zoom as well, which is why that lives at the tail of _on_escape --
        # a second QShortcut on Escape would be ambiguous and fire neither.
        bind(["0", "B"], self.reset_zoom)
        bind(["C", "M"], self.zoom_to_content_sampled)
        # M mirrors C at both shift levels. An unshifted QShortcut("M") does
        # not match Shift+M, so the shifted variant needs its own binding.
        bind(["Shift+C", "Shift+M"], self.zoom_to_content)
        bind(["Ctrl+Q", "Q"], self.close)
        bind(["Escape"], self._on_escape)
        bind(["Alt+Left"], self.history_back)
        bind(["Alt+Right"], self.history_forward)
        bind(["Ctrl+C"], self.copy_selection)
        bind(["Ctrl+N"], self.open_copy)
        bind(["Ctrl+H"], self.show_help)
        bind(["T", ";"], self.toggle_toc)
        bind(["U"], self.toc_nav_next)
        bind(["I"], self.toc_nav_previous)
        bind(["O"], self.toc_nav_descend)
        bind(["Y"], self.toc_nav_ascend)
        bind(["D"], self.toggle_dark_mode)

    # ---------- navigation ----------

    def advance_forward(self):
        self._navigate(self.first_page + 1, self._clamp_first_page_overscroll)

    def advance_backward(self):
        self._navigate(self.first_page - 1, self._clamp_first_page_overscroll)

    def advance_forward_row(self):
        # j: down one whole grid row (self.cols pages).
        self._navigate(self.first_page + self.cols, self._clamp_first_page_overscroll)

    def advance_backward_row(self):
        # k: up one whole grid row.
        self._navigate(self.first_page - self.cols, self._clamp_first_page_overscroll)

    def zoom_in_step(self):
        self.zoom_at_focus(ZOOM_STEP)

    def zoom_out_step(self):
        self.zoom_at_focus(1.0 / ZOOM_STEP)

    def _navigate(self, new_first_page, clamp_fn):
        # Shared by advance_forward/backward, "g"/history jumps, and search.
        self.first_page = new_first_page
        clamp_fn()
        # Update the page range immediately -- it must never wait on a page
        # render (background or not) to reflect where navigation landed, no
        # matter how many of these land back-to-back before any of them
        # finishes rendering.
        self._update_status()
        if self._render_inflight > 0:
            # A previous step's render hasn't landed. Rendering this one too
            # would queue more work behind it, which is what made rapid
            # advancing bog down. Instead just record that we've moved on:
            # when the in-flight batch lands, _on_render_done catches up to
            # wherever first_page drifted to, in one render. A burst of
            # keypresses costs one render, not one each.
            self._nav_pending = True
            return
        self._render_from_last_position(is_catchup=False)

    def _render_from_last_position(self, is_catchup):
        # Renders (and, unless catching up, animates) however far
        # first_page has moved since the last time this actually ran --
        # one call, regardless of how many individual navigation requests
        # accumulated into that move while a previous render was in flight.
        old_first_page = self._last_dispatched_first_page
        self._last_dispatched_first_page = self.first_page
        self._nav_pending = False
        # A catch-up can land while the previous slide is still playing;
        # snap it to completion so the new content isn't drawn through stale
        # slide-layer offsets.
        self._finish_slide_animation()
        delta = self.first_page - old_first_page
        if delta == 0:
            self._show_current_pages()
            return
        if is_catchup:
            # Behind a burst of requests: reach the destination rather than
            # spend the animation's budget on a cosmetic slide for a step
            # that was never really one page. Responsiveness over finishing
            # work already in flight -- see docs/ARCHITECTURE.md.
            self._show_current_pages(prefetch=False)
            return
        direction = 1 if delta > 0 else -1
        old_pixmaps = [cell._page_pixmap for cell in self.cells]
        if delta == direction:
            # An actual one-page move: the "one page short" frame is where
            # the view already was, so reuse those pixmaps rather than
            # re-rendering them.
            pre_final_pixmaps = old_pixmaps
        else:
            # A longer jump: the pre-final frame was never displayed, so
            # synthesize it, letting a big jump land with the same animation
            # instead of snapping.
            pre_final_pixmaps = self._render_pixmaps_for_first_page(self.first_page - direction)
        self._show_current_pages()
        self._start_slide_animation(pre_final_pixmaps, direction)

    def _render_pixmaps_for_first_page(self, first_page):
        # What each cell WOULD show at this first_page, without navigating --
        # used to synthesize the frame a long jump animates from. Only for
        # jumps of more than one page, so a synchronous render is fine, and
        # the prefetch cache often makes it free.
        pixmaps = []
        for i, cell in enumerate(self.cells):
            page_idx = first_page + i
            r, c = divmod(i, self.cols)
            hint = self._cell_size_hint(r, c) or cell.size()
            if 0 <= page_idx < self.page_count:
                key = self._cache_key(page_idx, hint.width(), hint.height())
                cached = self._cache_get(key)
                pixmaps.append(cached if cached is not None else self.render_page(page_idx, hint))
            else:
                pixmaps.append(None)
        return pixmaps

    def _start_slide_animation(self, old_pixmaps, direction):
        # Snap any running slide to completion first, so a rapid second
        # advance starts fresh from the settled state instead of stacking
        # transitions. Responsiveness over in-flight work.
        self._finish_slide_animation()
        for cell, old_pm in zip(self.cells, old_pixmaps):
            cell.begin_slide(old_pm, direction)
        self._slide_active = True
        self._slide_clock.start()
        self._slide_timer.start()

    def _on_slide_tick(self):
        progress = min(1.0, self._slide_clock.elapsed() / PAGE_SLIDE_DURATION_MS)
        for cell in self.cells:
            cell.set_slide_progress(progress)
        if progress >= 1.0:
            self._finish_slide_animation()

    def _finish_slide_animation(self):
        if not self._slide_active:
            return
        self._slide_timer.stop()
        self._slide_active = False
        for cell in self.cells:
            cell.end_slide()

    def jump_to_first(self):
        # Through _navigate (not a bare _show_current_pages) so Home/End
        # get the same render coalescing and animation-state bookkeeping as
        # every other jump, and through history so Alt+Left can undo them.
        self._push_history()
        self._navigate(0, self._clamp_first_page)
        self._record_history_position()

    def jump_to_last(self):
        self._push_history()
        self._navigate(max(0, self.page_count - 1), self._clamp_first_page)
        self._record_history_position()

    def jump_to_page(self, page_number_1_based, animate=True):
        idx = page_number_1_based - 1
        if idx < 0 or idx >= self.page_count:
            return
        self._push_history()
        if animate:
            self._navigate(idx, self._clamp_first_page)
        else:
            self.first_page = idx
            self._clamp_first_page()
            self._update_status()
            self._show_current_pages()
        self._record_history_position()

    def focus_jump_input(self):
        self._jump_animate = True
        # The field lives in a normally-hidden overlay, so it has to be shown
        # before it can take focus.
        self._show_goto_overlay(True)
        self.page_input.setFocus()
        self.page_input.selectAll()

    def focus_jump_input_no_anim(self):
        # Shift+G: same as G, but the jump skips the slide animation.
        self._jump_animate = False
        self._show_goto_overlay(True)
        self.page_input.setFocus()
        self.page_input.selectAll()

    def _jump_from_input(self):
        text = self.page_input.text().strip()
        if text.isdigit():
            self.jump_to_page(int(text), animate=self._jump_animate)
        self._jump_animate = True
        self.page_input.clearFocus()
        self.setFocus()
        self._show_goto_overlay(False)

    def _clamp_first_page(self):
        # "Full grid" clamp: keeps every cell showing a real page, used by
        # absolute navigation (Home/End/go-to-page/grid or zoom changes).
        max_first = max(0, self.page_count - self.rows * self.cols)
        self.first_page = max(0, min(self.first_page, max_first))

    def _clamp_first_page_overscroll(self):
        # Used by single-page advance (arrows/space/j/k/wheel). Addison
        # wanted the grid to slide past either end only as far as the first
        # or last page still being visible: scrolling on into blank space
        # would leave no cue about where you are in the document.
        count_per_grid = self.rows * self.cols
        min_first = -(count_per_grid - 1)
        max_first = self.page_count - 1
        self.first_page = max(min_first, min(self.first_page, max_first))

    # ---------- links & history ----------

    def _get_links(self, page_idx):
        links = self._links_cache.get(page_idx)
        if links is None:
            links = self.doc.load_page(page_idx).get_links()
            self._links_cache[page_idx] = links
        return links

    def link_at(self, page_idx, point):
        """The link dict (as from fitz's get_links()) under a page-space
        point, or None. Later links win on overlap, matching how they'd
        paint if a viewer ever drew them."""
        x, y = point
        hit = None
        for link in self._get_links(page_idx):
            r = link.get("from")
            if r is not None and r.x0 <= x <= r.x1 and r.y0 <= y <= r.y1:
                hit = link
        return hit

    def follow_link(self, link, page_idx, in_new_copy=False):
        kind = link.get("kind")
        # LINK_NAMED covers destinations referenced by name (e.g. LaTeX/
        # hyperref citations, equation/section refs) rather than by a
        # literal page number -- PyMuPDF still resolves these to a concrete
        # 'page' for us, so they behave exactly like LINK_GOTO here. Most
        # internal links in a typical academic PDF are actually this kind.
        if kind in (fitz.LINK_GOTO, fitz.LINK_NAMED):
            target_page = link.get("page")
            if target_page is None or not (0 <= target_page < self.page_count):
                return
            if in_new_copy:
                self._open_linked_copy(self.cells[0], target_page)
            else:
                self._push_history()
                target_first_page = target_page - self._central_cell_offset()
                self._navigate(target_first_page, self._clamp_first_page)
                self._record_history_position()
        elif kind == fitz.LINK_URI:
            uri = link.get("uri")
            if uri:
                QDesktopServices.openUrl(QUrl(uri))

    def follow_link_to_web_search(self, link, page_idx):
        # Center-then-left on a citation looks the cited work up on Google
        # Scholar instead of jumping to the bibliography. The whole raw entry
        # goes to Scholar unparsed -- not a principle, just cheap and hard to
        # get wrong, where splitting out authors and title could fail. An
        # external (URI) link opens as it normally would.
        kind = link.get("kind")
        if kind == fitz.LINK_URI:
            uri = link.get("uri")
            if uri:
                QDesktopServices.openUrl(QUrl(uri))
            return
        if kind not in (fitz.LINK_GOTO, fitz.LINK_NAMED):
            return
        target_page = link.get("page")
        if target_page is None or not (0 <= target_page < self.page_count):
            return
        # Match by the citation's own number first (e.g. "[5]" -> reference
        # [5]) -- this is definitionally correct and independent of whatever
        # the destination point's coordinates mean in this file. Fall back to
        # the destination point only when there's no number to go on.
        number = self._citation_number(link, page_idx)
        text = self._reference_text_at(target_page, link.get("to"), number)
        if not text:
            return
        url = "https://scholar.google.com/scholar?q=" + quote_plus(text[:600])
        QDesktopServices.openUrl(QUrl(url))

    def _citation_number(self, link, source_page_idx):
        # The number of the clicked citation (e.g. "[5]" -> 5), read from the
        # text under the link's clickable rect on the source page. None if
        # there's no number there (an author-year citation, say).
        frm = link.get("from")
        if frm is None:
            return None
        try:
            label = self.doc.load_page(source_page_idx).get_textbox(frm)
        except Exception:
            return None
        if not label:
            return None
        m = re.search(r"\[\s*(\d+)", label) or re.search(r"(\d+)", label)
        return int(m.group(1)) if m else None

    def _reference_text_at(self, page_idx, dest, number=None):
        # The text of the bibliography entry a citation points at. Prefer the
        # citation NUMBER (match "[n]" in the reference list); fall back to the
        # destination point.
        #
        # Point-matching alone is unreliable: a hyperref \bibitem anchor sits
        # slightly ABOVE its entry, so "nearest line" latches onto the tail of
        # the PREVIOUS entry and reads across the boundary -- a "<end of ref N>
        # [N+1] <start of ref N+1>" mash-up. And the anchor's coordinate origin
        # can vary between files, mislocating it entirely. Number-matching
        # sidesteps both. Either way we read line by line from the entry's
        # marker to the next marker, staying within its column.
        if dest is None and number is None:
            return None
        dest_x = dest.x if dest is not None else None
        dest_y = dest.y if dest is not None else None
        page = self.doc.load_page(page_idx)
        lines = []  # (y0, x0, text) for every non-empty text line
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type", 0) != 0:  # skip images
                continue
            for line in block.get("lines", []):
                text = "".join(s.get("text", "") for s in line.get("spans", []))
                if text.strip():
                    x0, y0 = line["bbox"][0], line["bbox"][1]
                    lines.append((y0, x0, text))
        if not lines:
            return None
        lines.sort(key=lambda l: (round(l[0], 1), l[1]))  # reading order

        markers = []  # (line index, entry number)
        for i, l in enumerate(lines):
            mm = _REF_MARKER.match(l[2])
            if mm:
                markers.append((i, int(mm.group(1))))
        if markers:
            # Columns from the marker x-positions (a gap > 40pt starts a new
            # column), so continuation lines and the "next entry" test stay in
            # the entry's column on a 2-column references page.
            col_lefts = []
            for x in sorted(lines[i][1] for i, _ in markers):
                if not col_lefts or x - col_lefts[-1] > 40:
                    col_lefts.append(x)
            start = None
            if number is not None:
                hits = [i for i, n in markers if n == number]
                if hits:
                    start = (hits[0] if dest_y is None else
                             min(hits, key=lambda i: (abs(lines[i][0] - dest_y),
                                                      abs(lines[i][1] - dest_x))))
            if start is None and dest_y is not None:
                # No number match: the entry marker at or just below the anchor
                # (the anchor commonly sits a hair above the entry), nearest
                # first, x breaking column ties.
                idxs = [i for i, _ in markers]
                pool = [i for i in idxs if lines[i][0] >= dest_y - 3.0] or idxs
                start = min(pool, key=lambda i: (abs(lines[i][0] - dest_y),
                                                 abs(lines[i][1] - dest_x)))
            if start is None:
                return None
            ci = min(range(len(col_lefts)), key=lambda k: abs(col_lefts[k] - lines[start][1]))
            col_left = col_lefts[ci]
            col_right = col_lefts[ci + 1] if ci + 1 < len(col_lefts) else float("inf")
            in_col = lambda x0: col_left - 6 <= x0 < col_right - 6
            collected = [lines[start][2]]
            for i in range(start + 1, len(lines)):
                if not in_col(lines[i][1]):
                    continue  # a different column at a similar height -- skip
                if _REF_MARKER.match(lines[i][2]):
                    break  # next entry in this column
                collected.append(lines[i][2])
                if len(collected) >= 15:
                    break
        else:
            # No bracketed markers (e.g. author-year style): needs the point.
            # Start at the line nearest the anchor and extend down only while
            # lines stay tightly spaced (one entry).
            if dest_y is None:
                return None
            start = min(range(len(lines)),
                        key=lambda i: (abs(lines[i][0] - dest_y), abs(lines[i][1] - dest_x)))
            gaps = sorted(lines[i + 1][0] - lines[i][0] for i in range(len(lines) - 1)
                          if lines[i + 1][0] - lines[i][0] > 0)
            med = gaps[len(gaps) // 2] if gaps else 0
            collected = [lines[start][2]]
            prev_y = lines[start][0]
            for i in range(start + 1, len(lines)):
                y0 = lines[i][0]
                if med and (y0 - prev_y) > med * 1.6:
                    break  # paragraph gap -> next entry
                collected.append(lines[i][2])
                prev_y = y0
                if len(collected) >= 15:
                    break
        text = " ".join(" ".join(t.split()) for t in collected)
        return text or None

    def _push_history(self):
        # Records the position being navigated away from, so Back returns to
        # it. Jumping somewhere new after a Back discards what was ahead,
        # like a browser.
        self._search_history_open = False  # a real jump ends any search session
        if self._history_pos < len(self._history) - 1:
            self._history = self._history[: self._history_pos + 1]
        if not self._history or self._history[-1] != self.first_page:
            self._history.append(self.first_page)
            self._history_pos = len(self._history) - 1

    def _record_history_position(self):
        # Jumping to the page already on screen would otherwise append a
        # duplicate entry, costing a "dead" Back press that visibly does
        # nothing.
        if self._history and self._history[-1] == self.first_page:
            self._history_pos = len(self._history) - 1
            return
        self._history.append(self.first_page)
        self._history_pos = len(self._history) - 1

    def history_back(self):
        self._search_history_open = False
        if self._history_pos <= 0:
            return
        self._history_pos -= 1
        self._navigate(self._history[self._history_pos], self._clamp_first_page)

    def history_forward(self):
        self._search_history_open = False
        if self._history_pos >= len(self._history) - 1:
            return
        self._history_pos += 1
        self._navigate(self._history[self._history_pos], self._clamp_first_page)

    def open_copy(self):
        """Ctrl+N: open an exact duplicate of this window (same grid size,
        same page currently being read), independent of this one's
        history/selection/crop state from here on."""
        viewer = PdfGridViewer(self.path, rows=self.rows, cols=self.cols,
                               source=self._source, autocrop=False)
        viewer.dark_mode = self.dark_mode
        viewer._apply_background_theme()
        # Inherit this window's crop rather than detecting a fresh one. The
        # crop is what "same view" mostly means here, and re-detecting would
        # redraw the random page sample -- so an exact duplicate could open
        # at a visibly different zoom. Both rects are plain immutable-by-
        # convention geometry, copied so neither window can edit the other's.
        if self.crop_rect is not None and self.crop_source_rect is not None:
            viewer.crop_rect = fitz.Rect(self.crop_rect)
            viewer.crop_source_rect = fitz.Rect(self.crop_source_rect)
            viewer._crop_generation += 1
        target_page = max(self.first_page, 0)
        viewer.first_page = target_page - viewer._central_cell_offset()
        viewer._clamp_first_page()
        viewer._show_current_pages()
        viewer._update_status()
        self._set_transient_parent(viewer)
        viewer.show()
        _open_windows.append(viewer)
        return viewer

    def _open_linked_copy(self, source_cell, target_page):
        """Center-click on a link: open a single-page copy of this document
        at the link's target, positioned exactly over source_cell (this
        window's top-left page) so the new page visually opens right on top
        of the page the link was clicked from. _align_over only actually
        moves anything under xcb (see its own comment); _set_transient_parent
        is what gives native Wayland an approximate substitute."""
        viewer = PdfGridViewer(self.path, rows=1, cols=1, source=self._source)
        viewer.dark_mode = self.dark_mode
        viewer._apply_background_theme()
        viewer.first_page = target_page
        viewer._clamp_first_page()
        viewer._show_current_pages()
        viewer._update_status()
        self._set_transient_parent(viewer)
        viewer.show()
        viewer._align_over(source_cell)
        _open_windows.append(viewer)
        return viewer

    def _set_transient_parent(self, new_window):
        """Hints to the compositor that new_window "belongs to" this one --
        an ordinary, unprivileged request (unlike absolute positioning,
        which native Wayland refuses outright by protocol design -- see
        the QT_QPA_PLATFORM comment at the top of this file). Most window
        managers, including Mutter, use this to place a new window near
        its parent instead of their generic new-window placement policy
        (typically dead-center of the screen), and to keep it stacked
        above the parent. Not pixel-exact the way _align_over's xcb-only
        positioning is, but it's a real improvement under native Wayland,
        where exact positioning isn't possible at all.

        Must be called before new_window.show(): Wayland only honors the
        parent relationship for initial-placement purposes if it's known
        before the window's first show/commit, not set on it afterward.
        new_window.winId() forces its native platform window to exist
        (without showing it yet) so setTransientParent has something to
        act on this early.

        The relationship is then dropped again on the next event-loop turn,
        once the window is up and the compositor has placed it. Qt closes a
        window's transient children along with it, so a copy that keeps the
        link dies when the window it was opened from is closed -- and these
        copies are meant to outlive their parent, which is the whole point of
        opening one. Placement is decided at first map and survives the
        release; what is given up is the compositor keeping the copy stacked
        above its parent, which is worth less than the copy staying open.
        """
        new_window.winId()
        new_handle = new_window.windowHandle()
        source_handle = self.windowHandle()
        if new_handle is not None and source_handle is not None:
            new_handle.setTransientParent(source_handle)
            QTimer.singleShot(0, lambda: self._release_transient_parent(new_window))

    @staticmethod
    def _release_transient_parent(new_window):
        # The window may have been closed before this ran; a destroyed
        # platform window has no handle and needs nothing released.
        try:
            handle = new_window.windowHandle()
        except RuntimeError:
            return  # the C++ side is already gone
        if handle is not None:
            handle.setTransientParent(None)

    def _align_over(self, source_cell):
        # self.move() below is a no-op under native Wayland: the protocol
        # forbids a client from setting its own window position. It only
        # positions anything under xcb, and is kept as a harmless no-op
        # since _set_transient_parent covers the Wayland case approximately.
        #
        # Let the new window's layout and initial placement settle before
        # measuring anything on it.
        QApplication.processEvents()
        cell = self.cells[0]
        target_pos = source_cell.mapToGlobal(QPoint(0, 0))
        target_size = source_cell.size()
        # How far this window's own top-left sits from its cell's top-left
        # -- window frame plus any layout margins (the grid now fills the
        # window, so this is small) -- measured on THIS window rather than
        # assumed, so it's correct regardless of platform/style quirks.
        offset = cell.mapToGlobal(QPoint(0, 0)) - self.pos()
        self.resize(self.size() + (target_size - cell.size()))
        self.move(target_pos - offset)

    def closeEvent(self, event):
        # Copy windows are held in _open_windows so Qt doesn't collect them;
        # drop the reference here or every copy ever opened stays alive --
        # document, cache and all -- for the life of the process.
        if self in _open_windows:
            _open_windows.remove(self)
        # The app-wide filter would otherwise keep running for this closed
        # window for as long as anything still references it.
        QApplication.instance().removeEventFilter(self)
        _save_last_page(self.path, self.first_page)
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        # The QWindow handle only exists once the window is shown, so the
        # screen-change watch can't be set up in __init__.
        handle = self.windowHandle()
        if handle is not None and not self._screen_watch_connected:
            handle.screenChanged.connect(self._on_screen_changed)
            self._screen_watch_connected = True
        # central now has a real size; place the overlays over it. The
        # singleShot catches the final geometry once the initial layout has
        # fully settled.
        self._position_overlays()
        QTimer.singleShot(0, self._position_overlays)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_overlays()

    def _on_screen_changed(self, screen):
        # A monitor with a different scale factor changes this window's DPR,
        # and everything on screen was rendered for the old one. Re-render at
        # the new ratio; if the screens share a scale factor the DPR-keyed
        # cache makes it free.
        self._show_current_pages()

    def set_chord_held(self, held):
        self.chord_held = held

    def eventFilter(self, obj, event):
        # Handled centrally on the long-lived main window rather than in
        # PageCell: a PageCell can be destroyed and replaced mid-gesture
        # (chord+scroll rebuilds the grid while the chord button is still
        # down), which would otherwise strand its release event and leave
        # chord_held stuck on.
        event_type = event.type()
        # '?' opens the Ask box. Handled here, not as a QShortcut: Shift+/
        # arrives as Key_Question WITH ShiftModifier, which a no-modifier
        # QKeySequence("?") misses on Wayland (docs/GOTCHAS.md). Match on the
        # key alone, and ignore it while a text field has focus so '?' can
        # still be typed.
        if event_type == QEvent.KeyPress and event.key() == Qt.Key_Question:
            if isinstance(obj, QWidget) and obj.window() is self:
                if not isinstance(QApplication.focusWidget(), QLineEdit):
                    self.focus_ask_input()
                    return True
            return super().eventFilter(obj, event)
        # Overlay fields are only shown while active. Reflect focus changes
        # straight into visibility; never consume the event.
        if event_type in (QEvent.FocusIn, QEvent.FocusOut):
            # Go-to-page is transient: it shows and hides with its focus.
            # Addison wanted the search box NOT tied to focus -- it stays up
            # from "/" until Escape, so Enter can hand focus back to the
            # document for n/N and paging without the box vanishing. It is
            # shown and hidden explicitly instead.
            if obj is getattr(self, "page_input", None):
                self._show_goto_overlay(event_type == QEvent.FocusIn)
            return super().eventFilter(obj, event)
        if event_type in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            # Application-level filters see every window's events, and with
            # several windows open Qt calls the most recently installed one
            # first. Without this guard the newest window handled the side
            # buttons for ALL windows -- mouse-Back in an older window
            # re-cropped the newest. Only handle events whose target belongs
            # to this window. (Mouse events also pass through at the QWindow
            # stage; those aren't QWidgets and fall through, so each event is
            # still handled once.)
            if not isinstance(obj, QWidget) or obj.window() is not self:
                return super().eventFilter(obj, event)
            button = event.button()
            if button == Qt.BackButton:
                if event_type == QEvent.MouseButtonPress:
                    self.zoom_to_content_sampled()
                return True
            if button == Qt.ForwardButton:
                self.set_chord_held(event_type == QEvent.MouseButtonPress)
                return True
            if button == Qt.RightButton and event_type == QEvent.MouseButtonPress and self.chord_held:
                self.reset_zoom()
                return True
        return super().eventFilter(obj, event)

    def event(self, ev):
        # Trackpad pinch arrives as a native zoom gesture (where the platform
        # delivers one); value() is a small per-event scale delta, amplified
        # by PINCH_GAIN so a real pinch actually moves the zoom. The re-render
        # is throttled inside zoom_at_focus so a fast pinch stays responsive.
        if ev.type() == QEvent.NativeGesture and ev.gestureType() == Qt.ZoomNativeGesture:
            factor = 1.0 + ev.value() * PINCH_GAIN
            if factor > 0:
                self.zoom_at_focus(factor)
            return True
        return super().event(ev)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            # Ctrl + wheel zooms (cursor-anchored) instead of paging, as in a
            # browser. A wheel notch is 120 units; scale by ZOOM_STEP per
            # notch, smoothly for fractional trackpad deltas.
            delta = event.angleDelta().y()
            if delta != 0:
                self.zoom_at_focus(ZOOM_STEP ** (delta / 120.0))
            event.accept()
            return
        if self.chord_held:
            # Chord + scroll adjusts the column count instead of paging.
            # Addison's binding, and deliberately obscure; he may move it off
            # the extra mouse buttons later.
            self._chord_wheel_accum += event.angleDelta().y()
            step = 120
            while self._chord_wheel_accum <= -step:
                self.increase_cols()
                self._chord_wheel_accum += step
            while self._chord_wheel_accum >= step:
                self.decrease_cols()
                self._chord_wheel_accum -= step
            event.accept()
            return

        # Accumulate so trackpad/smooth-scroll deltas still advance exactly
        # one page per "tick" (a standard wheel tick is 120 units), instead
        # of racing through many pages for one scroll gesture.
        self._wheel_accum += event.angleDelta().y()
        step = 120
        while self._wheel_accum <= -step:
            self.advance_forward()
            self._wheel_accum += step
        while self._wheel_accum >= step:
            self.advance_backward()
            self._wheel_accum -= step
        event.accept()

    # ---------- search ----------
    #
    # Incremental and vim-style: / runs a debounced search as you type and
    # jumps to the first match from where you opened it. The field stays open
    # until Escape, which forgets the query and drops the highlights.

    def focus_search_input(self):
        # Remember where the reader was, so each keystroke's incremental jump
        # lands on the first match from that anchor rather than drifting
        # forward off the previous match.
        self._search_origin_page = self.first_page
        self._search_history_open = False  # this search opens a fresh Back target
        self._show_search_overlay(True)
        self.search_input.setFocus()
        self.search_input.selectAll()

    def focus_ask_input(self):
        # ? (Shift+/): show the Ask box and focus it. Shown before it
        # can take focus, same as the other overlay fields.
        self._show_ask_overlay(True)
        self.ask_input.setFocus()
        self.ask_input.selectAll()

    def _on_ask_return(self):
        # Addison wanted Enter to always launch: an empty box is valid (the
        # session is told to stand by), so the key never refuses. Close and
        # clear the box first so it isn't left hanging.
        context = self.ask_input.text()
        self.ask_input.clearFocus()
        self.setFocus()
        self._show_ask_overlay(False)
        self.ask_input.clear()
        # The path, not the buffer: the helper is a separate process and needs
        # a real file to open. Known gap -- if the document was renamed or
        # moved after opening, this path is stale, and the viewer has no way to
        # learn the new name once _DocumentSource has closed its handle.
        _launch_claude_helper(self.path, context)

    def _exit_ask(self):
        # Escape out: cancel without launching.
        self.ask_input.clear()
        self.ask_input.clearFocus()
        self.setFocus()
        self._show_ask_overlay(False)

    def _on_search_text_changed(self, _text):
        # A red "no matches" field describes the last completed search; the
        # moment the query is edited that's stale, so drop back to normal, and
        # (re)arm the debounce that actually runs the incremental search.
        self._update_search_field_style(no_match=False)
        self._search_debounce.start()

    def _clear_search_state(self):
        self._search_matches = []
        self._matches_by_page = {}
        self._current_match_index = None

    def _run_live_search(self):
        query = self.search_input.text()
        if not query:
            # Field emptied but still open: forget matches and drop
            # highlights, without closing the field.
            self._last_search_query = ""
            self._clear_search_state()
            self._update_search_field_style(no_match=False)
            for cell in self.cells:
                cell.update()
            return
        if query == self._last_search_query:
            return  # results for this exact query already in hand or in flight
        self._last_search_query = query
        self._search_generation += 1
        self._search_inflight = True
        self._refresh_loading_indicator()
        task = _SearchTask(
            self._source, query, self.page_count, self._search_generation, self._search_signals
        )
        QThreadPool.globalInstance().start(task)

    def _on_search_return(self):
        # Commit: the box and its highlights stay up, but focus returns to
        # the document so n/N and paging work again. Escape still closes it.
        self.search_input.clearFocus()
        self.setFocus()

    def _exit_search(self):
        # Escape out: forget the query and matches entirely, drop the
        # highlights, and close the field.
        self._search_debounce.stop()
        # Escape also cancels any search still running. Without this bump the
        # in-flight generation still matches, so _on_search_done accepts the
        # late result, repopulates the matches this just cleared and jumps the
        # view to one -- a navigation the reader cancelled seconds earlier.
        self._search_generation += 1
        self._search_inflight = False
        self._refresh_loading_indicator()
        self.search_input.blockSignals(True)
        self.search_input.clear()
        self.search_input.blockSignals(False)
        self._last_search_query = ""
        self._clear_search_state()
        self._search_highlights_visible = True  # armed for the next search
        self._update_search_field_style(no_match=False)
        self.search_input.clearFocus()
        self.setFocus()
        self._show_search_overlay(False)
        for cell in self.cells:
            cell.update()

    def _on_search_done(self, result):
        if result.generation != self._search_generation:
            return  # superseded by a newer search typed before this one landed
        self._search_inflight = False
        self._refresh_loading_indicator()
        self._search_matches = result.matches
        self._matches_by_page = {}
        for page_idx, rect in self._search_matches:
            self._matches_by_page.setdefault(page_idx, []).append(rect)
        self._current_match_index = None
        if self._search_matches:
            # Field stays open and focused throughout an incremental search.
            self._update_search_field_style(no_match=False)
            self._jump_to_nearest_match()
        else:
            # No matches: leave the field up, focused, and red.
            self._update_search_field_style(no_match=True)
            for cell in self.cells:
                cell.update()

    def _jump_to_nearest_match(self):
        # First match at or after the anchor page the search started from
        # (wrapping to the very first match if the anchor is past all of
        # them), so refining the query keeps jumping from where the reader
        # opened search rather than creeping forward off each prior match.
        origin = getattr(self, "_search_origin_page", self.first_page)
        idx = next((i for i, (p, _) in enumerate(self._search_matches) if p >= origin), 0)
        self._current_match_index = idx
        self._jump_to_current_match()

    def search_next(self):
        if not self._search_matches or self._current_match_index is None:
            return
        self._current_match_index = (self._current_match_index + 1) % len(self._search_matches)
        self._jump_to_current_match()

    def search_previous(self):
        if not self._search_matches or self._current_match_index is None:
            return
        self._current_match_index = (self._current_match_index - 1) % len(self._search_matches)
        self._jump_to_current_match()

    def _jump_to_current_match(self):
        self._search_highlights_visible = True
        page_idx, _ = self._search_matches[self._current_match_index]
        # Put the match in the most central cell rather than the top-left, so
        # it isn't tucked in a corner when several pages are shown. Routed
        # through _navigate so a one-page landing still slides.
        target_first_page = page_idx - self._central_cell_offset()
        self._navigate_search(target_first_page)
        # Stepping between matches on pages already on screen leaves every
        # pixmap unchanged, and setPixmap with an identical pixmap schedules
        # no repaint. The current-match marker is a paintEvent overlay, so
        # repaint explicitly (docs/GOTCHAS.md).
        for cell in self.cells:
            cell.update()

    def _navigate_search(self, target_first_page):
        # Like _navigate, but recorded in Back/Forward history. The whole
        # search session folds into ONE transition: origin -> wherever you end
        # up. The first jump off the origin opens the slot; later jumps
        # (refining, n/N) move the landing in place rather than piling up
        # entries, so one Back returns to where search was opened.
        origin = getattr(self, "_search_origin_page", None)
        self._navigate(target_first_page, self._clamp_first_page)
        landed = self.first_page
        if origin is None:
            return
        if self._search_history_open:
            self._history[self._history_pos] = landed
            return
        if landed == origin:
            return  # hasn't left the origin page yet -- nothing to record
        if self._history_pos < len(self._history) - 1:
            self._history = self._history[: self._history_pos + 1]
        if not self._history or self._history[-1] != origin:
            self._history.append(origin)
        self._history.append(landed)
        self._history_pos = len(self._history) - 1
        self._search_history_open = True

    def _central_cell_offset(self):
        # Row-major index of whichever grid cell is closest to the visual
        # center, ties broken toward the bottom-right.
        center_r = (self.rows - 1) / 2
        center_c = (self.cols - 1) / 2
        best_offset, best_key = 0, None
        for r in range(self.rows):
            for c in range(self.cols):
                key = ((r - center_r) ** 2 + (c - center_c) ** 2, -r, -c)
                if best_key is None or key < best_key:
                    best_key, best_offset = key, r * self.cols + c
        return best_offset

    def get_search_highlights(self, page_idx):
        """[(rect, is_current_match), ...] for the active query on page_idx."""
        if not self._search_highlights_visible:
            return []
        rects = self._matches_by_page.get(page_idx)
        if not rects:
            return []
        current_rect = None
        if self._current_match_index is not None:
            cur_page, cur_rect = self._search_matches[self._current_match_index]
            if cur_page == page_idx:
                current_rect = cur_rect
        return [(r, r == current_rect) for r in rects]

    # ---------- text selection ----------

    def _get_words(self, page_idx):
        words = self._words_cache.get(page_idx)
        if words is None:
            words = self.doc.load_page(page_idx).get_text("words")
            self._words_cache[page_idx] = words
        return words

    def compute_text_selection(self, page_idx, p1, p2):
        """Selected rects + extracted text between page-space points p1, p2.

        Groups words into lines (by MuPDF's own block/line numbering, which
        already follows reading order), finds which line each point falls
        in, and includes: every word on lines strictly between the two,
        plus only the words at-or-past the start point on its line and
        at-or-before the end point on its line -- the same shape every
        text editor's click-drag selection uses, rather than a plain
        rectangle intersection that would ignore reading order.
        """
        words = self._get_words(page_idx)
        if not words:
            return None

        lines = []
        line_index = {}
        for x0, y0, x1, y1, text, block_no, line_no, word_no in words:
            key = (block_no, line_no)
            i = line_index.get(key)
            if i is None:
                i = line_index[key] = len(lines)
                lines.append({"rect": fitz.Rect(x0, y0, x1, y1), "words": []})
            line = lines[i]
            line["rect"] = _union_rects([line["rect"], fitz.Rect(x0, y0, x1, y1)])
            line["words"].append((x0, y0, x1, y1, text))

        # Drop boilerplate lines before hit-testing: a rotated watermark's
        # bounding box can span most of the page height, so a drag through
        # ordinary whitespace snaps onto it and selects far too much.
        lines = [
            line for line in lines
            if not _is_boilerplate(" ".join(w[4] for w in line["words"]))
        ]
        if not lines:
            return None

        def line_of_point(pt):
            # Nearest line by true 2D distance, not vertical distance: a
            # point can sit inside a marginal note's y-range while being far
            # off to its side, where a main-column line should win.
            best_i, best_d = None, None
            for i, line in enumerate(lines):
                d = _point_rect_dist(pt, line["rect"])
                if d == 0.0:
                    return i
                if best_d is None or d < best_d:
                    best_d, best_i = d, i
            return best_i

        i1, i2 = line_of_point(p1), line_of_point(p2)
        if i1 is None or i2 is None:
            return None
        x1, x2 = p1[0], p2[0]
        if i1 > i2 or (i1 == i2 and x1 > x2):
            i1, i2, x1, x2 = i2, i1, x2, x1

        rects, texts = [], []
        for i in range(i1, i2 + 1):
            ws = sorted(lines[i]["words"], key=lambda w: w[0])
            if i1 == i2:
                chosen = [w for w in ws if x1 <= (w[0] + w[2]) / 2 <= x2]
            elif i == i1:
                chosen = [w for w in ws if (w[0] + w[2]) / 2 >= x1]
            elif i == i2:
                chosen = [w for w in ws if (w[0] + w[2]) / 2 <= x2]
            else:
                chosen = ws
            if not chosen:
                continue
            rects.append(_union_rects([fitz.Rect(w[0], w[1], w[2], w[3]) for w in chosen]))
            texts.append(" ".join(w[4] for w in chosen))

        if not rects:
            return None
        return {"rects": rects, "text": "\n".join(texts)}

    def _selection_text(self):
        # Panels are joined with a blank line: a multi-panel selection is one
        # per page, and pasting them run together loses the page break.
        texts = [seg["text"] for cell in self.cells for seg in cell.selections]
        return "\n\n".join(texts) if texts else ""

    def on_selection_changed(self):
        # Unix has two clipboards and Addison asked for each to be used for
        # what it is for: releasing a drag publishes the PRIMARY selection
        # (which is *defined* as "what is selected right now", and is what
        # middle-click pastes), and nothing else. CLIPBOARD is only written by
        # an explicit Ctrl+C, so selecting text here can no longer clobber
        # whatever the reader copied somewhere else.
        text = self._selection_text()
        clipboard = QApplication.clipboard()
        if text and clipboard.supportsSelection():
            clipboard.setText(text, QClipboard.Selection)

    def copy_selection(self):
        # Ctrl+C: the explicit copy, and the only thing that writes CLIPBOARD.
        # A window-level QShortcut fires even while one of the overlay fields
        # has focus, pre-empting that field's own Ctrl+C, so hand it back.
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit) and focused.hasSelectedText():
            focused.copy()
            return
        text = self._selection_text()
        if text:
            QApplication.clipboard().setText(text, QClipboard.Clipboard)

    def clear_all_selections(self):
        for cell in self.cells:
            if cell.selections or cell._pending is not None:
                cell.selections = []
                cell._pending = None
                cell.update()

    def _on_escape(self):
        if self._help_visible:
            self.hide_help()
            return
        if self.search_overlay.isVisible():
            # An open search field owns Escape: forget the query and matches,
            # drop the highlights, close the field.
            self._exit_search()
            return
        if self.ask_overlay.isVisible():
            # An open Ask box owns Escape: cancel without launching.
            self._exit_ask()
            return
        self.setFocus()
        self.clear_all_selections()
        self.clear_search_highlight()
        self.chord_held = False  # safety net if a release event was ever missed
        # With nothing overlaying the grid to dismiss, Escape is a second B/0:
        # drop the crop and restore any grid saved by a box-zoom collapse.
        self.reset_zoom()

    def show_help(self):
        if self._help_visible:
            return
        self.takeCentralWidget()  # detaches self.central without deleting it
        self.setCentralWidget(self.help_view)
        self._help_visible = True

    def hide_help(self):
        if not self._help_visible:
            return
        self.takeCentralWidget()  # detaches self.help_view without deleting it
        self.setCentralWidget(self.central)
        self._help_visible = False
        # self.central (and its overlay children) was hidden while detached;
        # re-place the overlays once its geometry is restored.
        QTimer.singleShot(0, self._position_overlays)

    def clear_search_highlight(self):
        # Hides the highlighting (like vim's :nohlsearch) without forgetting
        # the query itself -- n/N still walk the same matches afterward, and
        # any of them re-shows highlighting immediately.
        if self._search_highlights_visible:
            self._search_highlights_visible = False
            for cell in self.cells:
                cell.update()

    # ---------- grid resizing ----------

    def increase_cols(self):
        self.cols += 1
        self._grid_changed()

    def decrease_cols(self):
        if self.cols > 1:
            self.cols -= 1
            self._grid_changed()

    def increase_rows(self):
        self.rows += 1
        self._grid_changed()

    def decrease_rows(self):
        if self.rows > 1:
            self.rows -= 1
            self._grid_changed()

    def _grid_changed(self):
        self._rebuild_cells()
        self._clamp_first_page()
        self._show_current_pages()
        self._update_status()
        # The freshly created cells stack above the overlays, so lift them
        # back on top.
        self._raise_overlays()

    # ---------- zoom box ----------

    def zoom_to_box(self, page_idx, widget_box, cell):
        crop = self._box_to_crop_rect(page_idx, widget_box, cell)
        if crop is None:
            return  # drag landed entirely in letterboxed margin, or is degenerate
        self.crop_rect = crop
        self.crop_source_rect = self.doc.load_page(page_idx).rect
        self._crop_generation += 1
        self._detect_generation += 1  # a manual crop supersedes any detection still in flight
        self._show_current_pages()
        self._update_status()

    def _box_to_crop_rect(self, page_idx, widget_box, cell):
        # The page-space crop rect for a drawn widget box, or None if it's too
        # small to be a real selection. Computed against the live cell, so it
        # must run before any grid rebuild that would destroy that cell.
        p1 = cell._widget_point_to_page_point(widget_box.topLeft())
        p2 = cell._widget_point_to_page_point(widget_box.bottomRight())
        if p1 is None or p2 is None:
            return None
        x0, x1 = sorted((p1[0], p2[0]))
        y0, y1 = sorted((p1[1], p2[1]))
        current_clip = self._effective_clip_rect(self.doc.load_page(page_idx))
        if (x1 - x0) < current_clip.width * 0.01 or (y1 - y0) < current_clip.height * 0.01:
            return None
        return fitz.Rect(x0, y0, x1, y1)

    # ---------- box-zoom to a single page (right-drag ended with left) ------
    #
    # Collapse the grid to one page, remembering the previous grid so a later
    # zoom-out or recrop brings it back (_take_saved_grid_dims, which every
    # zoom-reset path funnels through). The rebuild is deferred to
    # the event loop because it originates inside a cell's mouse handler and
    # would otherwise be tearing down that very cell mid-event.

    def zoom_box_to_single_page(self, page_idx, widget_box, cell):
        crop = self._box_to_crop_rect(page_idx, widget_box, cell)
        QTimer.singleShot(0, lambda: self._collapse_to_single_page(page_idx, crop))

    def collapse_to_single_page_no_zoom(self, page_idx):
        QTimer.singleShot(0, lambda: self._collapse_to_single_page(page_idx, None))

    def _collapse_to_single_page(self, page_idx, crop):
        self._save_grid_state()
        if crop is not None:
            self.crop_rect = crop
            self.crop_source_rect = self.doc.load_page(page_idx).rect
            self._crop_generation += 1
            self._detect_generation += 1
        self.rows, self.cols = 1, 1
        self.first_page = page_idx
        self._rebuild_cells()
        self._clamp_first_page()
        self._show_current_pages()
        self._update_status()
        self._raise_overlays()

    def _save_grid_state(self):
        # Only the FIRST collapse records the layout, so repeated collapses
        # still restore all the way back to the reader's real grid. Addison
        # specified this restore behaviour.
        if self._saved_grid is None:
            self._saved_grid = (self.rows, self.cols, self.first_page)

    def _take_saved_grid_dims(self):
        # Restore a saved grid and rebuild the (blank) cells, but do NOT
        # render here: the caller renders once, at the final crop, so the
        # collapsed view goes straight to the finished grid with no flash of
        # anything in between. Returns True if a grid was restored.
        if self._saved_grid is None:
            return False
        self.rows, self.cols, self.first_page = self._saved_grid
        self._saved_grid = None
        self._rebuild_cells()
        self._clamp_first_page()
        return True

    def reset_zoom(self):
        # Zoom-out: drop the crop and, if a collapse is pending, return to the
        # saved grid -- rendered once here (there's no async step, so nothing
        # intermediate is drawn).
        self.crop_rect = None
        self.crop_source_rect = None
        self._crop_generation += 1
        self._detect_generation += 1  # same rationale as zoom_to_box
        self._take_saved_grid_dims()
        self._show_current_pages()
        self._update_status()
        self._raise_overlays()

    def _current_page_index(self):
        # first_page can sit outside [0, page_count) after an overscroll
        # (see _clamp_first_page_overscroll). PyMuPDF interprets negative
        # page numbers Python-style, so an unclamped load_page(-2) would
        # silently read from the END of the document.
        return max(0, min(self.first_page, self.page_count - 1))

    def update_zoom_focus(self, cell, widget_pt):
        # Remember which page point the cursor is over, so a cursor-anchored
        # zoom knows where to pull toward even when triggered by a key.
        if cell.page_idx is None:
            return
        frac = cell._widget_point_to_clip_fraction(widget_pt)
        if frac is not None:
            self._zoom_focus = (cell.page_idx, frac[0], frac[1])

    def zoom_at_focus(self, scale):
        # Scale the effective clip about the point under the cursor, storing
        # the result as the crop so every page zooms alike and the grid layout
        # is untouched. Holding the focal point fixed is what makes it feel
        # like zooming toward the cursor. The crop updates on every event
        # (cheap); the re-render is coalesced by _schedule_zoom_render.
        focus = self._zoom_focus
        if focus is not None and 0 <= focus[0] < self.page_count:
            page_idx, ffx, ffy = focus
        else:
            page_idx, ffx, ffy = self._current_page_index(), 0.5, 0.5
        page = self.doc.load_page(page_idx)
        pr = page.rect
        clip = self._effective_clip_rect(page)
        new_w = clip.width / scale
        new_h = clip.height / scale
        # Zoomed out to (or past) the whole page: just drop the crop.
        if new_w >= pr.width and new_h >= pr.height:
            if self.crop_rect is not None:
                self.crop_rect = None
                self.crop_source_rect = None
                self._crop_generation += 1
                self._detect_generation += 1
                self._schedule_zoom_render()
            return
        if new_w < pr.width * 0.02 or new_h < pr.height * 0.02:
            return  # don't zoom into a sliver
        # Zooming out from a non-page-shaped crop (e.g. the content crop pages
        # open with) can grow one dimension past the page before the other --
        # clamp each to the page so the crop never spills outside it.
        new_w = min(new_w, pr.width)
        new_h = min(new_h, pr.height)
        new_x0 = clip.x0 + ffx * clip.width * (1 - 1.0 / scale)
        new_y0 = clip.y0 + ffy * clip.height * (1 - 1.0 / scale)
        # Keep the window inside the page, sliding (not shrinking) it back in
        # if the focal anchor pushed an edge past the page bound.
        new_x0 = min(max(new_x0, pr.x0), pr.x1 - new_w)
        new_y0 = min(max(new_y0, pr.y0), pr.y1 - new_h)
        self.crop_rect = fitz.Rect(new_x0, new_y0, new_x0 + new_w, new_y0 + new_h)
        self.crop_source_rect = pr
        self._crop_generation += 1
        self._detect_generation += 1
        self._schedule_zoom_render()

    def pan_crop(self, cell, dx_widget, dy_widget):
        # Slide the zoom window by a cursor drag. The crop keeps its size and
        # only moves, clamped inside the page. dx/dy are widget pixels,
        # converted via the displayed image's scale. The window slides
        # *opposite* to the drag so the page tracks the cursor.
        if self.crop_rect is None or self.crop_source_rect is None:
            return
        displayed = cell.pixmap()
        if displayed is None or displayed.isNull():
            return
        disp = displayed.deviceIndependentSize()
        disp_w, disp_h = disp.width(), disp.height()
        if disp_w <= 0 or disp_h <= 0:
            return
        src = self.crop_source_rect
        cr = self.crop_rect
        dx = -(dx_widget / disp_w) * cr.width
        dy = -(dy_widget / disp_h) * cr.height
        new_x0 = min(max(cr.x0 + dx, src.x0), src.x1 - cr.width)
        new_y0 = min(max(cr.y0 + dy, src.y0), src.y1 - cr.height)
        if new_x0 == cr.x0 and new_y0 == cr.y0:
            return  # already against the edge in both axes -- nothing to do
        self.crop_rect = fitz.Rect(new_x0, new_y0, new_x0 + cr.width, new_y0 + cr.height)
        self._crop_generation += 1
        self._detect_generation += 1
        self._schedule_zoom_render()

    def _schedule_zoom_render(self):
        # Leading-edge throttle, so a continuous pinch re-renders at a steady
        # cadence instead of once per event, and the final event still lands a
        # render. Superseded renders are dropped by the generation guard.
        if not self._zoom_render_timer.isActive():
            self._zoom_render_timer.start()

    def _do_zoom_render(self):
        # Prefetch skipped: mid-zoom the neighbouring pages' crop keeps
        # changing too, so prefetching them is wasted work.
        self._show_current_pages(prefetch=False)

    def zoom_to_content(self):
        # Shift+C: detected from the current page only. Always run against
        # the page's full un-cropped geometry, so it's a fresh best guess each
        # time rather than compounding with a manual zoom. A pending box-zoom
        # collapse is restored in _on_detect_done with the new crop.
        self._dispatch_content_detect([self._current_page_index()])

    def zoom_to_content_sampled(self):
        # Union over a random sample, as a fraction of each page's own size
        # so differently-sized pages compose. Whichever sampled page had the
        # widest genuine content sets the bound, which under-crops rather
        # than over-crops -- single-page detection instead tends to cut off
        # a page that is mostly figure, or to over-crop one with an abstract
        # above the body.
        #
        # Addison chose both the sample size and the randomness: when one
        # weird page skews the crop, pressing C again redraws the sample and
        # usually escapes it. That re-press is the intended affordance.
        if self.page_count == 0:
            return
        sample_indices = random.sample(range(self.page_count), min(10, self.page_count))
        self._dispatch_content_detect(sample_indices)

    def _dispatch_content_detect(self, page_indices):
        # Detection is a noticeable freeze on dense documents, so it runs on
        # the thread pool and the crop is applied when the result lands.
        # Priority 1, same as visible renders: it invalidates whatever is
        # rendering anyway, so it shouldn't queue behind prefetch.
        self._detect_generation += 1
        self._detect_pending_gen = self._detect_generation
        self._refresh_loading_indicator()
        task = _ContentDetectTask(
            self._source, page_indices, self._detect_generation, self._detect_signals
        )
        QThreadPool.globalInstance().start(task, 1)

    def _on_detect_done(self, result):
        # Retiring the pending marker is keyed to the generation that was
        # dispatched, not to the current one: _detect_generation is bumped by
        # every manual crop change too, so one of those landing mid-detection
        # sends this result down the superseded path below -- and with the
        # marker left standing, the status overlay's spinner would stay
        # lit until some later detection happened to finish.
        if result.generation == self._detect_pending_gen:
            self._detect_pending_gen = None
            self._refresh_loading_indicator()
        if result.generation != self._detect_generation:
            return  # superseded by a newer detection or a manual crop
        # If a box-zoom collapse is pending (recrop-to-return), restore the
        # saved grid now -- once, together with the crop below -- so the
        # collapsed page transitions straight to the finished grid.
        restored = self._take_saved_grid_dims()
        if result.fractions is None:
            # Nothing detectable on any sampled page. Drop the crop anyway:
            # C means "fit the content", so the one thing it must never do is
            # leave the view exactly as it was -- a key that does nothing at
            # all reads as broken, and on a document where detection always
            # comes back empty it would do nothing every single time.
            had_crop = self.crop_rect is not None
            self.crop_rect = None
            self.crop_source_rect = None
            if had_crop or restored:
                self._crop_generation += 1
                self._show_current_pages()
                self._update_status()
            if restored:
                self._raise_overlays()
            return
        ref_page = self.doc.load_page(self._current_page_index())
        r = ref_page.rect
        fx0, fy0, fx1, fy1 = result.fractions
        self.crop_rect = fitz.Rect(
            r.x0 + fx0 * r.width,
            r.y0 + fy0 * r.height,
            r.x0 + fx1 * r.width,
            r.y0 + fy1 * r.height,
        )
        self.crop_source_rect = r
        self._crop_generation += 1
        self._show_current_pages()
        self._update_status()
        if restored:
            self._raise_overlays()

    def _effective_clip_rect(self, page):
        if self.crop_rect is None:
            return page.rect
        src = self.crop_source_rect
        fx0 = (self.crop_rect.x0 - src.x0) / src.width
        fy0 = (self.crop_rect.y0 - src.y0) / src.height
        fx1 = (self.crop_rect.x1 - src.x0) / src.width
        fy1 = (self.crop_rect.y1 - src.y0) / src.height
        r = page.rect
        return fitz.Rect(
            r.x0 + fx0 * r.width,
            r.y0 + fy0 * r.height,
            r.x0 + fx1 * r.width,
            r.y0 + fy1 * r.height,
        )

    # ---------- rendering ----------

    def _show_current_pages(self, prefetch=True):
        # Assign each cell its page, then dispatch renders to the pool. This
        # returns without waiting, and each cell paints the moment its own
        # render lands rather than the grid waiting on its slowest page.
        # _render_generation drops superseded renders; _render_inflight counts
        # this generation's outstanding work so _navigate knows whether to
        # dispatch another batch or coalesce into the running one.
        self._render_generation += 1
        generation = self._render_generation
        self._render_inflight = 0
        # Direct callers (grid/zoom, dark mode, screen changes) bypass
        # _navigate, so sync the navigation bookkeeping here -- this IS a
        # dispatch of first_page. A stale _last_dispatched_first_page made the
        # next j/k compute a huge delta and animate the wrong way.
        self._last_dispatched_first_page = self.first_page
        self._nav_pending = False
        for i, cell in enumerate(self.cells):
            page_idx = self.first_page + i
            valid = 0 <= page_idx < self.page_count
            cell.set_page(page_idx if valid else None)
            if valid:
                r, c = divmod(i, self.cols)
                hint = self._cell_size_hint(r, c) or cell.size()
                self._dispatch_visible_render(page_idx, i, hint, generation)
        if prefetch:
            self._schedule_prefetch()

    def _cell_size_hint(self, row, col):
        # Ask the layout for this cell's real geometry rather than deriving
        # it: Qt distributes leftover pixels unevenly, and a uniform estimate
        # guessed wrong for exactly those cells, rendering them undersized and
        # then stretching them.
        rect = self.grid_layout.cellRect(row, col)
        if rect.width() > 0 and rect.height() > 0:
            return rect.size()
        return None

    def _clip_rect_for_page(self, page_idx):
        page = self.doc.load_page(page_idx)
        return self._effective_clip_rect(page)

    def _dpr(self):
        return self.devicePixelRatioF()

    def _cache_key(self, page_idx, w, h):
        # crop_generation invalidates on zoom/crop change. dark_mode is in
        # the key rather than a generation bump, since both variants are worth
        # keeping cached. DPR is in the key because w/h are logical sizes: the
        # same (w, h) on differently-scaled monitors are different physical
        # resolutions.
        return (page_idx, w, h, self._crop_generation, self.dark_mode, _dpr_key(self._dpr()))

    def _dispatch_visible_render(self, page_idx, cell_index, size, generation):
        dpr = self._dpr()
        w, h = size.width(), size.height()
        key = self._cache_key(page_idx, w, h)
        cached = self._cache_get(key)
        if cached is not None:
            self.cells[cell_index].apply_rendered_pixmap(cached)
            return
        clip_rect = self._clip_rect_for_page(page_idx)
        task = _RenderTask(
            self._source, "visible", page_idx, clip_rect, w, h, dpr,
            generation, cell_index, self._crop_generation, self.dark_mode, self._render_signals,
        )
        self._render_inflight += 1
        self._refresh_loading_indicator()
        QThreadPool.globalInstance().start(task, 1)  # priority: ahead of prefetch

    def _schedule_prefetch(self):
        # Render the page just before the grid and the page just after it,
        # off-screen, so paging further in either direction often finds its
        # next page already sitting in the cache instead of starting a
        # render from scratch.
        #
        # Capped at _prefetch_inflight, like visible renders at
        # _render_inflight: a long scroll otherwise leaves a backlog of
        # prefetch work sharing the pool with visible renders. Priority alone
        # isn't enough -- a low-priority task that has already started still
        # has to finish before it frees a worker.
        if self._prefetch_inflight > 0:
            return
        if not self.cells:
            return
        size = self.cells[0].size()
        w, h = size.width(), size.height()
        if w <= 0 or h <= 0:
            return
        dpr = self._dpr()
        count_per_grid = self.rows * self.cols
        for page_idx in (self.first_page - 1, self.first_page + count_per_grid):
            if not (0 <= page_idx < self.page_count):
                continue
            key = self._cache_key(page_idx, w, h)
            if self._cache_get(key) is not None:
                continue
            clip_rect = self._clip_rect_for_page(page_idx)
            task = _RenderTask(
                self._source, "prefetch", page_idx, clip_rect, w, h, dpr,
                self._render_generation, -1, self._crop_generation, self.dark_mode, self._render_signals,
            )
            self._prefetch_inflight += 1
            QThreadPool.globalInstance().start(task, -1)  # priority: behind visible renders

    def _on_render_done(self, result):
        if result.error is not None:
            print(f"pdfviewer: failed to render page {result.page_idx + 1}:\n{result.error}", file=sys.stderr)
        if result.kind == "visible":
            current_generation = result.generation == self._render_generation
            if current_generation:
                # Only ever decremented for the generation _render_inflight
                # is currently counting -- a stale generation's arrival
                # doesn't correspond to anything it's tracking anymore.
                self._render_inflight -= 1
                self._refresh_loading_indicator()
            if current_generation:
                cell_ok = result.cell_index < len(self.cells)
                cell = self.cells[result.cell_index] if cell_ok else None
                still_wanted = cell is not None and cell.page_idx == result.page_idx
                if still_wanted and result.error is not None:
                    cell.show_render_error(result.page_idx)
                elif still_wanted and not result.image.isNull():
                    pixmap = self._image_to_pixmap(result.image, result.dpr)
                    self._cache_put(self._result_cache_key(result), pixmap)
                    size_now = cell.size()
                    if (result.w, result.h) == (size_now.width(), size_now.height()):
                        cell.apply_rendered_pixmap(pixmap)
                    else:
                        # The cell was resized while this render was in
                        # flight, and the debounced _render_full has often
                        # already repainted it crisply at the new size. Only
                        # use this if the cell is still blank, and kick the
                        # debounce to re-render at the true size.
                        if cell._page_pixmap is None:
                            cell.apply_rendered_pixmap(pixmap)
                        cell._render_timer.start()
            if current_generation and self._render_inflight <= 0 and self._nav_pending:
                # The batch this generation was waiting on has now fully
                # landed, and navigation requests kept arriving while it
                # was in flight -- catch up to wherever first_page has
                # drifted to since, in one more render.
                self._render_from_last_position(is_catchup=True)
        elif result.kind == "prefetch":
            self._prefetch_inflight -= 1
            if not result.image.isNull():
                pixmap = self._image_to_pixmap(result.image, result.dpr)
                self._cache_put(self._result_cache_key(result), pixmap)

    def _result_cache_key(self, result):
        # Built from the result's own fields, not _cache_key: the crop,
        # dark mode, or even DPR may all have changed while this render was
        # in flight, and the cache entry must describe the render that was
        # actually made.
        return (result.page_idx, result.w, result.h, result.crop_gen, result.dark, _dpr_key(result.dpr))

    def _image_to_pixmap(self, image, dpr):
        # dpr is the ratio the render was dispatched with, passed back
        # through the result -- NOT self._dpr() at completion time, which
        # can differ if the window moved to the other monitor mid-render.
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(dpr)
        return pixmap

    def _cache_get(self, key):
        pixmap = self._pixmap_cache.get(key)
        if pixmap is not None:
            self._pixmap_cache.move_to_end(key)
        return pixmap

    def _cache_put(self, key, pixmap):
        old = self._pixmap_cache.pop(key, None)
        if old is not None:
            self._pixmap_cache_bytes -= _pixmap_bytes(old)
        self._pixmap_cache[key] = pixmap
        self._pixmap_cache_bytes += _pixmap_bytes(pixmap)
        # len > 1 so a single pixmap larger than the whole budget still
        # stays cached rather than being evicted the moment it's added.
        while self._pixmap_cache_bytes > _PIXMAP_CACHE_BUDGET_BYTES and len(self._pixmap_cache) > 1:
            _, evicted = self._pixmap_cache.popitem(last=False)
            self._pixmap_cache_bytes -= _pixmap_bytes(evicted)

    def render_page(self, page_idx, target_size):
        # Synchronous main-thread render: a cell being resized live, and the
        # fallback for a multi-page jump's animation frame. Errors can't show
        # the async path's "failed to render" text, so they go to stderr and
        # a blank placeholder is returned -- safe, where an exception escaping
        # a resize or paint path could take down the GUI thread.
        w = max(1, target_size.width())
        h = max(1, target_size.height())
        dpr = self._dpr()
        # This path used to render unconditionally (it only ever WROTE the
        # cache), so every debounced resize settle re-rasterized pages the
        # background pipeline had just rendered at the same size.
        cached = self._cache_get(self._cache_key(page_idx, w, h))
        if cached is not None:
            return cached
        try:
            page = self.doc.load_page(page_idx)
            clip_rect = self._effective_clip_rect(page)
            target_w, target_h = _render_targets(w, h, dpr)
            zoom_x = target_w / clip_rect.width
            zoom_y = target_h / clip_rect.height
            # Contain-fit: render at exactly the resolution that fits the
            # clip in the target box, in physical pixels, so it displays
            # as-is with no further rescale.
            zoom = max(min(zoom_x, zoom_y), 0.05)
            matrix = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=matrix, clip=clip_rect, alpha=False)
            image = _clamp_image_size(_pix_to_qimage(pix, self.dark_mode), dpr, target_w, target_h)
        except Exception:
            print(f"pdfviewer: failed to render page {page_idx + 1}:\n{traceback.format_exc()}", file=sys.stderr)
            image = QImage(int(w * dpr), int(h * dpr), QImage.Format_RGB888)
            image.fill(Qt.gray)
            pixmap = QPixmap.fromImage(image)
            pixmap.setDevicePixelRatio(dpr)
            # Deliberately not cached: now that this path READS the cache,
            # caching the placeholder would pin the gray box until the next
            # crop/DPR change instead of retrying on the next call.
            return pixmap
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(dpr)
        self._cache_put(self._cache_key(page_idx, w, h), pixmap)
        return pixmap

    # ---------- status ----------

    def _update_status(self):
        # Clamp to the actually-visible pages: overscroll can put first_page
        # outside [0, page_count) while still keeping one real page on
        # screen. Addison's format -- no "Pages", "of" collapsed to a bare
        # slash, e.g. "3-4/120".
        first = max(self.first_page, 0) + 1
        last = min(self.first_page + self.rows * self.cols - 1, self.page_count - 1) + 1
        self._status_text = f"{first}-{last}/{self.page_count}"
        self._render_status()

    def _render_status(self):
        # Composes the top-left overlay: page range plus background-work spinner.
        if not hasattr(self, "status_overlay"):
            return
        # Suffix, not prefix: the counter is left-anchored, so anything put
        # after the numbers leaves them exactly where they were whether or not
        # background work is running.
        busy = ""
        if self._loading:
            frame = BUSY_FRAMES[self._busy_frame % len(BUSY_FRAMES)]
            busy = f' <span style="color:#5aa9ff">{frame}</span>'
        self.status_overlay.setText(self._status_text + busy)
        self._position_overlays()
        # setText() only schedules a repaint, and a steady stream of input
        # events (a fast scroll) can defer it indefinitely even though the
        # text is already correct. Addison asked for the counter to stay as
        # current as possible -- he reads it while scrolling to see what page
        # he is on -- so force the repaint rather than letting Qt coalesce it.
        self.status_overlay.repaint()

    def _refresh_loading_indicator(self):
        # The spinner exists to explain why the UI is occasionally
        # unresponsive. Prefetch doesn't count -- it's anticipatory work
        # nobody is waiting on, and would flicker "loading" while the reader
        # sits still.
        # The document slurp DOES count: on a big scan it runs for tens of
        # seconds, and the spinner is the only sign the file is still being
        # taken into RAM (pages render normally throughout).
        self._loading = (self._render_inflight > 0 or self._search_inflight
                         or self._detect_pending_gen is not None
                         or not self._source.ready)
        if self._loading:
            if not self._busy_timer.isActive():
                # Restart the cycle from the first frame, so a short burst of
                # work always looks the same rather than picking up wherever
                # the last one stopped.
                self._busy_frame = 0
                self._busy_timer.start()
        else:
            self._busy_timer.stop()
        self._render_status()

    def _advance_busy_frame(self):
        self._busy_frame += 1
        self._render_status()

    def _adopt_buffer(self):
        """Move this window's own Document onto the in-RAM buffer once the
        background read lands (see _DocumentSource). Same bytes either way,
        so nothing on screen changes and no cache needs invalidating -- this
        just drops the last handle that was still tied to the file."""
        if not self._source.ready:
            return
        self._buffer_watch.stop()
        self.doc = self._source.open()
        self._refresh_loading_indicator()


def parse_args():
    parser = argparse.ArgumentParser(description="Minimal grid PDF viewer")
    parser.add_argument("pdf", help="Path to the PDF file to open")
    parser.add_argument("--rows", type=int, default=1, help="Initial number of rows")
    parser.add_argument("--cols", type=int, default=2, help="Initial number of columns")
    return parser.parse_args()


def main():
    args = parse_args()
    app = QApplication(sys.argv)
    try:
        viewer = PdfGridViewer(args.pdf, rows=max(1, args.rows), cols=max(1, args.cols))
    except Exception as exc:
        # A bad path, a non-PDF file, a corrupt PDF, or an encrypted one the
        # password prompt didn't unlock all raise here (from the read + probe
        # open in _DocumentSource) -- without this, that's a raw Python
        # traceback in the terminal instead of a clean message.
        QMessageBox.critical(None, "PDF Viewer", f"Couldn't open {args.pdf!r}:\n\n{exc}")
        sys.exit(1)
    viewer.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
