# Devlog

> **Numbers here are almost all from Addison's machine and may be
> unreproducible.** Fedora/GNOME on native Wayland, mixed-DPI dual monitor
> (1.0 and 1.25 scale), 16 cores, btrfs `compress=zstd:1` on NVMe with `/home`
> ~95% full — **cold reads ~25 MB/s, warm ~1.8 GB/s**, which is unusually slow
> and is why several designs exist at all. Treat the figures as evidence about
> *mechanism* and as ratios; re-measure before calling anything a regression.
>
> **Which PDFs a number came from matters, and this note used to conflate
> three sources.** The cold-read figures come from *one* large scanned textbook
> (a few hundred MB) in Addison's own library — that file is what the read-path
> and buffering work was measured against, and it won't exist for anyone else.
> Other behaviour has been checked by hand against assorted real PDFs from the
> same library, which is where most real-world rendering and layout problems
> have surfaced; those runs are manual and unrecorded. The automated suite uses
> neither: it generates every fixture with PyMuPDF at import, ~1.1 MB total,
> written and then read straight back, so it never performs a cold read and
> has no coverage of the I/O path at all. Say which of the three a number came
> from when you add one.

New entries go at the bottom.

---

## 2026-07 (early)

- Platform settled on native Wayland; `pdfviewer_xcb.py` kept for comparison.
- Background render pool, per-thread documents, generation guards, pixmap LRU.
- **`get_pixmap()` holds the GIL for nearly its whole duration**, ~90 ms on
  image-heavy pages and *resolution-independent* (embedded-image decode
  dominates). Going from 1 to 4 concurrent render threads dropped the main
  thread's Python throughput from ~40% to ~8% of unloaded speed — hence the low
  thread count.
- Residual: a few figure-dense pages still briefly stall a fast scroll.
  Deprioritized by Addison. Unimplemented proposal: warm the whole document into
  the render cache at low priority after opening, instead of prefetching a few
  pages of runway.

## 2026-07-17

- PageUp/PageDown never fired: `QKeySequence("PageUp")` parses to `Key_unknown`.
  Fixed to `"PgUp"`/`"PgDown"`.
- Same-page match stepping didn't repaint: `setPixmap` with an identical pixmap
  schedules zero paints. `_jump_to_current_match` now calls `cell.update()`.

## 2026-07-21

Toolbar removed, floating overlays introduced. Key remap (`h`/`l`, `j`/`k`, `m`,
`b`, `;`). Cursor-anchored zoom, Ctrl+wheel, pinch. Box-zoom collapse with saved
grid. Incremental search. TOC fluid paging. Internal-invariant tests recovered
from an earlier throwaway suite.

## 2026-07-22

Citation→Scholar rewritten twice (coordinates → number matching; nearest-block →
marker-to-marker extraction). Search history folding. Ask-agent launcher added;
`?` moved from `QShortcut` to `eventFilter` after it silently never fired on
Wayland — confirmed fixed on real hardware, and structurally invisible to the
offscreen suite.

## 2026-07-28

Escape also resets zoom; `Shift+M` aliases `Shift+C`. Established that a second
`QShortcut` on an already-bound sequence is ambiguous and fires neither, and
that an unshifted `QShortcut("M")` doesn't match Shift+M.

## 2026-07-31 — whole file into RAM

- Immutable vs mutable stream buffer: 4 extra Documents over a 13 MB PDF cost
  +4 MB with a read-only memoryview. With a plain `bytearray`, a 321 MB file
  took 674 MB RSS for *one* Document.
- **Unchunked background reads starve the app; chunking is free.** Cold, 321 MB,
  foreground = open + TOC + render 2 pages: 1.65 s alone, **12.53 s** against an
  unchunked background read, **1.63 s** against 1 MB chunks with 10 ms sleeps.
  The background read takes ~12.6 s either way. Both ends are best-case.
- Net on a 664 MB scan, cold: window 1.02→1.60 s, first page 3.34→4.05 s, page
  jumps **228–301 ms → 38–78 ms**, RSS 195→831 MB.
- Suite → 24 tests / 128 checks. `test_buffer_survives_move_during_load` drives
  the race by stubbing `threading.Thread` to capture the reader body and running
  it by hand; a small PDF buffers far too fast to race against honestly.

## 2026-08-10 — content crop on scans

- Root cause: a pure scan returns 0 text blocks on every page, so detection
  returned None and the handler early-returned without touching the crop.
- Raster fallback added: 50 dpi grey, adaptive ink threshold, bed-band peeling,
  thin-speck rejection. Result on the reported file: stable
  `x 0.164–0.855 y 0.157–0.874`, 34–50 ms for 10 pages.
- Figures added via `get_bboxlog()` (`get_drawings()` is ~3× slower). One plot
  page had 6113 strokes, hence 8×8 bucketing against O(n²) clustering.
- Cluster vote keeps every group ≥25% of the biggest. Output on text papers is
  byte-identical before/after; only single-page `Shift+C` changes.
- **On a scan the expensive part is `get_text("blocks")` itself** — ~94 ms/page
  on the 664 MB scan, 944 ms for 10 — not the raster pass. Detection therefore
  goes text-only across all sampled pages first and rasterizes at most 4.
- Suite → 28 tests / 148 checks, all four new tests confirmed to fail pre-fix.

## 2026-08-11 — content crop made slow by figures

- `_graphic_rects` was building one `fitz.Rect` per mark at ~17 µs each; two
  pages of one paper had **48 932 and 64 929** marks — 1.4 s for a single page,
  holding the GIL.
- Vectorized with numpy; verified byte-identical to the old loop on 676 real
  pages. Worst page 1411→342 ms; 10-page sampled crop 1.8–3.1 s → 0.12–0.81 s.
  (`ufunc.at` is an unbuffered element-wise loop and loses to `argsort` +
  `np.minimum.reduceat`.)
- **`get_bboxlog()` is the remaining floor**: ~4.5 µs/mark, 285 ms on the 65k-mark
  page, GIL held for roughly half (measured with a spinning counter thread).
  Hence `_GRAPHIC_DETECT_BUDGET_S = 0.5`, checked before each page.
- `_GRAPHIC_BBOX_KINDS` contained `"image"`, which MuPDF never emits. Fixing it
  changed 43 of 586 pages in Addison's literature directory, every one growing
  to include a previously-cut figure.
- Suite → 32 tests / 163 checks. `test_failed_detection_still_zooms_out` was
  found passing through its exception path after a stub signature went stale;
  **the other stubs have not been audited for the same false pass.**

## 2026-08-17

Outside review by sol. See `REVIEW-2026-08.md`; findings still open.

## 2026-08-19 — documentation moved into the repo

- Project context previously lived in one agent's private memory on one machine.
  Merged into `docs/` and the memories deleted, so agents on other machines and
  other models start from the same information. `AGENTS.md` (with `CLAUDE.md` as
  a symlink to it) is Addison's and points at README, FEATURES, METADOCS,
  ARCHITECTURE and HISTORY.
- Went through the project with Addison separating decisions he specified from
  agent decisions that had been narrated as his intent. His are now named, and
  `METADOCS.md` defines what attribution means: an explicit statement that he
  specified a behaviour, marking it a requirement rather than a revisable agent
  choice. Naming him in a rationale is not attribution.
- Three design principles recorded in `ARCHITECTURE.md`, all his: responsiveness
  over finishing in-flight work; under-cropping over over-cropping, with the
  crop tool assumed permanently unreliable; browser conventions for UI, with
  ordinary PDF-viewer behaviour as the check for oddity.
- Docs restructured to `METADOCS`/`ARCHITECTURE`/`HISTORY`/`DEVLOG`/`GOTCHAS`.
  `HOTKEYS.md` deleted as a duplicate of the Ctrl+H help page.
- Comment pass over `pdfviewer.py`: 4183 -> 3971 lines, comment text ~71k -> ~55k
  characters. Stale toolbar references removed, benchmark narratives moved here,
  bug stories to `HISTORY.md`, trap warnings condensed against `GOTCHAS.md`,
  code restatement deleted. Module docstring no longer claims "minimal".
- Corrected in passing: the slide animation does **not** re-render per frame
  (`_on_slide_tick` only advances progress; `_draw_slide_layer` blits existing
  pixmaps). The per-event re-render problem was zoom, and was already fixed.
- Corrected in the Ctrl+H help: Ctrl+N was described as an "exact duplicate",
  but the copy re-detects its own content crop and may differ in zoom.

## 2026-08-19 — busy indicator moved, Ctrl+C copying

Two small changes Addison asked for in `designer_notes.txt`.

- The background-work marker moved from a leading dot to a spinner appended
  after the page range, so it can no longer shift the numbers. Four
  quadrant-filled circles (`BUSY_FRAMES`) stepped by `_busy_timer` every 500 ms
  (`BUSY_INTERVAL_MS`), his cadence. The timer runs only while `_loading`, so an
  idle window doesn't wake twice a second; the cycle restarts at frame 0 each
  time work begins.
- Text selection no longer writes CLIPBOARD. Drag release publishes PRIMARY
  (`on_selection_changed`); Ctrl+C writes CLIPBOARD (`copy_selection`). The
  Ctrl+C `QShortcut` is window-level and so pre-empts a focused overlay field's
  own copy — `copy_selection` detects a `QLineEdit` with a selection and defers
  to it.
- Tests: 32 groups / 163 checks -> 34 / 175. Both new groups were confirmed to
  fail against the pre-change logic. The offscreen platform reports
  `supportsSelection() == False`, so the PRIMARY assertion is skipped there and
  the load-bearing check is that a drag leaves CLIPBOARD untouched.
- `test_status_format` now checks `startswith` rather than `endswith`, since the
  indicator is a suffix.

## 2026-08-19 (later) — docs restructured around the code

No behaviour change; suite re-run at 34 groups / 175 checks, all passing.

- `METADOCS.md`: HISTORY given a test the code can fail — if it can be learned
  from the code and its docs, it isn't history. Routing rewritten as one list
  headed by the comment at the code, since the previous four independent
  triggers let one session's two changes land in ARCHITECTURE, DEVLOG *and*
  HISTORY. Added the one-home rule and the SUBSYSTEMS entry.
- `HISTORY.md`: 78 -> 56 lines. Both 2026-08-19 entries removed as duplicates of
  what ARCHITECTURE and the code already carried; the docs-moved-into-repo entry
  folded into the Gap note; the "text selection was harder than anticipated"
  line dropped as naming no failed approach.
- `ARCHITECTURE.md`: 256 -> 100 lines. Keeps Addison's three design principles,
  the cross-cutting invariants that get broken from outside, the platform
  default, a symbol index and how to run the tests.
- `SUBSYSTEMS.md` (new, 115 lines): one section per area — shape, which parts
  are Addison's requirements, known gaps. Opened when changing that area. It
  points at the comments rather than restating them, which is most of why the
  two files together are shorter than the one they replace: the code already
  explained nearly all of it, often in more detail.
- `pdfviewer.py`: the only doc content with no home at the code was the Ask
  agent's two gaps, now comments — hard-wired terminal/model flags against
  `FEATURES.md`'s stated intent, and the stale path handed to the helper after
  the file moves.
- Always-loaded set (METADOCS + ARCHITECTURE + HISTORY) went from 446 lines to
  272.
- Every doc's opening paragraph deleted: each was describing its own purpose,
  which `METADOCS.md` already does — HISTORY's was copied from it word for word.
  What survived is what METADOCS doesn't say: DEVLOG's machine caveat, GOTCHAS'
  reason to look before blaming a handler, and, in ARCHITECTURE, a sentence on
  what `pdfviewer.py` actually is.
- Found while checking that the docs matched the code: `HELP_HTML` still
  described the busy indicator as "a small dot to its left" after it became a
  spinner on the right, and two comments still called it a dot. All corrected.

## 2026-08-19 — review follow-up: platform gate, search cancel, copy windows

Four changes, all from `REVIEW-2026-08.md` except the last, which Addison
asked for. Suite: 34 groups / 175 checks → 38 / 192, ~25 s.

- **Wayland is only requested in a Wayland session.** Measured the failure the
  review only predicted: `QT_QPA_PLATFORM=wayland` with `WAYLAND_DISPLAY` set to
  a nonexistent socket exits 134 with `Could not load the Qt platform plugin`.
  There is no fallback, so the old unconditional `setdefault` made the viewer
  fail to *start* under X11 rather than merely look wrong there — the reason
  this became urgent is that Addison is sharing it with colleagues. Verified all
  three branches: Wayland session → `wayland`, `XDG_SESSION_TYPE=x11` → unset and
  Qt picks xcb, explicit setting untouched.
- **Escape cancels an in-flight search.** `_exit_search` cleared the matches but
  never bumped `_search_generation`, so `_on_search_done` still accepted the late
  result, repopulated the matches and jumped the view — the review's repro
  (a hidden search landing on page 6) reproduced exactly.
- **Ctrl+N inherits the parent's crop** (`autocrop=False` + copied rects) instead
  of running a fresh detection, which resamples pages at random and could open
  the "exact duplicate" at a different zoom. `_open_linked_copy` deliberately
  still detects its own; it lands on a different page, where a box-zoom crop
  carried over from the source window would usually frame the wrong region.
- **Copy windows now outlive the window they came from.** Addison reported them
  closing with the parent and read it as the copy being a subprocess; it isn't —
  same process, and the cause is that Qt closes a window's transient children
  along with it. Releasing the hint one event-loop turn after `show()` keeps the
  compositor's parent-relative placement (identical `pos()` before and after, and
  after the parent closes) and drops the lifetime tie. What is given up is the
  copy being kept stacked above its parent. Both were confirmed against the real
  compositor; the offscreen suite cannot see either, so its checks cover the
  release only (`GOTCHAS.md`).

Not done, and still open from the review: unpinned `requirements.txt`, and the
non-atomic state write in `_save_last_page`.

## 2026-08-19

- **Encrypted PDFs (review finding 3).** `_DocumentSource.__init__` now checks
  the probe's `needs_pass` and prompts, and `_open` replays the password on
  every Document — the per-thread ones, the copy windows', and the swap onto
  the RAM buffer, none of which the user initiates. Cancelling the prompt
  raises `EncryptedPdfError` into the existing `main()` handler, so the failure
  mode is a message box rather than a window that renders errors; the comment
  there that claimed this already worked is corrected.
  `test_encrypted_pdf_unlocks_everywhere` covers a wrong-password retry, a
  worker render, a search, the post-buffer re-open, a Ctrl+N copy, and the
  cancel path. Nothing was
  measured; the prompt costs one dialog at startup for files that need it and
  one `needs_pass` read for files that don't. All 33 groups pass.

## 2026-08-19 (later) — repository put under git

- **The project is now a git repository**, pushed to Addison's private GitHub
  repo `AddisonWC/pdf_viewer`; the initial commit is the working tree as it
  stood. `venv/`, `__pycache__/`, tool caches and `.claude/settings.local.json`
  are ignored; the small test PDFs and `designer_notes.txt` are tracked, so the
  notes travel with the repo to anyone it is shared with. Nothing in the program
  changed and nothing was measured.

## 2026-08-19 (later still) — split into a package

Acting on the outside review's "split the module" suggestion, and on three of
its smaller ones. `docs/REVIEW-2026-08.md` records what was declined and why.

- **`pdfviewer.py` (4149 lines) became `pdfviewer.py` (48) plus a `viewer`
  package.** The UI-independent half came out first: an AST pass confirmed that
  nothing below `PdfGridViewer` references `PdfGridViewer` or `PageCell`, so
  detection, the document source, render math, the three tasks, persistence,
  the Ask launcher, rect helpers, constants and `HELP_HTML` moved with no
  dependency inversion. Module map is in `ARCHITECTURE.md`.
- **The window class was not split, and that is a decision, not a leftover.**
  Bucketing its 130 methods by area and measuring which instance attributes
  cross buckets: 28 of 86 are touched by three or more areas — `cells` by 8,
  `doc` and `first_page` by 7, `page_count` by 6, `crop_rect`,
  `crop_source_rect`, `_source`, `_crop_generation` and `cols` by 5. Search,
  crop and rendering are not separate concerns in that class; they are views
  onto one mutable view state. Mixins would have moved the lines without
  touching the coupling.
- **`PixmapCache` was the exception** and came out as a real object:
  `_pixmap_cache` and `_pixmap_cache_bytes` were touched only by `_cache_get`,
  `_cache_put` and `__init__`, nowhere else in 4149 lines. The window keeps key
  construction (`_cache_key`, `_result_cache_key`), which reads window state.
- **The move was verified, not just tested.** A script compared every top-level
  symbol's source text before and after: 83 identical, none missing, and the
  only diffs were the five intended ones. `PdfGridViewer`'s own diff is the
  cache swap and nothing else.
- **Atomic state write.** `_save_last_page` writes a temp file in the state
  directory and `os.replace`s it, cleaning the temp up if the rename fails.
  That file holds *every* document's position, so the previous in-place
  truncate-and-write risked losing all of them.
- **`requirements.txt` now has ranges.** Upper bounds at the next major; floors
  are explicitly marked in the file as unverified, since nobody has run the
  suite against old releases.
- **Two hazards the split introduced, both now covered by tests.**
  `_launch_claude_helper` locates `pdf_helper_prompt.txt` via `__file__`, which
  moved down a directory — a wrong path there fails silently at the far end of
  a terminal launch. And `pdfviewer_xcb.py` does `from pdfviewer import main`.
  `test_entry_point_and_helper_prompt_paths` asserts both, and asks the
  launcher what path it passes rather than recomputing it.
- **The import-binding trap cost a test failure immediately.**
  `test_failed_detection_still_zooms_out` patched `detect.detect_content_rect`,
  which no longer reaches `_ContentDetectTask` because `tasks` bound the name at
  import. Patched on `tasks` instead; the rule is in `GOTCHAS.md`.
- **Suite: 38 groups / 192 checks → 43 / 222, ~29 s** (Addison's machine, same
  offscreen platform). Four new groups: stale-generation render drop,
  search-refine history fold, atomic state write, entry-point and helper paths.
  Each was confirmed to fail against a mutation of the logic it guards — the
  first drafts of two of them survived their mutants and were rewritten (the
  history one only checked Back's destination, which the mutant preserves; the
  path one recomputed the path instead of reading it off the launch argv).

## 2026-08-19 (later)

- **`pump()` no longer spins a fixed budget.** It was 40 iterations of
  `processEvents()` + `time.sleep(0.003)` unconditionally, 187 calls per run:
  **25.8 s of the 29.4 s total was inside pump**, against 7 s of CPU. The rest
  was the main thread sleeping on work that had already landed. It now tracks an
  idle streak and returns once nothing is outstanding.
- **Three idle signals were not enough.** Events-processed + pool
  `activeThreadCount` + `threading.active_count()` got the run to 16.6 s but
  broke `test_buffer_survives_move_during_load`: `_buffer_watch` (150 ms) was
  armed and unfired, so the loop looked idle while the result was still one
  timeout away. Armed `QTimer`s now count as work outstanding. Rule in
  `GOTCHAS.md`.
- **The timer scan is per-decision, not per-iteration.** `findChildren(QTimer)`
  over the widget tree cost ~30 ms per pump when run on every idle iteration
  (21.5 s); moving it to fire only after a streak has elapsed gave 20.4 s. Small
  next to the main win, and it caps the scan at `n/quiet` calls.
- **29.8 s → 20.4 s, 222/222 checks unchanged.** The residual is honest: the
  8 ms page-slide tick and the 500 ms busy timer are legitimately armed during
  those tests, so those pumps correctly wait out the full budget. Getting below
  this means per-call-site conditions, not a smarter `pump`.
- **Verified against flakiness, since early exit trades time for a race.** Five
  consecutive runs clean and tightly grouped (20.41–20.54 s), then three more
  clean under three spinning CPU hogs — the case where a premature return would
  show. 8/8.
- **The suite moved to `tests/`,** unchanged otherwise: still one file, still
  the hand-rolled `check`/`main()` harness. The four root `*_test.pdf` files
  were deleted rather than moved: 26 KB referenced by nothing — not the suite,
  which builds every fixture with PyMuPDF at import, and not the docs (Addison
  confirmed they are not used for manual spot-checks either). Still in history.
- **The move exposed a latent cwd dependency, which is the reason to run it
  from more than one directory.** `test_wayland_only_requested_in_a_wayland_session`
  builds a subprocess probe that injects `dirname(__file__)` into `sys.path` to
  `import viewer`. That path is now `tests/`, not the repo root — but the test
  still passed from the repo root, because the cwd supplied `viewer/` regardless.
  It failed 5 checks from any other cwd. Both it and the `pdfviewer_xcb.py`
  lookup in `test_entry_point_and_helper_prompt_paths` now use `REPO_ROOT`.
  Verified 222/222 from the repo root, from inside `tests/`, and from `$HOME`.
- **The header note at the top of this file was rewritten.** It had collapsed
  three different PDF sources into one claim that "test files" are 300–660 MB
  scanned textbooks. Corrected by Addison: the cold-read figures come from a
  single large scanned textbook, other behaviour has been checked by hand
  against assorted real PDFs, and the automated suite uses neither.
