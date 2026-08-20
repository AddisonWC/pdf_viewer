"""Tuning constants, theme colours and shared regexes.

Values only: anything with behaviour lives in the module that uses it.
"""
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor


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
