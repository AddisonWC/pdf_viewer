"""Handing this document off to other programs."""
import os
import shlex
import shutil
import subprocess
import sys


# ---------- "Ask an agent about this PDF" launcher ----------
#
# Fire-and-forget by design: open an interactive session in a terminal,
# seeded only with this document's path, and never talk to it again. The
# viewer's whole job is handing off which file. pdf_helper_prompt.txt (next
# to this script) is appended to the session's system prompt so it reads the
# *page images* for notation questions -- math symbols don't survive text
# extraction. Model/effort are per-session flags, so no other session is
# affected. The terminal and CLI program are hard-wired here; FEATURES.md
# says they should be the user's choice, which is intent, not yet true.

def _launch_claude_helper(pdf_path, context=""):
    """Open gnome-terminal running `claude` on this PDF. `context`, if given,
    becomes the first prompt; if empty, Claude is told to stand by. Returns
    True if a terminal was launched."""
    pdf_path = os.path.abspath(pdf_path)
    workdir = os.path.dirname(pdf_path)
    # pdf_helper_prompt.txt sits beside pdfviewer.py, one level above this
    # package -- test_ask_claude_launcher asserts the path resolves, because
    # a wrong path here fails silently at the far end of a terminal launch.
    helper = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "pdf_helper_prompt.txt",
    )
    context = context.strip()
    if context:
        initial = f"I'm reading the PDF at {pdf_path}. {context}"
    else:
        initial = (f"You're going to answer questions about the PDF at {pdf_path}. "
                   "Wait for my instructions.")
    # Terminal, CLI program and model flags are hard-wired here. FEATURES.md
    # says the choice is the user's; that is a statement of intent, not yet
    # true, and making it true means making all four configurable.
    claude_argv = [
        "claude",
        "--model", "sonnet",
        "--effort", "high",
        "--append-system-prompt-file", helper,
        initial,
    ]
    term = shutil.which("gnome-terminal")
    if not term:
        print("Ask Claude: gnome-terminal not found on PATH", file=sys.stderr)
        return False
    # Run via a login shell so `claude` resolves on PATH; keep the window open
    # after it exits (exec bash) so the last answer stays readable.
    inner = " ".join(shlex.quote(a) for a in claude_argv) + "; exec bash"
    try:
        subprocess.Popen(
            [term, f"--working-directory={workdir}", "--", "bash", "-lc", inner],
            cwd=workdir, start_new_session=True,
        )
    except Exception as exc:  # never let a launch failure crash the viewer
        print(f"Ask Claude: failed to launch terminal: {exc}", file=sys.stderr)
        return False
    return True
