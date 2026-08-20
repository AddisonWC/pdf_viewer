# How to maintain these docs

For agents, before writing anything in `docs/`.

## The files

**ARCHITECTURE.md** — the rules that bind a change anywhere in the program, and
an index of the symbols to grep. A fact belongs here only if an agent working on
a *different* part would get it wrong without it.

**SUBSYSTEMS.md** — how one area is shaped, which parts are Addison's
requirements, and what is known to be missing. Opened when you are changing that
area.

**HISTORY.md** — only what the working code cannot show: approaches tried and
abandoned, alternatives deliberately rejected, bugs that still constrain the
design. If an agent could learn it from the code and its docs, it does not
belong here. A change that landed and worked is not history.

**DEVLOG.md** — one appended entry per session: what changed, what was measured.
The raw record, and where measurements live. Searched, not read start to finish.

**GOTCHAS.md** — library and platform behavior that has already cost debugging
time here. A lookup, for when something behaves unexpectedly.

ARCHITECTURE, HISTORY and this file are in every agent's context, so a wasted
line in them is paid for repeatedly; keep them short. SUBSYSTEMS, DEVLOG and
GOTCHAS are opened deliberately and can afford more length. `README.md`,
`FEATURES.md`, `designer_notes.txt` and `AGENTS.md`/`CLAUDE.md` are Addison's —
never edit them.

## What earns a line

Would an agent do the wrong thing without it? If not, cut it.

Each fact has one home. Cross-reference rather than restate: the same point in
two files is a line paid for twice and two things to keep true. That includes
what a file is for — it's described above, so no file introduces itself.

The comment at the code is the first home for anything that explains code. A doc
that restates one is a second copy to keep true — name the function and stop.

README and FEATURES describe the program to a reader; these docs describe it to
whoever has to change it. Repeat a point from them only where a code decision
turns on it, and then only the part that bears on the code.

## Where a fact goes

The *why* of what you changed goes in the comment on the code you changed, in
the same edit. Then:

- `SUBSYSTEMS.md`, when you changed the shape of an area or what's required in it.
- `ARCHITECTURE.md`, when the change binds agents working elsewhere in the
  program — a new invariant, or a principle Addison has stated.
- `DEVLOG.md`, every session, one entry.
- `HELP_HTML` in `pdfviewer.py`, for any key that changed. It is hand-written and
  drifts silently.
- `GOTCHAS.md`, when library or platform behavior cost you debugging time.
- `HISTORY.md`, rarely: when a later agent would otherwise retry something that
  already failed, or revert something for the reason it was already rejected. Under
  **Settled**, only a question with a live alternative someone could argue for.
  Working code with its rationale at the code needs nothing in HISTORY.

## Attribution

Attribution means stating outright that Addison specified something. Its only
job is to mark a design choice as a **requirement** rather than an agent's
choice that a later agent may revise. Make the choice the subject and the source
explicit:

```python
# Addison asked for the counter to stay as current as possible, so force the
# repaint rather than letting Qt coalesce it.
```

Naming him without saying he decided it — "Addison reads this number while
scrolling, so force the repaint" — attributes nothing. It asserts a habit and
uses it as rationale, leaving a later agent unable to tell whether the behaviour
is required. Describing how he uses the program is fine as explanation; it just
confers no requirement status.

- Only for things he actually specified, in his wording where it is distinctive.
  If you don't know who decided something, say nothing.
- **Attribute the decision, not your explanation of it.** A reason he gave is
  his; a reason you reconstructed is yours, and his name turns it into testimony.
- **Never give two of his decisions a shared reason.** If he ruled on several at
  once, give each its own or give none.
- Attach the claim to the concrete choice. A broad statement about his
  philosophy is the easiest to get subtly wrong and the hardest for him to catch.
- **An unattributed decision is not an unimportant one.** Most of the code is
  agent-written; no name means nobody recorded who decided it.
- Say so where something is explicitly open to revision. Don't attribute
  anything to yourself or to a model.

## Writing rules

- **The code is the truth; docs are point-in-time.** Verify before asserting.
  Don't cite line numbers, they rot — name functions and constants.
- **Label machine-specific measurements.** See the header of `DEVLOG.md`.
- **No agent-interaction preferences.** How Addison likes to be worked with is
  handled outside this repo. `docs/` describes the project.
- **Don't generate memories.** Everything a future agent needs goes here.

## Comments versus docs

A comment carries the *why* of the code it sits on: design intent, and traps
that would cause a regression if forgotten. It is the primary explanation, and
the docs point at it rather than copying it. Keep trap warnings to a line or two
and leave the full explanation in `GOTCHAS.md`.

Three things do not belong in the code:

- benchmark numbers, and the story of how they were measured → `DEVLOG.md`
- accounts of past bugs → `HISTORY.md`, and only if the bug still constrains
  something today
- anything describing the project rather than the code it sits on
