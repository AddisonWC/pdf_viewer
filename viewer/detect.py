"""Content detection: where the ink actually is on a page.

Governed by principle 2 in ARCHITECTURE.md -- detection is assumed to stay
unreliable, so every choice here is about making failure cheap.
"""
import fitz
import numpy as np

from .geometry import _is_boilerplate, _pad_within_page, _rect_gap, _union_rects


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
