#!/usr/bin/env python3
"""Runs pdfviewer.py under XWayland (xcb) instead of the native Wayland it
uses by default. Kept for reference, NOT recommended -- see the verdict
below; pdfviewer.py's own QT_QPA_PLATFORM comment has the original
sharpness measurements that started this investigation.

The appeal of xcb is real cross-window positioning (center-click's
"positioned exactly over this page", and normal window-manager
repositioning hotkeys), which native Wayland's protocol refuses outright
to any ordinary client. But plain xcb collapses every monitor to one
rounded-up global integer scale factor instead of each one's real
per-monitor scale, which visibly softens rendering on a mixed-DPI
multi-monitor setup. This variant tries to correct that: before Qt
starts, it asks Mutter directly (GetCurrentState -- an ordinary,
unprivileged D-Bus call) for each monitor's *real* Wayland-native scale,
and passes that to Qt via QT_SCREEN_SCALE_FACTORS, computed fresh at every
launch rather than hardcoded.

Verdict, after actually testing this: don't use it. Two separate problems
surfaced, neither of which has a clean fix:
  1. Forcing the "correct" per-monitor DPR breaks a different, unrelated
     arithmetic coincidence XWayland's uncorrected DPR was accidentally
     relying on: XWayland's raw/virtual screen geometry is itself already
     inflated relative to Wayland's true logical size (by that same
     global rounded-up factor), and Qt derives the *logical* screen size
     everything gets laid out against as raw-geometry / DPR. With the
     wrong-but-matching DPR, that division happens to cancel out and
     recover the correct logical size; with the "correct" per-monitor
     DPR, it doesn't, and the whole window/toolbar/font layout ends up
     computed against a too-large logical canvas -- which is what made
     toolbar and help-view text render distinctly smaller than intended,
     with no simple fix (font point size doesn't touch this at all: see
     the git history around when that was diagnosed).
  2. Independent of (1), this variant measured meaningfully slower than
     native Wayland in practice -- not from oversized image rendering
     (checked directly: render_page() targets were comparable, sometimes
     smaller, than native Wayland's), but most likely the extra per-frame
     compositing XWayland has to do to reconcile its own inflated virtual
     screen against the real physical monitor on every redraw, which
     native Wayland clients never pay at all.
Both are structural properties of going through XWayland on this kind of
mixed-DPI setup, not bugs in this file. pdfviewer's default (native
Wayland, imprecise/near-parent window placement via set_parent) is the
better trade-off.
"""
import os
import re
import subprocess


def _real_monitor_scales():
    """{connector_name: real_scale} from Mutter, or {} if unavailable."""
    try:
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.DisplayConfig",
             "--object-path", "/org/gnome/Mutter/DisplayConfig",
             "--method", "org.gnome.Mutter.DisplayConfig.GetCurrentState"],
            capture_output=True, text=True, check=True, timeout=2,
        ).stdout
    except Exception:
        return {}
    # GVariant's pretty-printer only prints the "uint32" type annotation on
    # the first element of an array with that type -- later elements of the
    # same type print the bare number, so that prefix has to be optional.
    scales = {}
    for m in re.finditer(r"\(\d+, \d+, ([\d.]+), (?:uint32 )?\d+, (?:true|false), \[\('([^']+)'", out):
        scale, connector = m.groups()
        scales[connector] = float(scale)
    return scales


os.environ["QT_QPA_PLATFORM"] = "xcb"
scales = _real_monitor_scales()
if scales:
    os.environ["QT_SCREEN_SCALE_FACTORS"] = ";".join(f"{name}={s:g}" for name, s in scales.items())

from pdfviewer import main

if __name__ == "__main__":
    main()
