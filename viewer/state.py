"""Last-read-page persistence."""
import json
import os
import tempfile


# Addison asked the viewer to remember where he was in each document. Just
# the page: grid size and crop describe how this window happens to be set
# up, not the document. Best-effort throughout -- a missing or corrupt
# state file must never break opening or closing a document.

def _state_file_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "pdfviewer", "last_page.json")


def _load_state():
    try:
        with open(_state_file_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _load_last_page(path):
    return _load_state().get(os.path.abspath(path))


def _save_last_page(path, first_page):
    state_path = _state_file_path()
    data = _load_state()
    data[os.path.abspath(path)] = first_page
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        # Write a temp file in the same directory and rename it over the
        # target: os.replace is atomic within a filesystem, so a crash or a
        # kill mid-write leaves the previous file intact. Writing in place
        # truncates first, and an interruption there loses the saved position
        # for EVERY document, not just this one.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(state_path),
                                   prefix="last_page.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, state_path)
        except Exception:
            # Leaving a stray temp file behind would litter the state dir on
            # every failed save, so clean up before giving up.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:
        pass
