#!/usr/bin/env python3
"""Offscreen regression tests for the viewer.

Runs the viewer under Qt's headless "offscreen" platform and drives its
features directly (and via synthesized key/mouse events), asserting on the
resulting state. Not a pixel test -- it can't judge how things look -- but it
catches crashes and logic regressions in navigation, zoom/crop, the box-zoom
collapse + restore, search, and TOC behaviour.

    python tests/test_pdfviewer.py  # or: venv/bin/python tests/test_pdfviewer.py

Exits non-zero if any check fails.
"""
import os
import re
import sys
import tempfile
import threading
import time

# The suite sits in tests/, so the repo root -- which holds pdfviewer.py and the
# viewer package -- has to go on the path before those imports resolve.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Isolate the last-read-page persistence into a throwaway dir, so tests don't
# read or pollute the real ~/.local/state/pdfviewer/last_page.json (which would
# make first_page-dependent checks depend on prior runs).
os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="pv_test_state_")

import fitz
import numpy as np
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import (Qt, QPoint, QPointF, QSize, QEvent, QEventLoop,
                            QThreadPool, QTimer)
from PySide6.QtGui import QShortcut, QImage, QPixmap, QMouseEvent, QClipboard
from PySide6.QtTest import QTest

import pdfviewer
from viewer import constants, detect, document, integrations, render, tasks, window

APP = QApplication.instance() or QApplication(["test"])

_failures = []


def check(label, cond):
    print(("  ok  " if cond else "FAIL  ") + label)
    if not cond:
        _failures.append(label)


# Every background task in the viewer goes through the global QThreadPool
# (capped to one thread in viewer/__init__.py); the only other worker is the
# document "pdf-slurp" thread, which is a plain threading.Thread.
_POOL = QThreadPool.globalInstance()
_PUMP_LOOP = QEventLoop()


def _timer_pending():
    """True if any live widget has an armed QTimer.

    Debounces and watchdogs run from 8 ms up to BUSY_INTERVAL_MS (500 ms), far
    longer than the idle streak below, so an armed timer has to count as work
    outstanding -- otherwise pump() returns while the result it was waiting for
    is still one timeout away. A repeating timer that is always armed simply
    costs pump() its early exit, which is slow but never wrong.
    """
    for w in APP.topLevelWidgets():
        for t in w.findChildren(QTimer):
            if t.isActive():
                return True
    return False


def _work_outstanding(handled):
    """True if anything might still deliver a result to the GUI thread."""
    return (handled                              # events were just processed
            or _POOL.activeThreadCount() > 0     # a render/search/detect task
            or threading.active_count() > 1)     # a document slurp thread


def pump(n=40, quiet=10):
    """Spin the event loop so background work can land.

    Returns as soon as the loop has been idle -- nothing processed, no pool
    task running, no slurp thread -- for `quiet` consecutive iterations, and
    otherwise keeps going for the full `n`. The idle streak, rather than a
    single idle iteration, is what makes the early exit safe: a task that has
    been queued but not yet picked up shows up as idle for an instant, and a
    caller waiting on a QTimer that has not fired yet still gets the whole
    budget. Raising `quiet` above `n` restores the old unconditional spin.
    """
    idle = 0
    for _ in range(n):
        if _work_outstanding(_PUMP_LOOP.processEvents(QEventLoop.AllEvents)):
            idle = 0
        else:
            idle += 1
            if idle >= quiet:
                # Scanning the widget tree for armed timers is the expensive
                # check, so it runs only here, once a streak has already gone
                # by -- not on every idle iteration.
                if not _timer_pending():
                    return
                idle = 0
        time.sleep(0.003)


# ---------- test documents ----------

def _make_toc_pdf(path):
    """40-page, 3-level TOC, with precise destination points on the header
    lines plus a repeating running header and body mentions of each title
    (to exercise header-highlight disambiguation)."""
    doc = fitz.open()
    for i in range(40):
        pg = doc.new_page()
        pg.insert_text((72, 40), "Running Header  Introduction", fontsize=8)
        pg.insert_text((72, 140), f"Section body page {i + 1} lorem ipsum " * 2, fontsize=11)
    toc = []
    dests = {}
    pg = 1
    for s in range(1, 3):
        title = f"{s} Section"
        toc.append([1, title, pg, {"kind": fitz.LINK_GOTO, "to": fitz.Point(72, 126), "zoom": 0}])
        dests[title] = pg
        pg += 1
        for ss in range(1, 3):
            t2 = f"{s}.{ss} Sub"
            toc.append([2, t2, pg, {"kind": fitz.LINK_GOTO, "to": fitz.Point(72, 126), "zoom": 0}])
            pg += 1
            for sss in range(1, 3):
                t3 = f"{s}.{ss}.{sss} Subsub"
                toc.append([3, t3, pg, {"kind": fitz.LINK_GOTO, "to": fitz.Point(72, 126), "zoom": 0}])
                pg += 1
    # Put a matching header line at y~126 on each destination page.
    for lvl, title, page, dest in toc:
        doc.load_page(page - 1).insert_text((72, 140), title, fontsize=16)
    doc.set_toc(toc)
    doc.save(path)
    doc.close()


def _make_plain_pdf(path, pages=20):
    doc = fitz.open()
    for i in range(pages):
        pg = doc.new_page()
        pg.insert_text((72, 100), f"Page {i + 1} the quick brown fox", fontsize=14)
    doc.save(path)
    doc.close()


def _make_encrypted_pdf(path, password, pages=12):
    """A password-protected PDF, otherwise identical to _make_plain_pdf."""
    doc = fitz.open()
    for i in range(pages):
        pg = doc.new_page()
        pg.insert_text((72, 100), f"Page {i + 1} the quick brown fox", fontsize=14)
    doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256,
             user_pw=password, owner_pw=password)
    doc.close()


def _make_citation_pdf(path):
    """Two pages: a body with a citation link to a *middle* entry on a
    references page of tightly-packed, multi-line, numbered entries, with the
    link anchor sitting a few points ABOVE the entry's first line (as hyperref
    \\bibitem anchors do). Exercises center-click link follow, center-drag pan,
    and the center+left citation -> web-search path -- including that the search
    text is the target entry alone, not a mash-up across an entry boundary."""
    doc = fitz.open()
    page0 = doc.new_page(width=400, height=600)
    page0.insert_text((50, 100), "See citation", fontsize=12)
    page0.insert_text((186, 100), "[2]", fontsize=12)  # sits inside the link rect
    p1 = doc.new_page(width=400, height=600)
    p1.insert_text((50, 40), "References", fontsize=13)
    entries = [
        ("[1] Ada Lovelace and Charles Babbage. The Analytical Engine.", "    Memoirs of Computing, 1843."),
        ("[2] Grace Hopper and John Mauchly. Compiler design methods.", "    Proceedings of Firsts, 1952."),
        ("[3] Alan Turing. Computing machinery and intelligence.", "    Mind LIX, 1950."),
    ]
    y = 120
    tops = []
    for marker, cont in entries:
        tops.append(y - 8)  # approx top of the marker line
        p1.insert_text((50, y), marker, fontsize=10)
        p1.insert_text((50, y + 13), cont, fontsize=10)
        y += 34
    # Re-fetch page 0: adding page 1 can invalidate the earlier handle. Anchor a
    # few points above entry [2]'s first line -- the real "nearest line lands on
    # the previous entry" failure mode.
    doc.load_page(0).insert_link({
        "kind": fitz.LINK_GOTO, "from": fitz.Rect(180, 92, 210, 108),
        "page": 1, "to": fitz.Point(50, tops[1] - 4),
    })
    doc.save(path)
    doc.close()


# The scan's body occupies this fraction of each page; the crop detected
# from the raster should land on it.
SCAN_BODY = (0.15, 0.12, 0.85, 0.88)


def _make_scanned_pdf(path, pages=4):
    """A scan: every page is one full-page greyscale image and there is no
    text layer at all, so text-block detection sees an empty page. Includes
    the two things that defeat a naive ink bbox -- a solid dark scanner-bed
    band down the left edge, and a speck of dirt hugging the right edge,
    well clear of the body."""
    W, H = 850, 1100
    x0, y0, x1, y1 = SCAN_BODY
    doc = fitz.open()
    for i in range(pages):
        arr = np.full((H, W), 250, dtype=np.uint8)  # paper, not pure white
        for y in range(int(H * y0), int(H * y1), 22):  # lines of "text"
            arr[y:y + 9, int(W * x0):int(W * x1)] = 20
        arr[:, :6] = 10               # scanner-bed band
        arr[200:260, W - 3:] = 170    # edge dirt
        pm = fitz.Pixmap(fitz.csGRAY, W, H, arr.tobytes(), 0)
        pg = doc.new_page(width=612, height=792)
        pg.insert_image(pg.rect, stream=pm.tobytes("png"))
    doc.save(path)
    doc.close()


def _make_figure_pdf(path, pages=4):
    """Pages whose top half is a vector figure and bottom half is text,
    separated by more than the clustering gap. Nothing but the caption is
    visible to get_text("blocks")."""
    doc = fitz.open()
    for i in range(pages):
        pg = doc.new_page(width=612, height=792)
        for k in range(60):  # a plot: many separate strokes
            xk = 120 + k * 6
            pg.draw_line(fitz.Point(xk, 90), fitz.Point(xk, 90 + 200 - k * 2))
        pg.insert_text((120, 330), f"Figure {i + 1}: a plot", fontsize=9)
        for k in range(12):
            pg.insert_text((90, 430 + k * 20), "body text line " * 3, fontsize=11)
    doc.save(path)
    doc.close()


DENSE_MARKS = 24000


def _make_dense_figure_pdf(path):
    """One page whose figure is DENSE_MARKS separate filled rects -- a
    filled-contour/mesh plot, as produced by matplotlib or pgfplots, which
    is what made content-crop crawl on vilar--subcell.pdf (64929 marks on
    its worst page).

    The marks are written straight into the content stream: page.draw_rect
    commits a whole Shape per call and would take minutes for this many,
    while one raw `re f` per mark builds the same thing in ~0.1s. Note it
    has to be one operator per mark -- a single Shape with 24000 rects in
    it merges into ONE fill-path, which is not the case under test."""
    doc = fitz.open()
    pg = doc.new_page(width=612, height=792)
    pg.insert_text((120, 430), "Figure 1: a dense plot", fontsize=9)
    # Content-stream operators are in PDF's native bottom-left origin,
    # unlike the page methods above (PyMuPDF flips y for those), so the
    # figure's y is mirrored to put it in the page's top half.
    ops = [b"q 0.2 0.4 0.8 rg"]
    for row in range(120):
        for col in range(200):
            ops.append(b"%.1f %.1f 1.2 1.2 re f"
                       % (120 + col * 1.5, 792 - (90 + row * 1.5) - 1.2))
    ops.append(b"Q")
    xref = pg.get_contents()[0]
    doc.update_stream(xref, pg.read_contents() + b"\n" + b"\n".join(ops))
    doc.save(path, deflate=True)
    doc.close()


def _make_image_figure_pdf(path, pages=2):
    """A figure page whose figure is a raster IMAGE rather than vector art,
    with only a caption line of text under it. Text-block detection sees
    the caption alone, so the image has to come in through the graphics
    pass -- which it only does if the bboxlog kind is spelled the way
    MuPDF actually emits it ("fill-image", not "image")."""
    W, H = 300, 200
    arr = np.full((H, W), 90, dtype=np.uint8)
    png = fitz.Pixmap(fitz.csGRAY, W, H, arr.tobytes(), 0).tobytes("png")
    doc = fitz.open()
    for i in range(pages):
        pg = doc.new_page(width=612, height=792)
        pg.insert_image(fitz.Rect(100, 80, 500, 340), stream=png)
        # Caption close enough under the image to fall inside the
        # clustering gap, the way a real caption sits.
        pg.insert_text((100, 358), f"Figure {i + 1}: a photograph", fontsize=9)
    doc.save(path)
    doc.close()


SCRATCH = os.environ.get("TMPDIR", "/tmp")
TOC_PDF = os.path.join(SCRATCH, "test_pv_toc.pdf")
PLAIN_PDF = os.path.join(SCRATCH, "test_pv_plain.pdf")
CITATION_PDF = os.path.join(SCRATCH, "test_pv_citation.pdf")
SCAN_PDF = os.path.join(SCRATCH, "test_pv_scan.pdf")
FIGURE_PDF = os.path.join(SCRATCH, "test_pv_figure.pdf")
DENSE_PDF = os.path.join(SCRATCH, "test_pv_dense.pdf")
IMAGE_FIGURE_PDF = os.path.join(SCRATCH, "test_pv_image_figure.pdf")
_make_toc_pdf(TOC_PDF)
_make_plain_pdf(PLAIN_PDF)
_make_citation_pdf(CITATION_PDF)
_make_scanned_pdf(SCAN_PDF)
_make_figure_pdf(FIGURE_PDF)
_make_dense_figure_pdf(DENSE_PDF)
_make_image_figure_pdf(IMAGE_FIGURE_PDF)


def new_viewer(path, rows=1, cols=1):
    v = window.PdfGridViewer(path, rows=rows, cols=cols)
    v.resize(1000, 800)
    v.show()
    pump()
    return v


def toc_current(v):
    it = v.toc_tree.currentItem()
    return it.text(0) if it else None


# ---------- tests ----------

def test_shortcuts_bound():
    v = new_viewer(TOC_PDF, 1, 2)
    keys = {s.key().toString() for s in v.findChildren(QShortcut)}
    for k in ["H", "L", "J", "K", "M", "B", "-", "=", "+", ";", "T", "/", "0", "C",
              "Ctrl+C"]:
        check(f"shortcut bound: {k!r}", k in keys)
    v.close()


def test_paging():
    v = new_viewer(TOC_PDF, 1, 2)
    v.first_page = 10
    v._show_current_pages()
    pump()
    QTest.keyClick(v, Qt.Key_H); pump()
    check("H pages back one", v.first_page == 9)
    QTest.keyClick(v, Qt.Key_L); pump()
    check("L pages forward one", v.first_page == 10)
    QTest.keyClick(v, Qt.Key_J); pump()
    check("J pages down one row (cols=2 -> +2)", v.first_page == 12)
    QTest.keyClick(v, Qt.Key_K); pump()
    check("K pages up one row", v.first_page == 10)
    QTest.keyClick(v, Qt.Key_Home); pump()
    check("Home -> first page", v.first_page == 0)
    QTest.keyClick(v, Qt.Key_End); pump()
    # End fills the grid (full-grid clamp), so the last page is visible in the
    # grid rather than necessarily pinned to the first cell.
    last_visible = v.first_page <= v.page_count - 1 <= v.first_page + v.rows * v.cols - 1
    check("End shows the last page", last_visible)
    v.close()


def test_status_format():
    v = new_viewer(TOC_PDF, 1, 2)
    v.first_page = 0
    v._update_status()
    # startswith, not endswith: the busy indicator is appended after the range.
    check("status overlay format 'first-last/total'", v.status_overlay.text().startswith("1-2/40"))
    v.close()


def test_grid_resize():
    v = new_viewer(TOC_PDF, 1, 1)
    QTest.keyClick(v, Qt.Key_Period); pump()   # more cols
    check("'.' increases cols", v.cols == 2)
    QTest.keyClick(v, Qt.Key_Comma); pump()    # fewer cols
    check("',' decreases cols", v.cols == 1)
    v.close()


def test_cursor_zoom():
    v = new_viewer(PLAIN_PDF, 1, 1)
    v.reset_zoom(); pump()
    full = v._effective_clip_rect(v.doc.load_page(0)).width
    v._zoom_focus = (0, 0.5, 0.5)
    v.zoom_in_step(); pump()
    c1 = v.crop_rect
    check("zoom in sets a crop", c1 is not None)
    ratio = full / c1.width if c1 else 0
    check("one zoom-in step ~= ZOOM_STEP", abs(ratio - constants.ZOOM_STEP) < 0.02)
    for _ in range(30):
        v.zoom_out_step(); pump(4)
    check("zooming all the way out clears the crop", v.crop_rect is None)
    v.close()


def test_zoom_out_from_content_crop_stays_in_page():
    # Regression: zooming out from a non-page-shaped (content) crop must not
    # produce a crop wider/taller than the page.
    v = new_viewer(TOC_PDF, 1, 1)
    pump(60)  # let the startup content-crop settle
    v._zoom_focus = (v.first_page, 0.5, 0.5)
    pr = v.doc.load_page(v._current_page_index()).rect
    v.zoom_out_step(); pump()
    if v.crop_rect is not None:
        check("zoom-out crop within page width", v.crop_rect.width <= pr.width + 0.5)
        check("zoom-out crop within page height", v.crop_rect.height <= pr.height + 0.5)
    else:
        check("zoom-out dropped crop (also fine)", True)
        check("zoom-out dropped crop (also fine)", True)
    v.close()


def test_collapse_and_restore():
    v = new_viewer(PLAIN_PDF, 2, 2)
    cell = v.cells[0]
    # Right-drag a box then click left -> collapse to a single zoomed page.
    cell._drag_button = Qt.RightButton
    cell._drag_start = QPoint(120, 120)
    cell._drag_current = QPoint(320, 420)
    cell._handle_two_button_gesture(Qt.LeftButton)
    pump()
    check("collapse -> 1x1 grid", (v.rows, v.cols) == (1, 1))
    check("collapse sets a crop", v.crop_rect is not None)
    check("collapse saved the old grid", v._saved_grid == (2, 2, 0))
    # Reset zoom restores the grid, single render, no zoomed-in intermediate.
    events = []
    orig = v._show_current_pages
    v._show_current_pages = lambda *a, **k: (events.append(v.crop_rect is not None), orig(*a, **k))[1]
    v.reset_zoom(); pump()
    v._show_current_pages = orig
    check("reset restores 2x2 grid", (v.rows, v.cols) == (2, 2))
    check("reset clears crop + saved grid", v.crop_rect is None and v._saved_grid is None)
    check("reset renders once, zoomed-out (no crop)", events == [False])
    v.close()


def test_recrop_after_collapse_single_render():
    v = new_viewer("journal.pdf" if False else PLAIN_PDF, 2, 2)
    cell = v.cells[0]
    cell._drag_button = Qt.RightButton
    cell._drag_start = QPoint(120, 120)
    cell._drag_current = QPoint(320, 420)
    cell._handle_two_button_gesture(Qt.LeftButton)
    pump()
    renders = []
    orig = v._show_current_pages
    v._show_current_pages = lambda *a, **k: (renders.append(1), orig(*a, **k))[1]
    v.zoom_to_content_sampled()  # recrop
    pump(120)
    v._show_current_pages = orig
    check("recrop after collapse restores grid", (v.rows, v.cols) == (2, 2))
    check("recrop renders exactly once (no full-page flash)", len(renders) == 1)
    v.close()


def test_search_incremental_commit_escape():
    v = new_viewer(PLAIN_PDF, 1, 1)
    QTest.keyClick(v, Qt.Key_Slash); pump()
    check("'/' opens + focuses search", v.search_overlay.isVisible() and v.search_input.hasFocus())
    QTest.keyClicks(v.search_input, "quick")
    for _ in range(120):
        APP.processEvents(); time.sleep(0.003)
        if v._search_matches:
            break
    check("live search finds matches without Enter", len(v._search_matches) > 0)
    mi = v._current_match_index
    QTest.keyClick(v.search_input, Qt.Key_Return); pump()
    check("Enter keeps box open", v.search_overlay.isVisible())
    check("Enter hands focus back (box unfocused)", not v.search_input.hasFocus())
    QTest.keyClick(v, Qt.Key_N); pump()
    check("'n' advances match (not typed into box)", v._current_match_index != mi and v.search_input.text() == "quick")
    QTest.keyClick(v, Qt.Key_Escape); pump()
    check("Escape closes + forgets search", not v.search_overlay.isVisible() and v._last_search_query == "" and not v._search_matches)
    v.close()


def test_search_no_match_red():
    v = new_viewer(PLAIN_PDF, 1, 1)
    QTest.keyClick(v, Qt.Key_Slash); pump()
    QTest.keyClicks(v.search_input, "zzqqxx")
    for _ in range(120):
        APP.processEvents(); time.sleep(0.003)
    check("no-match query -> red field", "e24d4d" in v.search_input.styleSheet())
    check("no-match keeps box open", v.search_overlay.isVisible())
    v._exit_search(); pump()
    v.close()


def test_search_escape_cancels_inflight_result():
    # Escaping the search box must also cancel the search still running behind
    # it. Before the generation bump in _exit_search, the in-flight generation
    # still matched, so a late result repopulated the matches Escape had just
    # cleared and navigated the view to one of them.
    v = new_viewer(PLAIN_PDF, 1, 1)
    QTest.keyClick(v, Qt.Key_Slash); pump()
    QTest.keyClicks(v.search_input, "quick")
    for _ in range(120):
        APP.processEvents(); time.sleep(0.003)
        if v._search_matches:
            break
    check("search ran before escape", len(v._search_matches) > 0)
    # The generation a search dispatched *now* would carry, captured before
    # Escape so the late result below looks exactly like one still in flight.
    inflight_gen = v._search_generation
    start_page = v.first_page
    QTest.keyClick(v, Qt.Key_Escape); pump()
    late = tasks._SearchResult(inflight_gen, [(6, fitz.Rect(72, 72, 200, 90))])
    v._on_search_done(late); pump()
    check("late result after Escape is discarded", not v._search_matches)
    check("late result after Escape does not navigate", v.first_page == start_page)
    check("Escape cleared the in-flight flag", not v._search_inflight)
    v.close()


def test_copy_window_inherits_crop():
    # Ctrl+N claims to be an exact duplicate. Content detection is a *random*
    # page sample, so re-running it in the copy could legitimately produce a
    # different crop -- the copy has to inherit the parent's instead.
    v = new_viewer(PLAIN_PDF, 1, 2)
    v.crop_rect = fitz.Rect(50, 60, 300, 400)
    v.crop_source_rect = v.doc.load_page(0).rect
    v._crop_generation += 1
    v._show_current_pages(); pump()
    copy = v.open_copy(); pump()
    check("copy inherits the parent's crop", copy.crop_rect == v.crop_rect)
    check("copy inherits the crop's source rect", copy.crop_source_rect == v.crop_source_rect)
    check("copy keeps the parent's grid", (copy.rows, copy.cols) == (v.rows, v.cols))
    # A copy of an uncropped window stays uncropped rather than detecting one.
    v2 = new_viewer(PLAIN_PDF, 1, 1)
    v2.reset_zoom(); pump()
    copy2 = v2.open_copy(); pump(80)
    check("copy of an uncropped window stays uncropped", copy2.crop_rect is None)
    for w in (copy, v, copy2, v2):
        w.close()


def test_copy_window_outlives_its_parent():
    # Qt closes a window's transient children with it, so the placement hint
    # has to be released once the copy is up -- otherwise closing the window a
    # copy was opened from closes the copy too.
    #
    # Only the release itself is really tested here: offscreen does NOT
    # propagate the close to transient children, so the two "survives" checks
    # below pass either way and are documentation, not coverage. The
    # close-propagation was reproduced, and the fix confirmed, against a real
    # Wayland compositor by hand (docs/DEVLOG.md).
    v = new_viewer(PLAIN_PDF, 1, 2)
    copy = v.open_copy(); pump()
    handle = copy.windowHandle()
    check("copy released the transient-parent link", handle is None or handle.transientParent() is None)
    linked = v._open_linked_copy(v.cells[0], 3); pump()
    lhandle = linked.windowHandle()
    check("linked copy released it too", lhandle is None or lhandle.transientParent() is None)
    v.close(); pump()
    check("copy survives its parent closing", copy.isVisible())
    check("linked copy survives its parent closing", linked.isVisible())
    copy.close(); linked.close()


def test_wayland_only_requested_in_a_wayland_session():
    # Qt does not fall back: asking for the wayland plugin with no compositor
    # aborts at startup, so an unconditional default stops the viewer running
    # under X11, VNC and SSH X-forwarding at all.
    import subprocess
    # The gate lives in viewer/__init__.py, which Python runs before any
    # viewer.* submodule, so importing the package is what has to be probed.
    probe = ("import os, sys;"
             "sys.path.insert(0, %r);"
             "import viewer;"
             "print(os.environ.get('QT_QPA_PLATFORM'))" % REPO_ROOT)

    def platform_for(env):
        base = dict(os.environ)
        base.pop("WAYLAND_DISPLAY", None)
        base.pop("QT_QPA_PLATFORM", None)
        base.update(env)
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                             text=True, env=base)
        return out.stdout.strip().splitlines()[-1] if out.stdout.strip() else out.stderr

    check("wayland session -> wayland",
          platform_for({"WAYLAND_DISPLAY": "wayland-0"}) == "wayland")
    check("XDG_SESSION_TYPE=wayland alone -> wayland",
          platform_for({"XDG_SESSION_TYPE": "wayland"}) == "wayland")
    check("x11 session -> Qt chooses (unset)",
          platform_for({"XDG_SESSION_TYPE": "x11"}) == "None")
    check("no session hints at all -> Qt chooses (unset)",
          platform_for({"XDG_SESSION_TYPE": ""}) == "None")
    check("an explicit platform still wins",
          platform_for({"WAYLAND_DISPLAY": "wayland-0",
                        "QT_QPA_PLATFORM": "offscreen"}) == "offscreen")


def test_toc_fluid_nav_and_out():
    v = new_viewer(TOC_PDF, 1, 1)
    v.toc_nav_next(); pump()          # init select "1 Section"
    v.toc_nav_descend(); pump()       # O -> 1.1 Sub, level 2
    v.toc_nav_descend(); pump()       # O -> 1.1.1 Subsub, level 3
    check("O dives to bottom level", toc_current(v) == "1.1.1 Subsub" and v._actual_toc_level == 3)
    v.toc_nav_previous(); pump()      # I up -> 1.1 Sub (still expanded)
    check("I pages up onto expanded parent", toc_current(v) == "1.1 Sub")
    v.toc_nav_ascend(); pump()        # Y: close subs, stay, level 2
    check("first Y closes subsections in place", toc_current(v) == "1.1 Sub" and v._actual_toc_level == 2)
    v.toc_nav_ascend(); pump()        # Y: climb to parent
    check("second Y climbs to parent", toc_current(v) == "1 Section" and v._actual_toc_level == 1)
    # O at bottom just (re)commits the level
    v.toc_nav_descend(); pump()       # -> 1.1 Sub lvl2
    v.toc_nav_descend(); pump()       # -> 1.1.1 Subsub lvl3
    v._actual_toc_level = 1
    v.toc_nav_descend(); pump()       # O at bottom
    check("O at bottom recommits level", v._actual_toc_level == 3 and toc_current(v) == "1.1.1 Subsub")
    v.close()


def test_toc_header_highlight_and_close():
    v = new_viewer(TOC_PDF, 1, 1)
    v.toc_nav_next(); pump()          # -> "1 Section" on page 1
    hl = v._toc_text_highlight
    check("TOC nav sets an in-document header highlight", hl is not None and len(hl[1]) > 0)
    check("highlight is on the destination page", hl is not None and hl[0] == v._current_page_index())
    check("get_toc_text_highlights returns rects for that page",
          len(v.get_toc_text_highlights(hl[0])) > 0 if hl else False)
    # Closing the TOC clears the in-document highlight.
    v.toggle_toc(); pump()
    check("closing TOC hides the panel", not v.toc_dock.isVisible())
    check("closing TOC clears the header highlight", v._toc_text_highlight is None)
    v.close()


def test_toc_semicolon_toggle_and_no_toc_doc():
    v = new_viewer(PLAIN_PDF, 1, 1)
    check("plain doc has no TOC entries", not v._toc_has_entries)
    QTest.keyClick(v, Qt.Key_Semicolon); pump()
    check("';' opens the (empty) TOC panel", v.toc_dock.isVisible())
    QTest.keyClick(v, Qt.Key_Semicolon); pump()
    check("';' closes it again", not v.toc_dock.isVisible())
    v.toc_nav_next(); pump()  # must not crash on a TOC-less doc
    check("TOC nav on no-TOC doc is a safe no-op", toc_current(v) is None)
    v.close()


def test_misc_no_crash():
    v = new_viewer(TOC_PDF, 1, 2)
    QTest.keyClick(v, Qt.Key_D); pump()          # dark mode
    check("dark mode toggles", v.dark_mode)
    v.show_help(); pump(); check("help shows", v._help_visible)
    v.hide_help(); pump(); check("help hides", not v._help_visible)
    cp = v.open_copy(); pump()
    check("open_copy makes an independent window", cp is not None and cp is not v)
    cp.close(); v.close()


# ---------- internal-invariant tests ----------
# Salvaged from an earlier session's throwaway suite (fix_tests.py). These
# exercise pure/low-level internals that the behavioural tests above never
# touch -- exactly the things that break silently and can't be caught by
# looking at the screen: dark-mode colour math, the byte-budgeted pixmap
# cache, the stale-render guard, and history bookkeeping.


class _FakePix:
    """Stand-in for a fitz pixmap: just the attributes _pix_to_qimage reads."""

    def __init__(self, arr):  # arr: (h, w, 3) uint8
        self.height, self.width = arr.shape[0], arr.shape[1]
        self.stride = self.width * 3
        self.samples = arr.tobytes()


def test_dark_mode_pixel_math():
    # Dark mode inverts HSL *lightness* while preserving hue/saturation, so
    # colour figures don't come out as photo negatives. Grayscale degenerates
    # to plain inversion (black text <-> white text).
    arr = np.array([[[0, 0, 0], [255, 255, 255], [255, 0, 0], [0, 0, 139]]], dtype=np.uint8)
    img = render._pix_to_qimage(_FakePix(arr), dark=True)

    def px(x):
        c = img.pixelColor(x, 0)
        return (c.red(), c.green(), c.blue())

    check("dark: black -> white", px(0) == (255, 255, 255))
    check("dark: white -> black", px(1) == (0, 0, 0))
    check("dark: pure red preserved (hue kept)", px(2) == (255, 0, 0))
    check("dark: dark blue -> light blue (hue kept)", px(3) == (116, 116, 255))
    light = render._pix_to_qimage(_FakePix(arr), dark=False)
    lc = light.pixelColor(2, 0)
    check("light: passthrough unchanged", (lc.red(), lc.green(), lc.blue()) == (255, 0, 0))


def test_pixmap_cache_invariants():
    v = new_viewer(PLAIN_PDF, 1, 1)
    key = v._cache_key(0, 100, 100)
    check("cache key is a 6-tuple", len(key) == 6)
    check("cache key carries DPR", key[5] == render._dpr_key(v._dpr()))
    # Byte-budget eviction: many pixmaps far exceeding the budget must evict
    # oldest-first while keeping the cache under budget.
    v._pixmap_cache.clear()
    big = QPixmap(3000, 3000)  # ~36 MB at 32bpp
    for i in range(10):        # ~360 MB total, well over the 256 MB budget
        v._pixmap_cache.put(("fake", i), big)
    check("byte budget keeps cache under budget",
          v._pixmap_cache.total_bytes <= render._PIXMAP_CACHE_BUDGET_BYTES)
    check("newest survives, oldest evicted",
          ("fake", 9) in v._pixmap_cache and ("fake", 0) not in v._pixmap_cache)
    huge = QPixmap(9000, 9000)  # ~324 MB alone -- larger than the whole budget
    v._pixmap_cache.put(("huge", 0), huge)
    check("single over-budget pixmap still cached", ("huge", 0) in v._pixmap_cache)
    v._pixmap_cache.clear()
    # render_page reads the cache now (it used to only ever write it).
    pm1 = v.render_page(0, QSize(400, 500))
    pm2 = v.render_page(0, QSize(400, 500))
    check("render_page returns cached pixmap on repeat", pm1 is pm2)
    v.close()


def test_stale_render_does_not_clobber():
    # A render that finishes after its cell was resized must not overwrite the
    # crisp pixmap the resize path has since drawn at the true size.
    v = new_viewer(PLAIN_PDF, 1, 1)
    pump(60)  # let the real background render land
    cell = v.cells[0]
    crisp = cell._page_pixmap
    check("cell has a crisp render to protect", crisp is not None)
    stale = QImage(50, 60, QImage.Format_RGB888)
    stale.fill(Qt.red)
    result = tasks._RenderResult(
        "visible", v._render_generation, 0, cell.page_idx,
        50, 60, v._crop_generation, v.dark_mode, v._dpr(), stale, None)
    v._render_inflight = 1
    v._on_render_done(result)
    check("stale-size render result does not replace current pixmap",
          cell._page_pixmap is crisp)
    v.close()


def test_history_back_and_clamp():
    v = new_viewer(PLAIN_PDF, 1, 1)
    v.jump_to_page(1, animate=False)  # page index 0
    pump()
    v.jump_to_page(5, animate=False)  # page index 4
    pump()
    n = len(v._history)
    v.jump_to_page(5, animate=False)  # same page again
    check("re-jump to current page adds no duplicate history entry",
          len(v._history) == n)
    back_target = v._history[v._history_pos - 1]
    v.history_back()
    pump()
    check("history_back returns to the previous position",
          v.first_page == back_target)
    # Overscroll clamp: a negative first_page must not index a bad page for
    # the content-detect reference.
    v.first_page = -2
    check("_current_page_index clamps negative first_page",
          v._current_page_index() == 0)
    v.first_page = 0
    v.close()


# ---------- center-drag pan + citation web-search ----------


def _mouse(kind, pos, button, buttons):
    et = {"press": QEvent.Type.MouseButtonPress, "move": QEvent.Type.MouseMove,
          "release": QEvent.Type.MouseButtonRelease}[kind]
    return QMouseEvent(et, QPointF(*pos), QPointF(*pos), button, buttons, Qt.NoModifier)


def _widget_point_over(cell, x0, y0, x1, y1):
    """Scan the cell for a widget point that maps into the given page-space rect."""
    disp = cell.pixmap().deviceIndependentSize()
    ox = (cell.width() - disp.width()) / 2
    oy = (cell.height() - disp.height()) / 2
    for wy in range(int(oy), int(oy + disp.height()), 3):
        for wx in range(int(ox), int(ox + disp.width()), 3):
            pt = cell._widget_point_to_page_point(QPoint(wx, wy))
            if pt is not None and x0 <= pt[0] <= x1 and y0 <= pt[1] <= y1:
                return (wx, wy)
    return None


def test_center_drag_pan_and_click():
    v = new_viewer(CITATION_PDF, 1, 1)
    cell = v.cells[0]
    # No crop: pan is a no-op.
    v.reset_zoom(); pump()
    v.pan_crop(cell, 40, 0)
    check("pan with no crop is a no-op", v.crop_rect is None)
    # Zoom in, then pan: window slides opposite the drag, size preserved, clamped.
    v._zoom_focus = (0, 0.5, 0.5)
    v.zoom_in_step(); v.zoom_in_step(); pump()
    c0 = fitz.Rect(v.crop_rect)
    v.pan_crop(cell, 60, 40); pump()
    c1 = v.crop_rect
    check("center-drag-right pans window left", c1.x0 < c0.x0)
    check("center-drag-down pans window up", c1.y0 < c0.y0)
    check("pan preserves crop size", abs(c1.width - c0.width) < 1e-6 and abs(c1.height - c0.height) < 1e-6)
    for _ in range(200):
        v.pan_crop(cell, 500, 500)
    src, cr = v.crop_source_rect, v.crop_rect
    check("pan clamps inside the page", cr.x0 >= src.x0 - 1e-6 and cr.y0 >= src.y0 - 1e-6
          and cr.x1 <= src.x1 + 1e-6 and cr.y1 <= src.y1 + 1e-6)
    # Event flow: a real center-drag pans (and does not follow the link).
    # Re-center the zoom first so a drag-right isn't already against the edge.
    v.reset_zoom(); pump()
    v._zoom_focus = (0, 0.5, 0.5)
    v.zoom_in_step(); v.zoom_in_step(); pump()
    before = fitz.Rect(v.crop_rect)
    APP.sendEvent(cell, _mouse("press", (400, 450), Qt.MiddleButton, Qt.MiddleButton))
    for i in range(1, 6):
        APP.sendEvent(cell, _mouse("move", (400 + i * 15, 450), Qt.NoButton, Qt.MiddleButton))
    APP.sendEvent(cell, _mouse("release", (475, 450), Qt.MiddleButton, Qt.NoButton))
    pump()
    check("center-drag event flow panned the crop", v.crop_rect.x0 != before.x0)
    check("center-drag left drag state clean", cell._drag_button is None)
    # A plain center-click (no drag) still follows the link in a new copy.
    v.reset_zoom(); pump()
    calls = []
    orig = v.follow_link
    v.follow_link = lambda link, pi, in_new_copy=False: calls.append(in_new_copy)
    hit = _widget_point_over(cell, 180, 92, 210, 108)
    check("found a widget point over the citation link", hit is not None)
    if hit:
        APP.sendEvent(cell, _mouse("press", hit, Qt.MiddleButton, Qt.MiddleButton))
        APP.sendEvent(cell, _mouse("release", hit, Qt.MiddleButton, Qt.NoButton))
        pump()
        check("plain center-click follows link in a new copy", calls == [True])
    v.follow_link = orig
    v.close()


def test_citation_to_web_search():
    v = new_viewer(CITATION_PDF, 1, 1)
    captured = []
    orig = window.QDesktopServices.openUrl
    window.QDesktopServices.openUrl = lambda url: captured.append(url.toString())
    try:
        link = v.link_at(0, (195, 100))  # over the citation link rect on page 0
        check("link_at finds the citation link", link is not None)
        v.follow_link_to_web_search(link, 0)
        url = captured[0] if captured else None
        check("opens a Google Scholar URL", url is not None and "scholar.google.com" in url)
        # The link targets the middle entry [2] (Hopper); the search text must be
        # that whole entry and NOT bleed into [1] (Lovelace) or [3] (Turing).
        check("Scholar query carries the cited entry [2]", url is not None and "Hopper" in url)
        check("Scholar query includes the entry's continuation line", url is not None and "1952" in url)
        check("Scholar query does not bleed into the previous entry", url is not None and "Lovelace" not in url)
        check("Scholar query does not bleed into the next entry", url is not None and "Turing" not in url)
    finally:
        window.QDesktopServices.openUrl = orig
    # The citation number wins over a misleading destination point: a point
    # aimed at entry [1] but number=2 must still return entry [2] (the real
    # "completely incorrect numbers" fix), while number=None falls back to the
    # point and returns [1]. (y just above [1]'s marker line so the point alone
    # resolves to [1].)
    aim_at_1 = fitz.Point(50, 106)
    by_number = v._reference_text_at(1, aim_at_1, number=2)
    check("number match overrides a wrong destination point", by_number and "Hopper" in by_number
          and "Lovelace" not in by_number)
    by_point = v._reference_text_at(1, aim_at_1, number=None)
    check("point fallback still works when there's no number", by_point and "Lovelace" in by_point)
    v.close()


def test_search_jump_recorded_in_history():
    # A search that moves the reader to another page must be undoable with
    # Back (Alt+Left) -- the whole search session folds into one Back target:
    # the page search was opened from.
    v = new_viewer(PLAIN_PDF, 1, 1)
    v.jump_to_page(1, animate=False); pump()  # start on page 0 (a real anchor)
    origin = v.first_page
    QTest.keyClick(v, Qt.Key_Slash); pump()
    QTest.keyClicks(v.search_input, "Page 15")  # a token only on a later page
    for _ in range(120):
        APP.processEvents(); time.sleep(0.003)
        if v._search_matches:
            break
    check("search moved to a different page", v.first_page != origin)
    moved_to = v.first_page
    QTest.keyClick(v, Qt.Key_Return); pump()  # commit; box stays, focus to doc
    v.history_back(); pump()
    check("Back after search returns to the pre-search page", v.first_page == origin)
    v.history_forward(); pump()
    check("Forward returns to the search landing page", v.first_page == moved_to)
    v.close()


def test_ask_claude_launcher():
    # ? opens the "Ask Claude" box; Enter launches a terminal running `claude`
    # on this PDF. We monkeypatch shutil.which/subprocess.Popen to capture the
    # argv instead of spawning a real terminal.
    v = new_viewer(PLAIN_PDF, 1, 1); pump()
    captured = {}
    orig_which = integrations.shutil.which
    orig_popen = integrations.subprocess.Popen
    integrations.shutil.which = lambda name: "/usr/bin/" + name

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        return object()

    integrations.subprocess.Popen = fake_popen
    try:
        QTest.keyClick(v, Qt.Key_Question); pump()
        check("? shows the Ask Claude box", v.ask_overlay.isVisible())
        QTest.keyClicks(v.ask_input, "where is the trace operator defined")
        QTest.keyClick(v.ask_input, Qt.Key_Return); pump()
        check("Enter hid the Ask box", not v.ask_overlay.isVisible())
        argv = captured.get("argv")
        check("launched a terminal subprocess", argv is not None)
        check("launches gnome-terminal", bool(argv) and argv[0].endswith("gnome-terminal"))
        inner = argv[-1] if argv else ""  # the bash -lc command string
        check("uses the sonnet model", "--model sonnet" in inner)
        check("sets high effort", "--effort high" in inner)
        check("appends the helper system prompt file",
              "--append-system-prompt-file" in inner and "pdf_helper_prompt.txt" in inner)
        check("seeds the typed context", "trace operator" in inner)
        check("hands off the pdf path", os.path.abspath(PLAIN_PDF) in inner)

        # Empty box still launches, with a stand-by prompt.
        captured.clear()
        QTest.keyClick(v, Qt.Key_Question); pump()
        QTest.keyClick(v.ask_input, Qt.Key_Return); pump()
        check("empty box still launches", "argv" in captured)
        inner2 = captured.get("argv", [""])[-1]
        check("empty box tells claude to stand by", "Wait for my instructions" in inner2)

        # Escape cancels without launching.
        captured.clear()
        QTest.keyClick(v, Qt.Key_Question); pump()
        check("? reopened the box", v.ask_overlay.isVisible())
        QTest.keyClick(v, Qt.Key_Escape); pump()
        check("Escape hid the Ask box", not v.ask_overlay.isVisible())
        check("Escape did not launch", "argv" not in captured)
    finally:
        integrations.shutil.which = orig_which
        integrations.subprocess.Popen = orig_popen
    v.close()


def test_document_survives_file_moved():
    """The PDF is read into RAM once at open and the path is never touched
    again, so moving/deleting/rewriting the file mid-session is a non-event.

    This used to break: every render worker thread lazily opened its OWN
    fitz.Document *by path* on its first task, so the first page rendered on
    a not-yet-warm thread after a move died with "no such file" -- and
    Ctrl+N did too, since a copy re-opened the path from scratch."""
    src = os.path.join(SCRATCH, "test_pv_movable.pdf")
    moved = os.path.join(SCRATCH, "test_pv_moved_away.pdf")
    for p in (src, moved):
        if os.path.exists(p):
            os.remove(p)
    _make_plain_pdf(src, pages=12)
    v = new_viewer(src, 1, 2)
    for _ in range(200):          # the read is a background thread; wait it out
        pump(5)
        if v._source.ready:
            break
    check("opened normally before the move", v.page_count == 12)
    check("document became RAM-resident", v._source.ready)
    check("main-thread doc moved onto the buffer", not v._buffer_watch.isActive())

    os.rename(src, moved)   # the reported failure: move the pdf after opening it
    os.remove(moved)        # and the harsher case -- gone from disk entirely
    check("file really is gone from disk",
          not os.path.exists(src) and not os.path.exists(moved))

    errors = []
    v._render_signals.done.connect(lambda r: r.error and errors.append(r.error))
    v.jump_to_page(10, animate=False)
    pump(80)
    check("pages still render after the move", not errors)
    check("moved-to page still produced a pixmap", v.cells[0]._page_pixmap is not None)

    # Search runs on the worker threads through the same documents.
    QTest.keyClick(v, Qt.Key_Slash); pump()
    QTest.keyClicks(v.search_input, "quick")
    for _ in range(120):
        APP.processEvents(); time.sleep(0.003)
        if v._search_matches:
            break
    check("search still works after the move", len(v._search_matches) > 0)
    QTest.keyClick(v, Qt.Key_Escape); pump()

    # A copy inherits the buffer instead of re-reading the (now absent) path.
    cp = v.open_copy(); pump(40)
    check("Ctrl+N copy opens with the file gone", cp is not None and cp.page_count == 12)
    check("copy shares the parent's buffer", cp._source is v._source)
    cp.close()

    # Rewriting the path with a *different* document must not leak in: the
    # open reader stays on the bytes it was opened with.
    _make_plain_pdf(src, pages=3)
    cp2 = v.open_copy(); pump(40)
    check("rewritten file does not change the open document", cp2.page_count == 12)
    cp2.close()
    v.close()
    os.remove(src)


def test_encrypted_pdf_unlocks_everywhere():
    """An encrypted PDF is unlocked once at the prompt and stays unlocked in
    every Document the session opens afterwards.

    This used to fail in a confusing way: MuPDF opens an encrypted file
    without complaint and only refuses when a page is actually parsed, so the
    viewer came up looking normal and then filled with render errors. The
    password has to be replayed per Document -- authentication lives on the
    Document, not the file -- so the interesting part is the opens the user
    never asks for: the render/search worker threads, the swap onto the RAM
    buffer, and Ctrl+N copies."""
    path = os.path.join(SCRATCH, "test_pv_encrypted.pdf")
    _make_encrypted_pdf(path, "hunter2")

    asked = []

    def ask(p, again):
        asked.append(again)
        return "wrong" if len(asked) == 1 else "hunter2"   # first try is wrong

    source = document._DocumentSource(path, ask_password=ask)
    check("prompted, then re-prompted after a wrong password", asked == [False, True])
    v = window.PdfGridViewer(path, rows=1, cols=2, source=source)
    v.resize(1000, 800); v.show(); pump()
    check("encrypted document opens once unlocked", v.page_count == 12)

    errors = []
    v._render_signals.done.connect(lambda r: r.error and errors.append(r.error))
    v.jump_to_page(4, animate=False)
    pump(80)
    check("pages render on a worker thread's own Document", not errors)
    check("page 5 produced a pixmap", v.cells[0]._page_pixmap is not None)

    QTest.keyClick(v, Qt.Key_Slash); pump()
    QTest.keyClicks(v.search_input, "quick")
    for _ in range(120):
        APP.processEvents(); time.sleep(0.003)
        if v._search_matches:
            break
    check("search reads the decrypted text", len(v._search_matches) > 0)
    QTest.keyClick(v, Qt.Key_Escape); pump()

    for _ in range(200):          # let the background read hand over the buffer
        pump(5)
        if source.ready:
            break
    check("encrypted document became RAM-resident", source.ready)
    v._adopt_buffer()
    errors.clear()
    v.jump_to_page(9, animate=False)
    pump(80)
    check("stream-opened Documents are unlocked too", not errors)

    cp = v.open_copy(); pump(40)
    check("Ctrl+N copy stays unlocked without re-prompting",
          cp is not None and cp.page_count == 12 and asked == [False, True])
    cp.close()
    v.close()

    # Giving up at the prompt is a clean refusal, not a window full of errors.
    try:
        document._DocumentSource(path, ask_password=lambda p, again: None)
        refused = None
    except document.EncryptedPdfError as exc:
        refused = str(exc)
    check("cancelling the prompt raises a clear error",
          refused is not None and "password" in refused.lower())
    os.remove(path)


def test_buffer_survives_move_during_load():
    """The load window -- open, then move the file while the background read
    of it is still running -- is the one genuinely racy moment, so it's the
    one worth pinning down. The read consumes a handle opened before the
    move, so it must still complete; and a Document requested inside the
    window, once the path is already gone, must wait for the buffer instead
    of failing.

    Driven deterministically by intercepting the reader thread and running
    its body by hand, since a small test PDF is buffered far too fast to
    lose a race against on purpose."""
    src = os.path.join(SCRATCH, "test_pv_midload.pdf")
    gone = os.path.join(SCRATCH, "test_pv_midload_moved.pdf")
    for p in (src, gone):
        if os.path.exists(p):
            os.remove(p)
    _make_plain_pdf(src, pages=8)

    pending = []

    class _HeldThread:            # captures the read instead of starting it
        def __init__(self, target=None, name=None, daemon=None):
            self._target = target

        def start(self):
            pending.append(self._target)

    orig_thread = document.threading.Thread
    document.threading.Thread = _HeldThread
    try:
        source = document._DocumentSource(src)
    finally:
        document.threading.Thread = orig_thread
    check("usable before the buffer lands", not source.ready and source.open().page_count == 8)

    os.rename(src, gone)   # file moves mid-load...
    os.remove(gone)        # ...and then vanishes outright

    # open() during the window can't use the path any more. It falls back to
    # the held handle via /proc/self/fd (which still resolves to the unlinked
    # inode), and failing that to waiting for the buffer -- the read is
    # finished off on another thread here so that wait can't deadlock.
    check("/proc/self/fd reaches the unlinked file",
          source._open_via_handle().page_count == 8)
    finisher = orig_thread(target=pending[0], daemon=True)
    finisher.start()
    doc = source.open()
    check("open() during the load still returns the right document",
          doc.page_count == 8)
    finisher.join(timeout=10)
    check("handle closed once buffered, so open() now uses the buffer",
          source._open_via_handle() is None and source.open().page_count == 8)
    check("read completed off the held handle despite the move", source.ready)
    check("worker documents come off the buffer too", source.get().page_count == 8)


def test_scanned_page_content_crop():
    # A scan has no text layer, so detection used to come back empty for
    # every page and C/M did nothing at all -- including not zooming out.
    doc = fitz.open(SCAN_PDF)
    page = doc.load_page(0)
    check("scan really has no text layer", len(page.get_text("blocks")) == 0)
    r = detect.detect_content_rect(page)
    check("scan gets a content rect anyway", r is not None)
    if r is not None:
        pr = page.rect
        f = (r.x0 / pr.width, r.y0 / pr.height, r.x1 / pr.width, r.y1 / pr.height)
        # Within the 1% pad of the body, having ignored the bed band and dirt.
        check("scan crop left edge on the body", abs(f[0] - SCAN_BODY[0]) < 0.03)
        check("scan crop top edge on the body", abs(f[1] - SCAN_BODY[1]) < 0.03)
        check("scan crop right edge ignores edge dirt", abs(f[2] - SCAN_BODY[2]) < 0.03)
        check("scan crop bottom edge on the body", abs(f[3] - SCAN_BODY[3]) < 0.03)
        check("scan crop is a real crop, not the whole page",
              r.width < pr.width * 0.85 and r.height < pr.height * 0.9)
    doc.close()
    # ...and end to end: C on a scan actually moves the view.
    v = new_viewer(SCAN_PDF, 1, 1)
    pump(120)
    v.reset_zoom(); pump()
    v.zoom_to_content_sampled(); pump(200)
    check("C crops a scanned document", v.crop_rect is not None)
    v.close()


def test_content_crop_keeps_figures():
    # get_text("blocks") cannot see vector art, so a figure page used to
    # crop down to its caption and cut the figure off entirely.
    doc = fitz.open(FIGURE_PDF)
    page = doc.load_page(0)
    r = detect.detect_content_rect(page)
    check("figure page gets a content rect", r is not None)
    if r is not None:
        check("crop starts above the figure, not at the caption", r.y0 < 100)
        check("crop still reaches the body text", r.y1 > 640)
        check("crop stays inside the page", r in page.rect + (-1, -1, 1, 1))
    doc.close()


class _StubPage:
    """Just enough page for _graphic_rects: a rect and a bboxlog."""

    def __init__(self, rect, bboxlog):
        self.rect = rect
        self._bboxlog = bboxlog

    def get_bboxlog(self):
        return self._bboxlog


def test_graphic_rects_bucketing():
    pr = fitz.Rect(0, 0, 800, 800)  # 8x8 grid of 100pt cells
    marks = [
        ("fill-path", (10, 10, 20, 20)),      # cell (0,0), unions with...
        ("stroke-path", (30, 30, 40, 40)),    # ...this one, same cell
        ("fill-path", (150, 150, 160, 160)),  # cell (1,1), stays separate
        ("fill-path", (-50, 200, 30, 260)),   # drawn partly off-page: clipped
        ("stroke-path", (300, 300, 250, 250)),  # corners reversed: normalized
        ("fill-path", (0, 0, 800, 800)),      # page-sized background: dropped
        ("fill-image", (400, 400, 500, 500)),  # an image IS content...
        ("fill-text", (600, 600, 700, 700)),  # ...text is not (it comes in
                                              # via get_text, which filters
                                              # boilerplate; this must not
                                              # sneak past that)
    ]
    got = sorted(tuple(round(v, 3) for v in r)
                 for r in detect._graphic_rects(_StubPage(pr, marks)))
    want = sorted([
        (10.0, 10.0, 40.0, 40.0),
        (150.0, 150.0, 160.0, 160.0),
        (0.0, 200.0, 30.0, 260.0),
        (250.0, 250.0, 300.0, 300.0),
        (400.0, 400.0, 500.0, 500.0),
    ])
    check("marks bucket, clip, normalize and union as expected", got == want)

    # The regression this whole change is about: cost per mark. 200k marks
    # through the old per-mark fitz.Rect loop took ~3.4s; vectorized it is
    # tens of ms, so a half-second bound can't flake but also can't pass
    # if the loop ever comes back.
    bulk = [("fill-path", (float(i % 700), float(i % 500),
                           float(i % 700) + 3.0, float(i % 500) + 3.0))
            for i in range(200000)]
    page = _StubPage(pr, bulk)
    t0 = time.perf_counter()
    rects = detect._graphic_rects(page)
    elapsed = time.perf_counter() - t0
    print(f"       ({len(bulk)} marks in {1000 * elapsed:.0f} ms)")
    check("200k marks stay well under half a second", elapsed < 0.5)
    check("...and still produce bucketed rects", 0 < len(rects) <= 64)


def test_dense_figure_page_detects_promptly():
    # End to end on a real PDF: a 24k-mark plot page.
    #
    # Measured as a RATIO, not a wall-clock bound. MuPDF charges ~4.5us
    # per mark just to hand the bboxlog over, and the old per-mark loop
    # added ~17us on top, so the whole regression is only a ~4x span in
    # absolute time -- too narrow for a fixed threshold to sit in without
    # flaking on a loaded machine, and it would drift with the fixture
    # size. What actually has to hold is that our own processing is small
    # next to MuPDF's enumeration: ~1.2x of it vectorized, ~4.8x with the
    # loop. Timing get_bboxlog in the same run makes that machine- and
    # size-independent.
    doc = fitz.open(DENSE_PDF)
    page = doc.load_page(0)
    check("fixture really is mark-dense",
          len(page.get_bboxlog()) >= DENSE_MARKS)
    t0 = time.perf_counter()
    page.get_bboxlog()
    bboxlog_cost = time.perf_counter() - t0

    t0 = time.perf_counter()
    r = detect.detect_content_rect(page, allow_raster=False)
    with_graphics = time.perf_counter() - t0
    t0 = time.perf_counter()
    detect.detect_content_rect(page, allow_raster=False, allow_graphics=False)
    without_graphics = time.perf_counter() - t0

    surcharge = with_graphics - without_graphics
    ratio = surcharge / bboxlog_cost if bboxlog_cost > 0 else 0.0
    print(f"       (detect {1000 * with_graphics:.0f} ms; figure pass costs "
          f"{ratio:.1f}x its own get_bboxlog)")
    check("figure pass adds little over MuPDF's own enumeration", ratio < 2.5)
    check("dense figure page still gets a content rect", r is not None)
    if r is not None:
        # The plot spans x 120-420, y 90-270 (PDF y is measured from the
        # top here because insert_text/draw use top-left origin pages).
        check("crop covers the whole dense plot",
              r.x0 <= 121 and r.x1 >= 419 and r.y0 <= 91)
    doc.close()


def test_image_figure_counts_as_content():
    # A raster figure is logged by MuPDF as "fill-image"; the kind list
    # said "image", which matches nothing, so an image-based figure was
    # invisible to detection and the crop fell back to the caption alone.
    doc = fitz.open(IMAGE_FIGURE_PDF)
    page = doc.load_page(0)
    blocks = [b for b in page.get_text("blocks") if b[4].strip()]
    check("only the caption is extractable as text", len(blocks) == 1)
    check("caption sits below the image", blocks[0][1] > 340)
    r = detect.detect_content_rect(page, allow_raster=False)
    check("image figure page gets a content rect", r is not None)
    if r is not None:
        check("crop starts at the image, not the caption", r.y0 < 100)
        check("crop still reaches the caption", r.y1 > 355)
    doc.close()


def test_graphic_detect_budget_stops_scanning():
    # A document where every sampled page is a dense figure would pay
    # get_bboxlog's unbounded cost ten times over. The task gives up on
    # figures once it has spent its budget -- but never before the first
    # page, so a single-page Shift+C is never silently degraded.
    calls = []
    real = detect._graphic_rects

    def slow(page):
        calls.append(1)
        time.sleep(tasks._GRAPHIC_DETECT_BUDGET_S * 0.6)
        return real(page)

    detect._graphic_rects = slow
    try:
        v = new_viewer(PLAIN_PDF, 1, 1)
        pump(60)
        v.zoom_to_content_sampled()
        for _ in range(60):
            pump(20)
            if v._detect_pending_gen is None:
                break
        check("figure pass ran on the first pages", len(calls) >= 1)
        check("figure pass stopped once the budget was spent",
              len(calls) < 10)
        check("the crop still landed", v.crop_rect is not None)
        v.close()
    finally:
        detect._graphic_rects = real


def test_failed_detection_still_zooms_out():
    # When detection comes back empty there is nothing to crop to, but the
    # keypress must not be a no-op: C means "fit the content", and leaving
    # the view untouched is what made it look broken on scans.
    v = new_viewer(PLAIN_PDF, 1, 1)
    pump(60)
    # Patched on tasks, not on detect: _ContentDetectTask imported the name,
    # so it resolves through tasks' globals and a patch on detect would not
    # reach it. (_graphic_rects above is different -- detect calls that one
    # through its own globals, so it must be patched on detect.)
    orig = tasks.detect_content_rect
    # **kw, so this stub keeps standing in for the real signature as its
    # opt-out flags (allow_raster, allow_graphics) come and go -- a stub
    # that raises TypeError instead would still leave fractions=None and
    # so would still "pass" this test, silently testing the wrong path.
    tasks.detect_content_rect = lambda page, **kw: None
    try:
        v._zoom_focus = (v.first_page, 0.5, 0.5)
        v.zoom_in_step(); pump(60)
        check("zoomed in before the recrop", v.crop_rect is not None)
        v.zoom_to_content_sampled(); pump(200)
        check("failed detection drops the crop", v.crop_rect is None)
        check("failed detection clears the crop source", v.crop_source_rect is None)
    finally:
        tasks.detect_content_rect = orig
    v.close()


def test_detect_loading_dot_clears_when_superseded():
    # _detect_generation is bumped by manual crop changes too, so a zoom
    # made mid-detection sends the result down the superseded path. The
    # loading dot has to go out anyway, or it stays lit indefinitely.
    v = new_viewer(PLAIN_PDF, 1, 1)
    pump(60)
    v.zoom_to_content_sampled()
    gen = v._detect_generation
    check("detection is pending", v._detect_pending_gen == gen)
    v._zoom_focus = (v.first_page, 0.5, 0.5)
    v.zoom_in_step()  # manual crop: bumps _detect_generation past the result
    check("manual crop superseded the detection", v._detect_generation != gen)
    pump(200)
    check("pending marker cleared by the stale result", v._detect_pending_gen is None)
    check("loading dot is out", not v._loading)
    check("manual zoom survived the stale result", v.crop_rect is not None)
    v.close()


def test_busy_indicator_follows_the_page_numbers():
    # Addison asked for the indicator to sit to the right of the numbers so it
    # can't move them, and to change every half second.
    v = new_viewer(PLAIN_PDF, 1, 1)
    pump(60)
    v._update_status()
    v._loading = False
    v._render_status()
    check("idle counter is the bare range", v.status_overlay.text() == v._status_text)
    v._loading = True
    v._busy_frame = 0
    v._render_status()
    text = v.status_overlay.text()
    check("busy counter still starts with the range", text.startswith(v._status_text))
    check("first frame is drawn after the numbers",
          constants.BUSY_FRAMES[0] in text[len(v._status_text):])
    v._advance_busy_frame()
    stepped = v.status_overlay.text()
    check("a tick changes the frame", constants.BUSY_FRAMES[1] in stepped)
    check("stepping still leaves the numbers first", stepped.startswith(v._status_text))
    for _ in range(len(constants.BUSY_FRAMES) - 1):
        v._advance_busy_frame()
    check("frames wrap back round", constants.BUSY_FRAMES[0] in v.status_overlay.text())
    check("indicator steps twice a second", v._busy_timer.interval() == 500)
    v._refresh_loading_indicator()
    check("timer runs exactly while there is work", v._busy_timer.isActive() == v._loading)
    v.close()


def test_selection_uses_primary_and_ctrl_c_uses_clipboard():
    # Two clipboards, each for its own job: a drag publishes PRIMARY only, and
    # CLIPBOARD is written by Ctrl+C alone (Addison's spec).
    v = new_viewer(PLAIN_PDF, 1, 1)
    pump(60)
    clipboard = APP.clipboard()
    clipboard.setText("sentinel-from-elsewhere", QClipboard.Clipboard)
    v.cells[0].selections = [{"rects": [], "text": "selected words"}]
    v.on_selection_changed()
    check("releasing a drag leaves the ordinary clipboard alone",
          clipboard.text(QClipboard.Clipboard) == "sentinel-from-elsewhere")
    if clipboard.supportsSelection():
        # Not the offscreen platform, but assert it where the platform has one.
        check("the drag published the primary selection",
              clipboard.text(QClipboard.Selection) == "selected words")
    QTest.keyClick(v, Qt.Key_C, Qt.ControlModifier); pump()
    check("Ctrl+C copies the selection to the ordinary clipboard",
          clipboard.text(QClipboard.Clipboard) == "selected words")

    # A window-level shortcut would otherwise eat the search box's own Ctrl+C.
    v.focus_search_input(); pump()
    v.search_input.setText("query text")
    v.search_input.selectAll()
    v.copy_selection()
    check("Ctrl+C in a text field copies the field, not the page",
          clipboard.text(QClipboard.Clipboard) == "query text")
    v._on_escape(); pump()
    v.close()


def test_stale_generation_render_is_dropped():
    # Async cancellation: a render dispatched before a page move must not
    # paint after it. The size guard (above) doesn't cover this -- a late
    # result can carry exactly the right size and still be for the old view.
    v = new_viewer(PLAIN_PDF, 1, 1)
    pump(60)
    cell = v.cells[0]
    crisp = cell._page_pixmap
    check("cell has a render to protect", crisp is not None)
    size = cell.size()
    stale = QImage(size.width(), size.height(), QImage.Format_RGB888)
    stale.fill(Qt.red)
    inflight = v._render_inflight
    result = tasks._RenderResult(
        "visible", v._render_generation - 1, 0, cell.page_idx,
        size.width(), size.height(), v._crop_generation, v.dark_mode,
        v._dpr(), stale, None)
    v._on_render_done(result)
    check("superseded generation does not repaint the cell",
          cell._page_pixmap is crisp)
    # The in-flight count belongs to the current generation, so a stale
    # arrival must not decrement it -- that would strand the busy spinner.
    check("superseded generation does not touch the in-flight count",
          v._render_inflight == inflight)
    v.close()


def test_search_refine_keeps_one_back_target():
    # A whole search session folds into ONE Back target: the page search was
    # opened from. Refining the query rewrites the landing entry in place
    # rather than pushing a second one, so Back after two matches still lands
    # on the pre-search page and not on the first match.
    v = new_viewer(PLAIN_PDF, 1, 1)
    v.jump_to_page(1, animate=False); pump()
    origin = v.first_page
    QTest.keyClick(v, Qt.Key_Slash); pump()

    def type_and_wait(text):
        # Wait for the search for THIS query to complete: _search_matches is
        # not cleared between queries, so polling it alone returns the
        # previous query's results immediately and tests nothing.
        v.search_input.setText(text)
        v._on_search_text_changed(text)
        for _ in range(300):
            APP.processEvents(); time.sleep(0.003)
            if (v._last_search_query == text and not v._search_inflight
                    and v._search_matches):
                pump()
                return True
        return False

    check("first query matched", type_and_wait("Page 12"))
    first_landing = v.first_page
    depth_after_first = len(v._history)
    pos_after_first = v._history_pos
    check("refined query matched", type_and_wait("Page 15"))
    second_landing = v.first_page
    check("refining moved the view again", second_landing != first_landing)
    # The heart of the fold: the refined landing REPLACES the previous one
    # rather than being pushed on top of it. Without this, Back/Forward walk
    # every intermediate match a reader typed through.
    check("refining does not deepen the history",
          len(v._history) == depth_after_first)
    check("refining does not advance the history position",
          v._history_pos == pos_after_first)
    check("the history entry now holds the refined landing",
          v._history[v._history_pos] == second_landing)
    QTest.keyClick(v, Qt.Key_Return); pump()
    v.history_back(); pump()
    check("one Back returns to the pre-search page (not the first match)",
          v.first_page == origin)
    v.history_forward(); pump()
    check("Forward lands on the refined match, not the first one",
          v.first_page == second_landing)
    v.close()


def test_last_page_state_write_is_atomic():
    # The state file holds EVERY document's saved position, so writing it in
    # place risks losing all of them. A failed write must leave the previous
    # file intact and not litter the directory with temp files.
    from viewer import state
    path = state._state_file_path()
    state._save_last_page("/tmp/doc_a.pdf", 4)
    state._save_last_page("/tmp/doc_b.pdf", 9)
    check("both documents saved", state._load_last_page("/tmp/doc_b.pdf") == 9)
    before = open(path).read()

    # Fail the rename, i.e. after the temp file is fully written.
    orig_replace = os.replace
    os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    try:
        state._save_last_page("/tmp/doc_c.pdf", 1)  # must not raise
    finally:
        os.replace = orig_replace
    check("a failed save leaves the previous file byte-identical",
          open(path).read() == before)
    check("the earlier positions are still readable",
          state._load_last_page("/tmp/doc_a.pdf") == 4)
    leftovers = [f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")]
    check("a failed save leaves no temp file behind", leftovers == [])
    # And a normal save still lands.
    state._save_last_page("/tmp/doc_c.pdf", 1)
    check("a later save succeeds normally", state._load_last_page("/tmp/doc_c.pdf") == 1)


def test_entry_point_and_helper_prompt_paths():
    # Two things the rest of the suite would not notice if they broke: the
    # entry point's public surface (pdfviewer_xcb.py does `from pdfviewer
    # import main`) and the location of pdf_helper_prompt.txt, which is read
    # at the far end of a terminal launch where a wrong path is silent.
    check("entry point still exports main", callable(pdfviewer.main))
    check("entry point still exports PdfGridViewer",
          pdfviewer.PdfGridViewer is window.PdfGridViewer)
    check("pdfviewer_xcb.py's import still resolves",
          "from pdfviewer import main" in open(
              os.path.join(REPO_ROOT, "pdfviewer_xcb.py")).read())
    # Ask the launcher what path it actually passes, rather than recomputing
    # it here -- recomputing would agree with a wrong implementation.
    captured = {}
    orig_which, orig_popen = integrations.shutil.which, integrations.subprocess.Popen
    integrations.shutil.which = lambda name: "/usr/bin/" + name

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        return type("P", (), {"pid": 1})()

    integrations.subprocess.Popen = fake_popen
    try:
        integrations._launch_claude_helper(PLAIN_PDF, "hello")
    finally:
        integrations.shutil.which = orig_which
        integrations.subprocess.Popen = orig_popen
    inner = captured.get("argv", ["", "", "", "", ""])[-1]
    m = re.search(r"--append-system-prompt-file (\S+)", inner)
    check("the launcher passes a helper-prompt path", m is not None)
    check("the path it passes actually exists",
          bool(m) and os.path.exists(m.group(1).strip("'\"")))


def main():
    tests = [
        test_shortcuts_bound,
        test_paging,
        test_status_format,
        test_busy_indicator_follows_the_page_numbers,
        test_selection_uses_primary_and_ctrl_c_uses_clipboard,
        test_grid_resize,
        test_cursor_zoom,
        test_zoom_out_from_content_crop_stays_in_page,
        test_scanned_page_content_crop,
        test_content_crop_keeps_figures,
        test_graphic_rects_bucketing,
        test_dense_figure_page_detects_promptly,
        test_image_figure_counts_as_content,
        test_graphic_detect_budget_stops_scanning,
        test_failed_detection_still_zooms_out,
        test_detect_loading_dot_clears_when_superseded,
        test_collapse_and_restore,
        test_recrop_after_collapse_single_render,
        test_search_incremental_commit_escape,
        test_search_no_match_red,
        test_search_escape_cancels_inflight_result,
        test_copy_window_inherits_crop,
        test_copy_window_outlives_its_parent,
        test_wayland_only_requested_in_a_wayland_session,
        test_toc_fluid_nav_and_out,
        test_toc_header_highlight_and_close,
        test_toc_semicolon_toggle_and_no_toc_doc,
        test_misc_no_crash,
        test_dark_mode_pixel_math,
        test_pixmap_cache_invariants,
        test_stale_render_does_not_clobber,
        test_history_back_and_clamp,
        test_center_drag_pan_and_click,
        test_citation_to_web_search,
        test_search_jump_recorded_in_history,
        test_ask_claude_launcher,
        test_document_survives_file_moved,
        test_buffer_survives_move_during_load,
        test_encrypted_pdf_unlocks_everywhere,
        test_stale_generation_render_is_dropped,
        test_search_refine_keeps_one_back_target,
        test_last_page_state_write_is_atomic,
        test_entry_point_and_helper_prompt_paths,
    ]
    for t in tests:
        print(f"\n== {t.__name__} ==")
        t()
    print("\n" + ("=" * 40))
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for f in _failures:
            print("  - " + f)
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
