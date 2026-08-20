# History

**Gap:** this account starts in mid-July 2026. It was assembled on 19 August
from one agent's private memory on one machine, so earlier work, and anything
written by other models, isn't recorded and can't be reconstructed from the code.

---

## How it grew

It began as a small grid viewer and became a tuned reading application. Most of
it is agent-written against Addison's design decisions, and he mostly doesn't
read the code.

**The toolbar came out (July),** because a toolbar takes layout space away from
pages. Floating translucent overlays replaced it.

**The whole file moved into RAM (July 31),** after Addison reported that moving a
PDF after opening it caused errors. The constraints that shape the buffering are
in the comments on `_DocumentSource`; that bug report is why it exists at all.

**Content detection grew two passes past text** because text-only detection
failed twice: a page whose top half was a plot got cropped down to its caption,
and a pure scan yielded no text blocks at all.

That scan is where the crop rule came from. Detection returning nothing made the
handler return without touching the crop, so `C` did nothing whatsoever —
including not zooming out, stranding the reader zoomed in. Addison's priority
was the dead key rather than crop accuracy.

**Adding figures made cropping slow enough to be unusable** on plot-heavy pages,
which is why the clustering is vectorized and the figure pass is time-budgeted.

**Citations to Scholar were wrong twice** before working, once on matching and
once on extraction. Both failed approaches are named in the comments on
`_reference_text_at` as constraints on the current design.

**Responsiveness became an explicit principle** after page movement and
pinch-zoom were each found re-rendering more than they needed to. Both are
fixed. Sharpness had been over-prioritized against performance in the process,
which was corrected: rendering at the right size is free, re-rendering on every
change is not.

## Settled — don't reopen

- **Native Wayland over XWayland.** XWayland would let copy windows be placed
  precisely, but collapses every output to one rounded-up global scale factor,
  visibly softening rendering on a mixed-DPI setup. Addison is the main user and
  has settled this: window placement isn't worth the sharpness.
- **Pathological scans barely crop.** Some scans have dark, non-saturated
  page-edge bands, so per-page results are noisy and their union reaches the page
  edge. Accepted, because a union can only under-crop, never cut content.
- **The Ask agent doesn't talk back to the viewer.** Fire-and-forget by design.
- **It isn't a `~/.claude` skill.** A skill's name and description load into
  every session's context; a per-launch system-prompt append costs other
  sessions nothing.
