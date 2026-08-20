"""Background work: rendering, search and content detection on the pool.

Every task carries a generation token and a superseded result is discarded by
the receiver; see the invariant in ARCHITECTURE.md.
"""
import sys
import time
import traceback
from collections import namedtuple

import fitz
from PySide6.QtCore import QObject, QRunnable, Signal
from PySide6.QtGui import QImage

from .detect import detect_content_rect
from .render import _clamp_image_size, _pix_to_qimage, _render_targets


_RenderResult = namedtuple(
    "_RenderResult", "kind generation cell_index page_idx w h crop_gen dark dpr image error"
)


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
