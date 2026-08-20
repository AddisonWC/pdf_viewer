# Gotchas

**Check this before concluding a handler is broken** — several "the key does
nothing" reports were binding-layer bugs with a perfectly good handler.

## Qt shortcuts

- **`QKeySequence` doesn't parse `"PageUp"`/`"PageDown"`.** It returns
  `Key_unknown` silently, so the shortcut binds to nothing. Use `"PgUp"`/`"PgDown"`
  or the `Qt.Key` enum.
- **An unshifted `QShortcut("M")` doesn't match Shift+M.** Shifted variants need
  their own binding. "Capital X does nothing" usually means a missing bind.
- **Shifted characters like `?` must be handled in `eventFilter`, not as a
  `QShortcut`.** Shift+/ delivers `Key_Question` *with* `ShiftModifier`, which a
  no-modifier `QKeySequence("?")` fails to match on Wayland. Note the offscreen
  suite **cannot** catch this: `QTest.keyClick` synthesizes the key with no
  modifier, so it passes either way.
- **A second `QShortcut` on an already-bound sequence is ambiguous and fires
  neither.** That's why Escape's reset-zoom lives at the tail of `_on_escape`
  rather than as its own binding.

## Qt painting

- **`setPixmap` with an identical pixmap (same `cacheKey`) schedules zero
  paints** — which is exactly what a same-page re-render on a cache hit
  produces. Overlay-only state (highlights, selection) must call `cell.update()`
  itself.
- **`setText` only schedules a repaint,** and a stream of input events can defer
  it indefinitely. The page counter forces `repaint()` for this reason.
- **Overlays must never be in a layout,** or they displace the page grid.

## PyMuPDF

- **Documents aren't thread-safe.** One per worker thread, always.
- **`get_pixmap()` holds the GIL for essentially its whole duration,** and the
  cost is resolution-independent. More render threads starve the GUI thread
  rather than adding throughput.
- **`get_bboxlog()` holds the GIL for roughly half its duration** and is
  unbounded in page complexity. Hence the figure-detection time budget.
- **`_GRAPHIC_BBOX_KINDS` must not contain `"image"` — MuPDF never emits it.**
  The real kinds are `fill-image`/`fill-imgmask` (and `fill-shade`). This made
  raster figures invisible to detection for weeks.
- **`get_text("blocks")` is text-only.** Images need `TEXT_PRESERVE_IMAGES`;
  vector art is never reported at all.
- **A pure scan returns zero blocks on every page** — an empty list, not a thin
  result. Handle it rather than early-returning.
- **`fitz.open(stream=)` shares an immutable buffer but copies a mutable one.**
  `bytes`/read-only `memoryview` are shared; a `bytearray` is copied per
  Document.
- **`fitz.open` succeeds on an encrypted file; only page access fails.** Check
  `needs_pass` — and note it stays truthy after a successful `authenticate()`
  (`is_encrypted` is what flips), so it means "this file is encrypted", not
  "this Document is still locked". Authentication is per Document, so every
  re-open needs the password again. A PDF with an empty user password never
  reports `needs_pass`: MuPDF unlocks it while opening.
- **Adding a page invalidates earlier page handles.** Re-fetch page 0 before
  `insert_link` if you added page 1 in between.

## Test suite

- **Patch a function where it is *called*, not where it is defined.** Now that
  the code is a package, `from .detect import detect_content_rect` binds the
  name into `tasks`' globals, so patching `detect.detect_content_rect` does not
  reach `_ContentDetectTask` — the test patches `tasks.detect_content_rect`.
  `_graphic_rects` is the opposite case: `detect` calls it through its own
  globals, so it must be patched on `detect`. Patching `shutil.which`,
  `subprocess.Popen` or `threading.Thread` works from anywhere, because those
  mutate an attribute on the shared stdlib module rather than rebinding a name.
- **A stub with the wrong signature can "pass" through the exception path.** A
  `detect_content_rect` stub started raising `TypeError` when a kwarg appeared,
  and the test still passed because the exception path also yields no crop.
  **The other stubs have not been audited for this.**
- **Confirm every new regression test fails against the pre-fix logic.** Two here
  would otherwise have been vacuous.
- **The suite sets `XDG_STATE_HOME` to a temp dir.** Last-read-page persistence
  is real global on-disk state and leaks between runs otherwise.
- **`page.draw_rect` commits a whole `Shape` per call** — 24 000 calls takes over
  two minutes. Write raw `re f` operators via `doc.update_stream` instead, one
  operator per mark (a single `Shape` merges into one `fill-path`).
- **Content-stream operators use PDF's bottom-left origin,** while PyMuPDF's page
  methods flip y. Easy to build an upside-down fixture.
- **Assert ratios, not wall-clock,** for perf tests. The dense-figure test
  compares the figure-pass surcharge against that page's own `get_bboxlog` time,
  because fixed and broken are only ~4× apart in absolute terms.

## Wayland

- **A client cannot set its own window position** — protocol, not a Qt
  limitation. Copy windows can only hint at a transient parent.
- **XWayland collapses all outputs to one rounded-up global integer scale,**
  softening rendering on mixed-DPI setups.
- **`QT_QPA_PLATFORM=wayland` with no compositor aborts; Qt does not fall back
  to xcb.** `Could not load the Qt platform plugin "wayland" ... even though it
  was found`, then `abort` — so requesting it unconditionally makes the program
  refuse to start under X11, VNC and SSH X-forwarding. Hence the session check
  in `viewer/__init__.py` — the package `__init__` because Python runs it before
  any `viewer.*` submodule, so no import order inside the package can let
  PySide6 load first. Anything added above that check must not import Qt.

## Qt window lifetime

- **Qt closes a window's transient children along with it.** `setTransientParent`
  reads as a placement *hint*, but it also ties the two windows' lifetimes: a
  copy window kept the link and vanished when the window it was opened from was
  closed. Placement is decided at first map and survives releasing the link, so
  `_set_transient_parent` sets it, then clears it on the next event-loop turn.
- **The offscreen platform does not propagate that close to transient children,**
  so the suite cannot catch it — like the `?` shortcut above. The regression test
  asserts the link is *released*; the close behaviour itself was checked by hand
  against a real compositor.

## High-DPI rendering

**QLabel's logical-size round trip can silently resample the whole page.** It
computes a DPR-tagged pixmap's logical size as `qRound(device / dpr)`, then
converts back to a source size as `qRound(logical * dpr)`. At fractional DPR
that can come out *larger* than the pixmap really is — e.g. 707 px at 1.25:
707/1.25 = 565.6 → 566 logical → 707.5 → 708 > 707. Qt clamps the source rect to
the real bounds without shrinking the destination, and the resulting ~0.1% scale
mismatch smooth-resamples the entire page: visibly soft text.

Whether a given render trips this is fractional-arithmetic luck per (page size,
cell size, crop), which is why one zoom state could look soft while a nearly
identical one stayed crisp. The predicate
`qRound(qRound(D/dpr)*dpr) > D` in either dimension predicted soft-vs-sharp in
all 60 cases of a sweep at DPR 1.0/1.25/1.5. `_render_targets`,
`_largest_safe_extent` and `_clamp_image_size` exist to keep every displayed
pixmap on the safe side of it.

Also note **Qt's `qRound` rounds half away from zero** while Python's `round()`
is half-to-even; the ties matter here, hence `_qround`.
