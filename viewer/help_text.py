"""The Ctrl+H help page."""

# Ctrl+H documentation, shown in place of the grid until Escape. Hand-written
# rather than generated from the bind() table so it can group related actions
# and explain why. It drifts silently: update it in the same edit as any key
# change.
HELP_HTML = """
<h2>PDF Viewer -- Help</h2>
<p><i>Press Escape to close this and go back to reading.</i></p>

<h3>Turning pages</h3>
<table cellspacing="6">
<tr><td><b>L / Right / Down / Space / Page Down</b></td><td>Advance one page (whole grid slides forward)</td></tr>
<tr><td><b>H / Left / Up / Backspace / Page Up</b></td><td>Go back one page</td></tr>
<tr><td><b>J / K</b></td><td>Down / up by a whole grid row at a time (in a multi-column grid, a non-overlapping row of pages)</td></tr>
<tr><td><b>Mouse wheel</b></td><td>One page per notch</td></tr>
<tr><td><b>Home / End</b></td><td>Jump to the first / last page</td></tr>
<tr><td><b>G</b>, then type a page number, <b>Enter</b></td><td>Go to a specific page (a "Go to page" box appears top-left while active), with the usual page-turn animation</td></tr>
<tr><td><b>Shift+G</b>, then type a page number, <b>Enter</b></td><td>Same, but skips the animation</td></tr>
</table>

<h3>Grid layout</h3>
<table cellspacing="6">
<tr><td><b>, / .</b></td><td>Fewer / more columns</td></tr>
<tr><td><b>Shift+, / Shift+.</b></td><td>Fewer / more rows</td></tr>
</table>

<h3>Zoom &amp; crop</h3>
<table cellspacing="6">
<tr><td><b>Right-click-drag</b> on a page</td><td>Draw a box to crop/zoom to that region (all pages)</td></tr>
<tr><td><b>Right-drag a box, then click <i>left</i></b></td><td>Zoom into that box and collapse to a single page; the previous grid is remembered (see below). Right-click then left-click without dragging a box does the same collapse without zooming</td></tr>
<tr><td><b>Left-drag, then click <i>right</i></b> before releasing</td><td>Content-crop (same as C)</td></tr>
<tr><td><b>- / = (or +)</b>, <b>Ctrl+wheel</b>, trackpad <b>pinch</b></td><td>Zoom out / in toward the mouse cursor; magnifies all pages without changing the grid layout</td></tr>
<tr><td><b>0</b>, <b>B</b>, or <b>Escape</b></td><td>Back to the full, uncropped page (Escape does this once there's no search box, Ask box, or help page open to close first)</td></tr>
<tr><td><b>C</b> or <b>M</b></td><td>Auto-crop margins/headers/footers, sampled across several pages (robust default)</td></tr>
<tr><td><b>Shift+C</b> or <b>Shift+M</b></td><td>Same, but detected from only the current page</td></tr>
<tr><td><b>Mouse "Back" side button</b></td><td>Same as C -- re-crop to detected content</td></tr>
<tr><td colspan="2"><i>Any zoom-out (0/B/Escape) or recrop (C/M, Back button) also restores the grid saved by a box-zoom-to-single-page collapse.</i></td></tr>
</table>

<h3>Search</h3>
<table cellspacing="6">
<tr><td><b>/</b>, then type</td><td>Incremental search: runs as you type and jumps to the first match from where you were. The box (top-right) stays open and turns red if nothing matches</td></tr>
<tr><td><b>Enter</b></td><td>Commit: keeps the box and highlights up but hands focus back to the document, so keys act on the pages again instead of typing into the box</td></tr>
<tr><td><b>N / Shift+N</b></td><td>After Enter, step to the next / previous match</td></tr>
<tr><td><b>Escape</b></td><td>Close the search box, drop the highlighting, and forget the query</td></tr>
</table>

<h3>Ask Claude about this PDF</h3>
<table cellspacing="6">
<tr><td><b>?</b> (Shift+/)</td><td>Open a box to start an interactive Claude Code session about this document in a terminal. Type optional context for the first prompt, or leave it blank to just drop into a session that's standing by</td></tr>
<tr><td><b>Enter</b></td><td>Launch the terminal (the PDF's path is handed off automatically, so you don't have to find the file again)</td></tr>
<tr><td><b>Escape</b></td><td>Cancel without launching</td></tr>
</table>

<h3>Selecting &amp; copying text</h3>
<table cellspacing="6">
<tr><td><b>Left-drag</b> over text</td><td>Select it. On release the selection goes to the X/Wayland <i>primary selection</i>, so you can paste it straight into another window with a middle-click. Your ordinary clipboard is left alone</td></tr>
<tr><td><b>Ctrl+left-drag</b></td><td>Add another, separate selection (e.g. across pages) instead of replacing the current one</td></tr>
<tr><td><b>Ctrl+C</b></td><td>Copy the current selection to the ordinary clipboard, for pasting with Ctrl+V</td></tr>
<tr><td><b>Escape</b></td><td>Clear the current selection (and, in the same press, zoom back out -- see Zoom &amp; crop)</td></tr>
</table>

<h3>Links</h3>
<table cellspacing="6">
<tr><td><i>Cursor becomes a pointing hand over any link.</i></td><td></td></tr>
<tr><td><b>Click</b> a link</td><td>Follow it in this window (internal links jump to the target page; external links open in your browser)</td></tr>
<tr><td><b>Center-click (middle button)</b> a link</td><td>For an internal link: open a copy of the document, jumped to that link's target, positioned near this window (native Wayland can't place a window exactly, only hint that it belongs near its parent). An external link opens in your browser, same as a normal click. Does nothing if there's no link under the cursor</td></tr>
<tr><td><b>Center-drag</b> (while zoomed in)</td><td>Pan the zoomed view -- grab the page and slide it under the cursor. Only pans when zoomed in; a plain center-click (no drag) still follows the link</td></tr>
<tr><td><b>Center-hold, then left-click</b> a citation</td><td>Look the cited work up on Google Scholar in your browser instead of jumping to the bibliography: opens a search for the reference entry the citation points at</td></tr>
</table>

<h3>History &amp; windows</h3>
<table cellspacing="6">
<tr><td><b>Alt+Left / Alt+Right</b></td><td>Back / forward through pages you've jumped to (via links, "go to page", or a search that moved you -- a whole search folds into one Back step, returning to where you opened it)</td></tr>
<tr><td><b>Ctrl+N</b></td><td>Open a copy of this window at the same page and grid (the copy re-detects its own content crop, so its zoom may differ)</td></tr>
<tr><td><b>Mouse "Forward" side button, held</b></td><td>Chord modifier: scroll wheel changes column count, right-click resets zoom, while held</td></tr>
</table>

<h3>Table of contents</h3>
<table cellspacing="6">
<tr><td><b>T</b> or <b>;</b></td><td>Toggle the table-of-contents sidebar (closed by default, opens collapsed to the top level). Click an entry to jump there. Shows a placeholder if the document has none</td></tr>
<tr><td><b>U / I</b></td><td>Page down / up through entries at the current level, flowing across section boundaries: paging down off a section's last subsection lands on the next section, then descends into its subsections; paging up does the reverse. The jumped-to entry is highlighted yellow, its header is highlighted on the page, and the view follows</td></tr>
<tr><td><b>O</b></td><td>Dive in: open the current entry's sublevel, move to its first child, and make that the level U/I now page at (on a bottom-level entry it just sets the level to here)</td></tr>
<tr><td><b>Y</b></td><td>Climb out: if this section's subsections are open, first just close them and set the level to this section; a second Y then moves to the parent, folding the level being left. Either way the level U/I pages at follows where you land</td></tr>
<tr><td colspan="2"><i>Y/U/I/O sit one keyboard row above H/J/K/L -- a vim motion shape shifted up. Pressing any of them opens the sidebar first if it's closed; closing the sidebar removes the header highlight from the page.</i></td></tr>
</table>

<h3>Other</h3>
<table cellspacing="6">
<tr><td><b>D</b></td><td>Toggle dark mode -- inverts each pixel's lightness while keeping its hue, so text goes white-on-dark but figures keep their colors</td></tr>
<tr><td><b>Ctrl+H</b></td><td>Show this help</td></tr>
<tr><td><b>Ctrl+Q / Q</b></td><td>Quit</td></tr>
</table>
<p><i>The page counter (top-left overlay, shown as first-last/total) has a small spinner to its right that turns while background work (loading, page rendering, search, or content detection) is running.</i></p>
"""
