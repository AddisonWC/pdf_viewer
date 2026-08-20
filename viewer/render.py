"""Turning a PyMuPDF pixmap into a QPixmap, at a size Qt will blit 1:1.

The rounding helpers exist to dodge QLabel's logical-size round trip, which
resamples the whole page when it comes out a pixel large -- GOTCHAS.md has
the arithmetic.
"""
from collections import OrderedDict

import numpy as np
from PySide6.QtGui import QImage


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


def _pixmap_bytes(pixmap):
    return pixmap.width() * pixmap.height() * pixmap.depth() // 8


def _dpr_key(dpr):
    # Rounded so float noise can't split what's physically the same scale
    # factor into distinct cache keys.
    return round(dpr * 100)


class PixmapCache:
    """Per-window LRU of rendered pages, budgeted by total bytes.

    Keys are built by the window (`_cache_key` / `_result_cache_key`), which
    is where the crop generation, dark mode and DPR that identify a render
    live; this only stores what it is handed.
    """

    def __init__(self, budget_bytes=_PIXMAP_CACHE_BUDGET_BYTES):
        self._entries = OrderedDict()
        self._bytes = 0
        self._budget = budget_bytes

    def get(self, key):
        pixmap = self._entries.get(key)
        if pixmap is not None:
            self._entries.move_to_end(key)
        return pixmap

    def put(self, key, pixmap):
        old = self._entries.pop(key, None)
        if old is not None:
            self._bytes -= _pixmap_bytes(old)
        self._entries[key] = pixmap
        self._bytes += _pixmap_bytes(pixmap)
        # len > 1 so a single pixmap larger than the whole budget still
        # stays cached rather than being evicted the moment it's added.
        while self._bytes > self._budget and len(self._entries) > 1:
            _, evicted = self._entries.popitem(last=False)
            self._bytes -= _pixmap_bytes(evicted)

    def clear(self):
        self._entries.clear()
        self._bytes = 0

    def __len__(self):
        return len(self._entries)

    def __contains__(self, key):
        return key in self._entries

    @property
    def total_bytes(self):
        return self._bytes
