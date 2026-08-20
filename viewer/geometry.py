"""Rect arithmetic and the boilerplate-text filter.

Shared by content detection and by text selection, which both have to reason
about where the real content on a page is.
"""
import fitz


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
