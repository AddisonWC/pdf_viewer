# Architecture

An M×N grid of PDF pages that slides forward one page at a time, on PySide6 and
PyMuPDF, for reading dense papers and scanned books.

`pdfviewer.py` is the entry point and nothing else; the program is the `viewer`
package beside it. The window class is still one ~2.4k-line class in
`viewer/window.py` — its areas share one mutable view state (`cells`, `doc`,
`first_page`, `crop_rect`, the generation counters), so they are not separable
without redesigning that state. Splitting it further by moving methods to other
files would hide that coupling rather than remove it.

---

## Design principles

Three rules explain most of the code. All three are Addison's.

**1. Responsiveness beats finishing work already in flight.** When new input
arrives, in-progress computation is abandoned rather than allowed to delay the
screen: a second page-advance snaps the running slide to its end, zooming out
cancels a pending content detection, continuous zoom recrops immediately and
defers the expensive re-render. The page counter is the mirror image of the same
rule — it forces a synchronous repaint so the number can't lag behind input,
because Addison reads it while scrolling.

**2. Under-cropping is much better than over-cropping.** Content detection is
assumed to be permanently unreliable: it works on ordinary papers and books, and
no one should expect it to work on exotic PDFs. What matters is that failure
stays cheap — cropping too little still shows the page and what happened,
cropping too much hides content with no clue why. So results are unioned across
sampled pages, detected rects are padded outward, and empty detection drops the
crop rather than leaving the view untouched. The `C` sample is random, so
pressing it again redraws the sample and usually escapes a bad crop; that's the
intended affordance, not a workaround.

**3. When in doubt, do what a browser does.** Most people spend most of their
computer time in a browser, so browser conventions are the ones already in
muscle memory: center-click opens a copy, Alt+Left/Right is history, Ctrl+wheel
zooms rather than scrolls. Where the question isn't a browser one, ordinary
PDF-viewer behavior is the reference for whether something feels wrong.

**Non-goals.** Many features are absent because Addison doesn't use them and
considers them clutter — a file browser, annotations, continuous scroll — and
some because they are probably hard, such as printing. The program is focused on
what he actually uses or wants; it isn't trying to be feature complete.

## Invariants

These are the ones broken from outside, by an agent who never opened the
subsystem they belong to.

- **Every background task carries a generation token, and a superseded result is
  discarded.** New background work follows this or it will clobber a view that
  has moved on. Renders additionally carry the cell size they were dispatched
  for, so a late render can't overwrite a crisper pixmap.
- **Overlays are never in a layout.** In one they displace the page grid instead
  of floating over it, which is the whole reason the toolbar came out.
- **One render thread.** PyMuPDF rendering holds the GIL, so more workers starve
  the GUI thread rather than adding throughput. Don't raise the count.
- **Render at the size you will display, but not on every change.** Rendering to
  the final size is free and scaling softens pages; re-rendering per event is
  not free, and is throttled or debounced wherever it appears.
- **Last-read-page persistence is best effort.** A missing or corrupt state file
  must never break opening or closing a document. The write is atomic
  (`_save_last_page`) because that one file holds every document's position.

## Platform

`QT_QPA_PLATFORM` is `setdefault`-ed to `wayland` **only when a Wayland session
is actually present** (`WAYLAND_DISPLAY`, or `XDG_SESSION_TYPE=wayland`);
elsewhere Qt chooses, and an explicit setting always wins. Qt does not fall back
from a missing compositor — it aborts — so the gate is what lets the viewer run
on other people's machines at all (`GOTCHAS.md`). `pdfviewer_xcb.py` forces xcb
for comparison. Wayland forbids a client from setting its own window position,
so copy windows can only hint that they belong near their parent — xcb would
allow it and was rejected (`HISTORY.md`).

## Index

Where each area lives and the symbols to grep for it. What an area is shaped
like, and which of it is Addison's spec, is in `SUBSYSTEMS.md`.

`pdfviewer.py` is the entry point only (`parse_args`, `main`); `pdfviewer_xcb.py`
imports `main` from it. Importing the `viewer` package sets `QT_QPA_PLATFORM`
and caps the thread pool, so `viewer/__init__.py` holds those two lines and
nothing may run before them — see **Platform** above.

| area | module | grep for |
| --- | --- | --- |
| Document source | `viewer/document.py` | `_DocumentSource`, `_resolve_password`, `EncryptedPdfError`; `_adopt_buffer`, `_buffer_watch` are on the window |
| Rendering | `viewer/render.py`, `viewer/tasks.py` | `_render_targets`, `_pix_to_qimage`, `PixmapCache`, `_RenderTask` |
| Content detection | `viewer/detect.py` | `detect_content_rect`, `_graphic_rects`, `_raster_content_rect`; `_ContentDetectTask` and `_GRAPHIC_DETECT_BUDGET_S` are in `viewer/tasks.py` |
| Rect math, boilerplate filter | `viewer/geometry.py` | `_union_rects`, `_pad_within_page`, `_is_boilerplate` |
| One grid cell | `viewer/cell.py` | `PageCell` |
| Persistence | `viewer/state.py` | `_state_file_path`, `_save_last_page` |
| Ask agent | `viewer/integrations.py` | `_launch_claude_helper`, `pdf_helper_prompt.txt` |
| Tuning, theme, regexes | `viewer/constants.py` | `BUSY_FRAMES`, `_REF_MARKER`, `ZOOM_STEP` |
| Help page | `viewer/help_text.py` | `HELP_HTML` |

Everything below is on `PdfGridViewer` in `viewer/window.py`:

- **Overlays** — `_position_overlays`, `_raise_overlays`, `_render_status`.
- **Selection and clipboards** — `on_selection_changed`, `copy_selection`.
- **Search** — `_navigate_search`, `_search_history_open`.
- **Table of contents** — `_actual_toc_level`, `_begin_toc_nav`, and the `Y/U/I/O`
  comment block above them.
- **Zoom, crop and gestures** — `zoom_at_focus`, `_handle_two_button_gesture`,
  `_take_saved_grid_dims`.
- **Citations** — `_citation_number`, `_reference_text_at`.
- **History and copies** — `open_copy`, `_align_over`, `_push_history`.

## Testing

`venv/bin/python tests/test_pdfviewer.py` — a hand-rolled offscreen suite, 43 groups /
222 checks, about 20 seconds, deterministic. Two layers: behavioural, driving the
widget through `QTest`, and internal-invariant, covering pixel math, cache
eviction and staleness guards. It is entirely agent-built; Addison has not been
involved in its design. It generates all seven of its fixtures with PyMuPDF at
import and reads nothing off disk, so it has no coverage of the I/O path. Its
traps, including the rule that a new regression test must be confirmed to fail
against the pre-fix logic, are in `GOTCHAS.md`.
