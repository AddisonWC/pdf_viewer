# Subsystems

## Document source

`_DocumentSource` owns the file's bytes and is the only source of
`fitz.Document` objects — copy windows inherit it via `PdfGridViewer(source=)`
rather than re-reading. The file is read into RAM once, on a background daemon
thread, in chunks, off a handle opened in `__init__`, so the load survives the
file being renamed or deleted mid-read. Workers re-open off the buffer on a
`_generation` check; the main thread swaps in `_adopt_buffer`, polled by
`_buffer_watch`.

Encryption is handled here too, because it has to be handled once for every
Document: `__init__` prompts (`ask_password`, defaulting to
`_prompt_for_password`) and `_open` replays the password on each Document it
hands out. Giving up at the prompt raises `EncryptedPdfError`, which `main()`
already reports cleanly. Anything that opens a `fitz.Document` outside
`_DocumentSource._open` would silently reintroduce the bug.

## Rendering

Background `QThreadPool` (`_RenderTask`), one `fitz.Document` per worker thread
because MuPDF documents aren't thread-safe. Results carry generation tokens and
the cell size they were dispatched for. A per-window `PixmapCache` sits in
front, budgeted by total bytes rather than entry count; the budget is an agent's
pick and open to revision. The cache only stores — the keys that identify a
render (crop generation, dark mode, DPR) are built by the window, in
`_cache_key` and `_result_cache_key`. Dark mode inverts HSL lightness rather
than RGB — Addison asked for that after seeing colour figures come out as photo
negatives.

## Content detection

`detect_content_rect(page, allow_raster=, allow_graphics=)` finds the content
region; `_ContentDetectTask` runs it on the pool over a random 10-page sample
(`C`) or the current page (`Shift+C`). Both the key split and the sample size
are Addison's. Three sources, in the order they are tried: text blocks filtered
by `_is_boilerplate`, vector graphics from `get_bboxlog()` bucketed and voted on,
and `_raster_content_rect` as a fallback for pure scans that report no text at
all. Two budgets bound the cost, `_MAX_RASTER_DETECT_PAGES` and
`_GRAPHIC_DETECT_BUDGET_S`, because both underlying PyMuPDF calls hold the GIL.

The whole subsystem is governed by principle 2 in `ARCHITECTURE.md`: it is
assumed to stay unreliable, and every choice here is about making failure cheap.

## Overlays

Floating translucent cards, children of `self.central`, positioned by hand in
`_position_overlays`: the page counter (`status_overlay`, `first-last/total`),
the go-to-page card, the search card and the Ask card. `_raise_overlays`
maintains stacking. There is no toolbar (`HISTORY.md`); Reset-Zoom and
Zoom-to-Content survive as keys with no button.

A spinner **after** the page range marks background work — Addison asked for it
to the right of the numbers so it can't move them, and to step every half second.
Prefetch deliberately doesn't count, being work nobody is waiting on, but the
document read does, since on a big scan it's the only sign the file is loading.
Goto is focus-tied; search and Ask stay up until Enter or Escape.

## Selection and the two clipboards

Releasing a text drag publishes **PRIMARY** only (`on_selection_changed`);
**CLIPBOARD** is written by Ctrl+C alone (`copy_selection`). Addison asked for
each clipboard to be used for what it is for, so merely selecting text can no
longer overwrite what the reader copied elsewhere.

## Search

Incremental, debounced, on the thread pool, generation-tokened. The box stays
open until Escape; Enter commits and blurs so `n`/`N` and paging work on the
document again. Escape also *cancels* a search still running, by bumping the
generation in `_exit_search` — without that a late result navigates the view
after the box has closed. Refining a query re-anchors to where search was opened, not to
the previous match, and the whole session folds into one Back/Forward transition
(`_search_history_open`).

## Table of contents

Sidebar, closed by default, and when opened it starts fully collapsed. All of the
paging behavior is Addison's spec and is written out in full in the `Y/U/I/O`
comment block above `_begin_toc_nav` — treat it as a requirement. Navigation
highlights the sidebar row and the header on the page, using the destination
point from `get_toc(simple=False)` with title-text search as fallback.

## Zoom, crop and mouse gestures

Cursor-anchored zoom (`zoom_at_focus`) from `-`/`=`/`+`, Ctrl+wheel and trackpad
pinch, with the re-render throttled. Two-button gestures
(`_handle_two_button_gesture`): right-drag then left-click collapses to a single
zoomed page and saves the grid, left-drag then right-click content-crops. Only
the first collapse records the layout, so repeated collapses still restore the
reader's real grid. The centre button resolves on release, so click follows a
link into a copy, drag pans, and hold-then-left-click sends a citation to Google
Scholar.

## Citations to Scholar

Matched by citation *number*, not by coordinates, and extracted entry-aware from
the reference list. Both constraints came from approaches that failed
(`HISTORY.md`); the comments on `_reference_text_at` say what breaks without
them. The whole raw entry goes to Scholar unparsed — cheap and unlikely to fail,
where splitting out author and title could.

## Ask agent

`?` opens the overlay; Enter always launches, including on an empty box.
`_launch_claude_helper` shells out to a terminal running `claude` with
`--append-system-prompt-file pdf_helper_prompt.txt`, hands off the file path and
nothing else, and never raises. There is no channel back to the viewer, by
design (`HISTORY.md`).

`pdf_helper_prompt.txt` tells the session that math notation doesn't survive text
extraction, so it must read page *images* for notation questions and use
extracted text only for coarse keyword localization.

Two known gaps, both marked at the code: the terminal, CLI program and model
flags are hard-wired although `FEATURES.md` says the choice is the user's, and
the path handed over is stale if the file moved after opening.

## History, copies, persistence

Alt+Left/Right walk a deduplicated history list. `open_copy` and
`_open_linked_copy` build a second window sharing the `_DocumentSource`;
`_align_over` places it as near the source cell as Wayland allows. The
transient-parent placement hint is released on the next event-loop turn
(`_release_transient_parent`), because Qt closes a window's transient children
along with it and a copy is meant to outlive the window it came from. `open_copy`
inherits the parent's crop and passes `autocrop=False` rather than re-detecting,
since detection resamples pages at random and an "exact duplicate" would
otherwise open at a different zoom; `_open_linked_copy` still detects its own,
as it lands on a different page and the source window's box-zoom crop would
usually be the wrong region there. Last-read page
is persisted per document under `$XDG_STATE_HOME` (Addison asked for this), keyed
by absolute path, so moving a file loses your place. Only the page is saved —
grid size and crop describe how a window happens to be set up, not the document.
