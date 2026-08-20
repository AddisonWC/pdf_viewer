"""One grid cell: displays a single rendered page and owns its drag state."""
from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QLabel, QSizePolicy

from .constants import (
    BACKGROUND_COLOR,
    DARK_BACKGROUND_COLOR,
    RESIZE_DEBOUNCE_MS,
    SEARCH_CURRENT_FILL,
    SEARCH_OTHER_FILL,
    SELECTION_BORDER,
    SELECTION_FILL,
    TOC_TEXT_HIGHLIGHT_FILL,
    ZOOM_BORDER,
    ZOOM_FILL,
)
from .render import _render_targets


class PageCell(QLabel):
    """Displays a single rendered PDF page, always filling its cell.

    Rendering is driven by this cell's own size, not the size at the moment
    a page was assigned. That way, a grid-dimension change or a window
    resize always ends with every visible page re-rendered crisply at
    whatever size it actually ends up occupying.
    """

    def __init__(self, viewer):
        super().__init__()
        self.viewer = viewer
        self.page_idx = None
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(1, 1)
        # So mouseMoveEvent fires even with no button held, to swap in a
        # pointing-hand cursor while hovering a link.
        self.setMouseTracking(True)
        self._update_background()
        self._page_pixmap = None  # full-res QPixmap currently loaded

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(RESIZE_DEBOUNCE_MS)
        self._render_timer.timeout.connect(self._render_full)

        # Drag state, shared by both buttons: which one is down (or None),
        # and the widget-space press/current points for drawing the
        # in-progress zoom box. Text selection can be made of several
        # non-contiguous segments (built with Ctrl-drag), in any mix of
        # panels: self.selections holds the committed ones, self._pending
        # the one currently being dragged (not yet committed).
        self._drag_button = None
        self._drag_start = None
        self._drag_current = None
        self.selections = []
        self._pending = None

        # Single-page slide animation (driven by the viewer's shared clock):
        # while active, paintEvent draws _anim_old_pixmap and the current
        # _page_pixmap at a horizontal offset instead of the plain resting
        # frame QLabel would otherwise draw.
        self._anim_old_pixmap = None
        self._anim_direction = 0
        self._anim_progress = None

    # ---------- left/right-drag: text selection vs. zoom box ----------

    def mousePressEvent(self, event):
        button = event.button()
        # A second mouse button pressed while the first is still held mid-drag
        # triggers the two-button gestures (see _handle_two_button_gesture):
        # right-drag then left = zoom the box into a single page; left-drag
        # then right = content-crop; center held then left = follow the citation
        # under the cursor out to a web search. Checked before the plain-button
        # branches so the chord wins over starting a fresh drag.
        if (self._drag_button is not None and button != self._drag_button
                and self.page_idx is not None
                and button in (Qt.LeftButton, Qt.RightButton)):
            self._handle_two_button_gesture(button)
            event.accept()
            return
        if button == Qt.MiddleButton:
            # Center-press no longer acts immediately: it starts a potential
            # pan (when zoomed in) and defers the decision to release, so a
            # drag can pan the zoomed view and a plain click still follows the
            # link. See mouseMoveEvent (pan) and mouseReleaseEvent (click).
            if self.page_idx is None:
                super().mousePressEvent(event)
                return
            self._drag_button = button
            self._drag_start = event.position().toPoint()
            self._drag_current = self._drag_start
            if self.viewer.crop_rect is not None:
                self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        # Back/Forward and chord+right-click are handled by an application-
        # wide event filter (see PdfGridViewer.eventFilter) instead of here,
        # since they need to survive a grid rebuild that can destroy and
        # replace *this* cell mid-gesture (e.g. chord+scroll changing the
        # column count while the chord button is still held down).
        if self.page_idx is None or button not in (Qt.LeftButton, Qt.RightButton):
            super().mousePressEvent(event)
            return
        self._drag_button = button
        self._drag_start = event.position().toPoint()
        self._drag_current = self._drag_start
        if self._drag_button == Qt.LeftButton:
            self._pending = None
            if not (event.modifiers() & Qt.ControlModifier):
                # No modifier always means "start a new selection" -- clear
                # every panel's existing selection first, so this behaves
                # identically whether the previous selection was in this
                # same panel or a different one. Ctrl held means "continue
                # the existing (possibly non-contiguous) selection" instead,
                # so nothing is cleared in that case.
                self.viewer.clear_all_selections()
        self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        # Keep the viewer's zoom focus on whatever page point is under the
        # cursor, so a cursor-anchored zoom (pinch / Ctrl+wheel / -,=) knows
        # where to zoom toward even between gestures.
        self.viewer.update_zoom_focus(self, event.position().toPoint())
        if self._drag_button is None:
            self._update_hover_cursor(event.position().toPoint())
            super().mouseMoveEvent(event)
            return
        if self._drag_button == Qt.MiddleButton:
            # Center-drag pans the zoomed crop by the incremental cursor delta
            # (a no-op when not zoomed in). Applied per move so the page tracks
            # the cursor; the re-render is coalesced by the same zoom throttle.
            prev = self._drag_current
            self._drag_current = event.position().toPoint()
            delta = self._drag_current - prev
            self.viewer.pan_crop(self, delta.x(), delta.y())
            event.accept()
            return
        self._drag_current = event.position().toPoint()
        if self._drag_button == Qt.LeftButton:
            self._pending = self._compute_selection(self._drag_start, self._drag_current)
        self.update()
        event.accept()

    def _cancel_drag(self):
        self._drag_button = None
        self._drag_start = None
        self._drag_current = None
        self._pending = None
        self.update()

    def _handle_two_button_gesture(self, new_button):
        # Called from mousePressEvent when a second button goes down during a
        # drag. Captures what it needs, cancels the in-progress drag (so the
        # later button releases are no-ops), and hands the actual work to the
        # viewer, which defers any grid rebuild to the event loop.
        page_idx = self.page_idx
        if self._drag_button == Qt.RightButton and new_button == Qt.LeftButton:
            box = QRect(self._drag_start, self._drag_current).normalized()
            moved = (self._drag_current - self._drag_start).manhattanLength() >= 6
            self._cancel_drag()
            if moved:
                self.viewer.zoom_box_to_single_page(page_idx, box, self)
            else:
                self.viewer.collapse_to_single_page_no_zoom(page_idx)
        elif self._drag_button == Qt.LeftButton and new_button == Qt.RightButton:
            self._cancel_drag()
            QTimer.singleShot(0, self.viewer.zoom_to_content_sampled)
        elif self._drag_button == Qt.MiddleButton and new_button == Qt.LeftButton:
            # Center held, then left-click: follow the citation under where the
            # center press landed out to a web search instead of jumping to its
            # target inside the document.
            end = self._drag_current
            self._cancel_drag()
            pt = self._widget_point_to_page_point(end)
            link = self.viewer.link_at(page_idx, pt) if pt is not None else None
            if link is not None:
                self.viewer.follow_link_to_web_search(link, page_idx)
        else:
            # Any other second-button combination during a drag: abandon the
            # in-progress drag cleanly rather than leaving it half-open.
            self._cancel_drag()

    def mouseReleaseEvent(self, event):
        if self._drag_button != event.button() or self._drag_button is None:
            super().mouseReleaseEvent(event)
            return
        button = self._drag_button
        start, end = self._drag_start, event.position().toPoint()
        self._drag_button = None
        self._drag_start = None
        MIN_DRAG_PX = 6
        moved_enough = (end - start).manhattanLength() >= MIN_DRAG_PX
        if button == Qt.MiddleButton:
            # A drag was a pan (already applied live in mouseMoveEvent); a plain
            # click follows the link under the cursor in a new copy, matching the
            # old center-click behaviour.
            self._drag_current = None
            if not moved_enough:
                pt = self._widget_point_to_page_point(end)
                link = self.viewer.link_at(self.page_idx, pt) if pt is not None else None
                if link is not None:
                    self.viewer.follow_link(link, self.page_idx, in_new_copy=True)
            self._update_hover_cursor(end)
            self.update()
            event.accept()
            return
        if button == Qt.RightButton:
            if moved_enough:
                self.viewer.zoom_to_box(self.page_idx, QRect(start, end).normalized(), self)
        else:
            if moved_enough:
                segment = self._compute_selection(start, end)
                if segment is not None:
                    self.selections.append(segment)
            else:
                pt = self._widget_point_to_page_point(end)
                link = self.viewer.link_at(self.page_idx, pt) if pt is not None else None
                if link is not None:
                    self.viewer.follow_link(link, self.page_idx)
            self._pending = None
            self.viewer.on_selection_changed()
        self.update()
        event.accept()

    def _update_hover_cursor(self, widget_pt):
        link = None
        if self.page_idx is not None:
            pt = self._widget_point_to_page_point(widget_pt)
            if pt is not None:
                link = self.viewer.link_at(self.page_idx, pt)
        self.setCursor(Qt.PointingHandCursor if link is not None else Qt.ArrowCursor)

    def _compute_selection(self, start_widget, end_widget):
        p1 = self._widget_point_to_page_point(start_widget)
        p2 = self._widget_point_to_page_point(end_widget)
        if p1 is None or p2 is None:
            return None
        return self.viewer.compute_text_selection(self.page_idx, p1, p2)

    def _widget_point_to_clip_fraction(self, widget_pt):
        # (fx, fy) in [0, 1] of where widget_pt falls within the displayed
        # page image (the current clip), or None if nothing is displayed.
        displayed = self.pixmap()
        if self.page_idx is None or displayed is None or displayed.isNull():
            return None
        disp = displayed.deviceIndependentSize()
        disp_w, disp_h = disp.width(), disp.height()
        if disp_w <= 0 or disp_h <= 0:
            return None
        offset_x = (self.width() - disp_w) / 2
        offset_y = (self.height() - disp_h) / 2
        fx = max(0.0, min(1.0, (widget_pt.x() - offset_x) / disp_w))
        fy = max(0.0, min(1.0, (widget_pt.y() - offset_y) / disp_h))
        return (fx, fy)

    def _widget_point_to_page_point(self, widget_pt):
        frac = self._widget_point_to_clip_fraction(widget_pt)
        if frac is None:
            return None
        page = self.viewer.doc.load_page(self.page_idx)
        clip = self.viewer._effective_clip_rect(page)
        return (clip.x0 + frac[0] * clip.width, clip.y0 + frac[1] * clip.height)

    def _page_rect_to_widget_rect(self, page_rect, clip, offset_x, offset_y, disp_w, disp_h):
        ix0, iy0 = max(page_rect.x0, clip.x0), max(page_rect.y0, clip.y0)
        ix1, iy1 = min(page_rect.x1, clip.x1), min(page_rect.y1, clip.y1)
        if ix1 <= ix0 or iy1 <= iy0:
            return None
        fx0 = (ix0 - clip.x0) / clip.width
        fy0 = (iy0 - clip.y0) / clip.height
        fx1 = (ix1 - clip.x0) / clip.width
        fy1 = (iy1 - clip.y0) / clip.height
        return QRectF(
            offset_x + fx0 * disp_w, offset_y + fy0 * disp_h,
            (fx1 - fx0) * disp_w, (fy1 - fy0) * disp_h,
        )

    def begin_slide(self, old_pixmap, direction):
        self._anim_old_pixmap = old_pixmap
        self._anim_direction = direction
        self._anim_progress = 0.0
        self.update()

    def set_slide_progress(self, progress):
        self._anim_progress = progress
        self.update()

    def end_slide(self):
        if self._anim_progress is not None:
            self._anim_old_pixmap = None
            self._anim_progress = None
            self.update()

    def _is_sliding(self):
        # Note: don't also require _anim_old_pixmap here. A cell that was
        # previously blank (off the start/end of the document) has no old
        # pixmap to slide out, but its new content must still slide in --
        # requiring both used to make that specific case skip straight to
        # super().paintEvent(), popping the final page in instantly instead
        # of animating it.
        return self._anim_progress is not None

    def _draw_slide_layer(self, painter, pixmap, x_offset):
        if pixmap is None or pixmap.isNull():
            return
        logical = pixmap.deviceIndependentSize()
        x = (self.width() - logical.width()) / 2 + x_offset
        y = (self.height() - logical.height()) / 2
        painter.drawPixmap(QPointF(x, y), pixmap)

    def paintEvent(self, event):
        if self._is_sliding():
            # Departing (old) content slides fully off the leading edge
            # while arriving (new) content slides in from the trailing
            # edge -- forward moves everything leftward, backward rightward.
            painter = QPainter(self)
            progress = self._anim_progress
            direction = self._anim_direction
            cell_w = self.width()
            old_offset = -direction * progress * cell_w
            new_offset = direction * (1 - progress) * cell_w
            self._draw_slide_layer(painter, self._anim_old_pixmap, old_offset)
            self._draw_slide_layer(painter, self._page_pixmap, new_offset)
            painter.end()
        else:
            super().paintEvent(event)
        if self.page_idx is None:
            return

        painter = QPainter(self)

        # Search highlights and the committed text selection are anchored
        # to PAGE coordinates, so they need the current clip + displayed
        # image geometry to place on screen; the in-progress zoom box below
        # is transient and already in widget coordinates.
        displayed = self.pixmap()
        if displayed is not None and not displayed.isNull():
            disp = displayed.deviceIndependentSize()
            disp_w, disp_h = disp.width(), disp.height()
            if disp_w > 0 and disp_h > 0:
                offset_x = (self.width() - disp_w) / 2
                offset_y = (self.height() - disp_h) / 2
                page = self.viewer.doc.load_page(self.page_idx)
                clip = self.viewer._effective_clip_rect(page)

                toc_rects = self.viewer.get_toc_text_highlights(self.page_idx)
                if toc_rects:
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(TOC_TEXT_HIGHLIGHT_FILL)
                    for r in toc_rects:
                        wr = self._page_rect_to_widget_rect(r, clip, offset_x, offset_y, disp_w, disp_h)
                        if wr is not None:
                            painter.drawRect(wr)

                matches = self.viewer.get_search_highlights(self.page_idx)
                if matches:
                    painter.setPen(Qt.NoPen)
                    for r, is_current in matches:
                        wr = self._page_rect_to_widget_rect(r, clip, offset_x, offset_y, disp_w, disp_h)
                        if wr is not None:
                            painter.setBrush(SEARCH_CURRENT_FILL if is_current else SEARCH_OTHER_FILL)
                            painter.drawRect(wr)

                painter.setPen(QPen(SELECTION_BORDER, 1))
                painter.setBrush(SELECTION_FILL)
                segments = self.selections if self._pending is None else self.selections + [self._pending]
                for seg in segments:
                    for r in seg["rects"]:
                        wr = self._page_rect_to_widget_rect(r, clip, offset_x, offset_y, disp_w, disp_h)
                        if wr is not None:
                            painter.drawRect(wr)

        if self._drag_button == Qt.RightButton and self._drag_start is not None:
            painter.setPen(QPen(ZOOM_BORDER, 1))
            painter.setBrush(ZOOM_FILL)
            painter.drawRect(QRectF(QRect(self._drag_start, self._drag_current).normalized()))

    def set_page(self, page_idx):
        """Assigns this cell a new page without rendering it -- the viewer
        dispatches the actual render (synchronously, or via a background
        task) and calls apply_rendered_pixmap once it's ready, so changing
        pages never blocks here on PyMuPDF rasterization."""
        self.page_idx = page_idx
        self.selections = []
        self._pending = None
        if page_idx is None:
            self._page_pixmap = None
            self.clear()

    def apply_rendered_pixmap(self, pixmap):
        self._page_pixmap = pixmap
        self.setPixmap(pixmap)

    def show_render_error(self, page_idx):
        # The traceback itself already went to stderr (see _on_render_done)
        # -- this is just enough for the user to notice something's wrong
        # and know what to paste back when reporting it.
        self._page_pixmap = None
        self.clear()
        self.setWordWrap(True)
        self.setText(f"⚠ Failed to render page {page_idx + 1}\n(see terminal output for details)")

    def _update_background(self):
        color = DARK_BACKGROUND_COLOR if self.viewer.dark_mode else BACKGROUND_COLOR
        self.setStyleSheet(f"background-color: {color};")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._page_pixmap is not None:
            # Immediate low-cost feedback while the debounce settles.
            self._apply_scaled()
        if self.page_idx is not None:
            self._render_timer.start()

    def _render_full(self, size_hint=None):
        if self.page_idx is None:
            return
        # Right after a grid change, this widget's own size() may not yet
        # reflect the geometry Qt is about to apply (that lands a moment
        # later via resizeEvent). Accepting a precomputed size_hint lets the
        # caller render at the correct resolution immediately instead of
        # waiting on that round trip -- the resizeEvent debounce below will
        # still self-correct if the real applied size differs at all.
        size = size_hint if size_hint is not None else self.size()
        self._page_pixmap = self.viewer.render_page(self.page_idx, size)
        # render_page already renders at exactly the resolution this size
        # needs, so set it directly: an extra QPixmap.scaled() pass here used
        # to soften every multi-cell grid.
        self.setPixmap(self._page_pixmap)

    def _apply_scaled(self):
        # Cheap interim rescale of the last full-res render, shown only
        # while a live window-resize is being debounced. _render_full above
        # replaces this with a pixel-exact render moments later.
        if self._page_pixmap is None:
            return
        # QPixmap.scaled works in physical pixels and keeps the source's
        # DPR, so scaling to self.size() (logical) used to produce an interim
        # image a DPR factor too small on any monitor with scale > 1.
        dpr = self._page_pixmap.devicePixelRatio()
        # Same round-trip-safe target as the real renders, though this
        # interim is smooth-scaled anyway.
        tw, th = _render_targets(self.width(), self.height(), dpr)
        target = QSize(tw, th)
        scaled = self._page_pixmap.scaled(
            target, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        scaled.setDevicePixelRatio(dpr)
        self.setPixmap(scaled)
