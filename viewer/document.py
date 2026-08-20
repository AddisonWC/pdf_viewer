"""Reading the PDF's bytes, and every fitz.Document made from them."""
import os
import sys
import threading
import time
import traceback

import fitz
from PySide6.QtWidgets import QInputDialog, QLineEdit


class EncryptedPdfError(Exception):
    """An encrypted document nobody supplied a working password for."""


def _prompt_for_password(path, again):
    """Modal password prompt; None if the user cancels.

    This runs before the main window exists, which is fine: main() builds the
    QApplication first, and a parentless dialog is what a startup prompt
    wants anyway."""
    label = ("Wrong password. Try again:" if again else
             f"{os.path.basename(path)} is password-protected.\n\nPassword:")
    password, ok = QInputDialog.getText(
        None, "PDF Viewer", label, QLineEdit.EchoMode.Password)
    return password if ok else None


def _resolve_password(doc, path, ask):
    """Ask until the password authenticates, or the user gives up.

    Encrypted files need this because MuPDF opens one happily and only fails
    once a page is actually parsed -- so without a check here an encrypted
    PDF used to open a normal-looking window that filled with render errors.

    Files with an empty user password never reach this: MuPDF authenticates
    those itself while opening and leaves needs_pass False."""
    again = False
    while True:
        password = ask(path, again)
        if password is None:
            raise EncryptedPdfError(
                "This PDF is password-protected and no password was given.")
        if doc.authenticate(password):
            return password
        again = True


class _DocumentSource:
    """The document's bytes, read once and held for the reader's lifetime.

    Every fitz.Document -- the main thread's, one per worker thread, and any
    copy window's -- comes from THIS buffer. Once it is up the path is never
    touched again, so the file can be moved, renamed, deleted or rewritten
    under an open reader with no effect.

    Opening by path was fragile in a non-obvious way: MuPDF parses lazily, so
    fitz.open(path) keeps reading from the file all session. An already-open
    Document survives a rename (its fd follows the inode), which is why the
    bug looked intermittent -- but this viewer opens a FRESH Document per
    render thread and per copy window, and those later opens go through the
    path, so the first page rendered on a cold thread after a move died.
    Rewrite-in-place (LaTeX recompiling the paper you are reading) was worse:
    no error, just a Document reading objects at byte offsets that had moved.

    Per-thread Documents stay separate objects, since one Document is not
    safe to render from concurrently, but they all wrap the same buffer.
    PyMuPDF does not copy stream data when the buffer is READ-ONLY -- it
    points MuPDF's fz_stream at it in place -- so N Documents cost one copy
    of the file in total. Read-only is the load-bearing word: a plain
    bytearray is copied per Document. Hence readinto a bytearray while
    loading, published as a read-only memoryview when complete, which also
    keeps peak RAM at one copy rather than the two b"".join would need.

    The read runs in the BACKGROUND: a synchronous slurp would freeze
    startup for tens of seconds on the large scans this is pointed at. The
    reader opens by path and is usable immediately; a daemon thread fills the
    buffer, and Documents handed out afterwards come off it (_generation
    bumps, retiring the per-thread Documents). Until the swap lands, behaviour
    is what it was before this class existed.

    It is CHUNKED WITH A BREATHER rather than one read(), because a single
    huge read monopolises the device queue and every small page read the
    viewer needs lands behind it -- enough to make startup ~8x slower.
    Chunking costs the slurp nothing measurable, so there is nothing to tune
    here; both ends are best-case. Numbers in docs/DEVLOG.md.

    The read comes off a handle opened in __init__ and held, NOT off the
    path, so a move or delete during the load cannot make it fail: the handle
    keeps the inode alive. New opens inside that window are covered too (see
    open()).
    """

    _READ_CHUNK = 1 << 20
    _READ_PAUSE_S = 0.010

    def __init__(self, path, ask_password=None):
        self.path = path
        # MuPDF picks its parser from `filetype` for stream opens (there's no
        # filename to sniff). The extension is right ~always; "pdf" covers
        # oddities like a .pdf.part download name.
        self._filetype = os.path.splitext(path)[1].lstrip(".").lower() or "pdf"
        # Opened now and held for the whole load: this handle, not the path,
        # is what the background read consumes.
        self._fh = open(path, "rb")
        self._data = None       # written exactly once, by _read_all
        self._ready = threading.Event()
        # Bumped when _data lands, so per-thread Documents opened by path
        # during the load get retired and reopened off the buffer.
        self._generation = 0
        self._local = threading.local()
        # Encryption is a property of the FILE, so every Document opened later
        # -- per-thread, copy window, off the buffer or off the path -- has to
        # be unlocked again. Ask once, here, and replay it in _open.
        self._password = None
        probe = fitz.open(path)  # a bad path or a corrupt file raises here, as before
        try:
            if probe.needs_pass:
                self._password = _resolve_password(
                    probe, path, ask_password or _prompt_for_password)
        finally:
            probe.close()
        threading.Thread(target=self._read_all, name="pdf-slurp", daemon=True).start()

    @property
    def ready(self):
        """True once the document is fully RAM-resident."""
        return self._data is not None

    def _read_all(self):
        try:
            size = os.fstat(self._fh.fileno()).st_size
            buf = bytearray(size)
            view = memoryview(buf)
            pos = 0
            while pos < size:
                got = self._fh.readinto(view[pos:pos + self._READ_CHUNK])
                if not got:
                    break        # file shrank mid-read; keep what's really there
                pos += got
                time.sleep(self._READ_PAUSE_S)
            view.release()
            if pos < size:
                del buf[pos:]
            else:
                buf += self._fh.read()   # ...or grew
            # Read-only view: this is what makes PyMuPDF share the buffer
            # across Documents instead of copying it for each one.
            self._data = memoryview(buf).toreadonly()
            self._generation += 1        # ordered after _data: readers see both or neither
        except Exception:
            print(f"pdfviewer: could not buffer {self.path!r}, staying on the file:\n"
                  f"{traceback.format_exc()}", file=sys.stderr)
        finally:
            try:
                self._fh.close()
            except Exception:
                pass
            self._ready.set()

    def open(self):
        """A new independent Document -- off the buffer once it's up."""
        data = self._data
        if data is None:
            try:
                return self._open(self.path)
            except Exception:
                # The path went away mid-load. The held handle still points
                # at the inode, and /proc/self/fd exposes that as a path MuPDF
                # can open even once the name is gone. Failing that, block
                # until the background read hands over the buffer.
                doc = self._open_via_handle()
                if doc is not None:
                    return doc
                self._ready.wait()
                data = self._data
                if data is None:
                    raise
        return self._open(stream=data, filetype=self._filetype)

    def _open(self, *args, **kwargs):
        """fitz.open, plus the password if the document wants one.

        Authentication lives on the Document rather than on the file, so each
        new one starts locked and has to be let in again."""
        doc = fitz.open(*args, **kwargs)
        if doc.needs_pass:
            if self._password is None or not doc.authenticate(self._password):
                doc.close()
                raise EncryptedPdfError(f"Could not unlock {self.path!r}.")
        return doc

    def _open_via_handle(self):
        try:
            return self._open(f"/proc/self/fd/{self._fh.fileno()}")
        except Exception:
            return None   # not Linux, or the read already finished and closed it

    def get(self):
        """The calling worker thread's own Document, created on first use."""
        generation = self._generation
        entry = getattr(self._local, "entry", None)
        if entry is None or entry[0] != generation:
            # Dropping the old Document (rather than closing it) lets
            # refcounting close it once nothing is still reading from it.
            entry = (generation, self.open())
            self._local.entry = entry
        return entry[1]
