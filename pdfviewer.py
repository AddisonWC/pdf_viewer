#!/usr/bin/env python3
"""Grid PDF viewer for reading dense math papers and books.

Shows an MxN grid of pages and slides it forward one page at a time, so a
few pages stay visible together. See README.md and FEATURES.md for what it
is and why; docs/ARCHITECTURE.md for how it is built.

The implementation is the `viewer` package next to this file; this is the
entry point. Importing `viewer` sets the Qt platform before anything loads
PySide6, so keep that import ahead of any Qt import here.

Usage: pdfviewer.py FILE.pdf [--rows R] [--cols C]
"""
import argparse
import sys

import viewer  # noqa: F401  (sets QT_QPA_PLATFORM; must precede Qt imports)
from PySide6.QtWidgets import QApplication, QMessageBox

from viewer.window import PdfGridViewer


def parse_args():
    parser = argparse.ArgumentParser(description="Minimal grid PDF viewer")
    parser.add_argument("pdf", help="Path to the PDF file to open")
    parser.add_argument("--rows", type=int, default=1, help="Initial number of rows")
    parser.add_argument("--cols", type=int, default=2, help="Initial number of columns")
    return parser.parse_args()


def main():
    args = parse_args()
    app = QApplication(sys.argv)
    try:
        window = PdfGridViewer(args.pdf, rows=max(1, args.rows), cols=max(1, args.cols))
    except Exception as exc:
        # A bad path, a non-PDF file, a corrupt PDF, or an encrypted one the
        # password prompt didn't unlock all raise here (from the read + probe
        # open in _DocumentSource) -- without this, that's a raw Python
        # traceback in the terminal instead of a clean message.
        QMessageBox.critical(None, "PDF Viewer", f"Couldn't open {args.pdf!r}:\n\n{exc}")
        sys.exit(1)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
