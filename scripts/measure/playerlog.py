"""Counting DISTINCT errors in a RimWorld Player.log, rather than counting lines.

WHY THIS FILE EXISTS
====================
`artifacts.py` has classified `Player.log` since the beginning and refuses a blind
scan of it with the right words -- *"a grep -c counts LINES, and one error spanning
30 lines is not 30 errors"* -- and then named `harvest_log.py` as the instrument.
But `harvest_log.py` is not a Measurement: it prints a report. It cannot answer
"how many distinct errors" as MEASURED or UNMEASURED, so the rule the registry
states had nothing enforcing it. That gap is what LOG_AND_CSV_GET_INSTRUMENTS_1
is about, and this module is the enforcement.

THE TWO NUMBERS, AND WHY BOTH ARE REPORTED
==========================================
They are different questions and conflating them is the whole trap:

    occurrences   how many times something went wrong
    distinct      how many DIFFERENT things went wrong

101 cast defs failing on one shared bug is 101 occurrences and 1 distinct error.
A `grep -c` on that log answers neither -- it returns roughly 1,700, because it
counts every frame of every stack trace.

HOW A BLOCK IS RECOGNISED
=========================
An error block STARTS at a line matching a known RimWorld opener and CONTINUES
through its trace. Continuation is recognised structurally -- leading whitespace,
`at `, `(wrapper`, `[Ref `, `  - PREFIX/POSTFIX`, a bare `Parameter name:` -- so a
trace of any depth folds into the block that owns it.

⚠️ `[Ref XXXXXXXX] Duplicate stacktrace, see ref for original` is RimWorld's OWN
de-duplication and it is deliberately still counted as an occurrence: it means the
same fault happened again to a different def, which is the number that told us 101
characters were missing rather than one.

HOW DISTINCT IS DECIDED
=======================
The opener is normalised before grouping: hex refs, decimal numbers, GUID-ish
module ids and Windows/Unix paths are masked. So

    Exception loading def from file CastRoster_BLACKSTAR.xml: ...
    Exception loading def from file CastRoster_DEEPWATER.xml: ...

collapse to one distinct error, which is the honest answer -- one bug, twelve files.
🔑 The masking is the judgement call in this module. It is listed in `_MASKS` so it
can be read and argued with, rather than hidden in a regex.

⛔ THIS IS NOT A PARSER FOR ANYTHING ELSE. It answers one question. If the log does
not look like a RimWorld Player.log at all, it returns UNMEASURED rather than 0 --
"no errors" and "I could not tell" must never render the same.
"""

from __future__ import annotations

import hashlib
import os
import re

from . import artifacts
from .result import Measured, Refused, Unmeasured

#: A line that OPENS an error block. Deliberately explicit rather than a catch-all
#: "error" substring: `Log.Message` text routinely contains the word.
_OPENERS = (
    re.compile(r"^Exception loading def from file "),
    re.compile(r"^Could not resolve cross-reference"),
    re.compile(r"^Could not load reference to "),
    re.compile(r"^Config error in "),
    re.compile(r"^XML error"),
    re.compile(r"^XML format error"),
    re.compile(r"^Error (in|while|loading|during|parsing|at )"),
    re.compile(r"^Exception (in|from|while|ticking|thrown)"),
    re.compile(r"^\[Def Error\]"),
    re.compile(r"^Failed to (load|find|resolve)"),
    re.compile(r"^Unhandled exception"),
    re.compile(r"^System\.[A-Za-z.]+Exception"),
    re.compile(r"^Tried to "),
)

#: A line that BELONGS to the block above it. Structural, not semantic, so a trace
#: of any depth or from any mod folds in.
_CONTINUATIONS = (
    re.compile(r"^\s"),
    re.compile(r"^at "),
    re.compile(r"^\(wrapper"),
    re.compile(r"^\[Ref [0-9A-Fa-f]+\]"),
    re.compile(r"^Parameter name:"),
    re.compile(r"^UnityEngine\."),
    re.compile(r"^Verse\."),
    re.compile(r"^RimWorld\."),
    re.compile(r"^\s*- (PREFIX|POSTFIX|TRANSPILER|FINALIZER)"),
)

#: What is masked before two openers are called the same error. Order matters:
#: paths before numbers, or a path's digits are masked first and the path pattern
#: no longer matches.
_MASKS = (
    (re.compile(r"[A-Za-z]:\\[^\s:]+"), "<path>"),
    (re.compile(r"/[^\s:]{4,}/[^\s:]+"), "<path>"),
    (re.compile(r"\b[\w.\-]+\.(xml|dll|cs|rws|png|json)\b", re.I), "<file>"),
    (re.compile(r"<[0-9a-f]{20,}>"), "<module>"),
    (re.compile(r"\b0x[0-9A-Fa-f]+\b"), "<hex>"),
    (re.compile(r"\b[0-9A-F]{8}\b"), "<ref>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    # ⚠️ BOUNDED AND WORD-AWARE ON PURPOSE. The first version of this was
    # r"'[^']*'", and the apostrophe in "doesn't" opened a quote that ran to the
    # next apostrophe hundreds of characters away, swallowing unrelated text into
    # a single <q>. A mask that eats the message is worse than no mask, because
    # the wrong two openers then collapse into one "distinct" error.
    (re.compile(r"(?<![A-Za-z])'[^'\n]{0,80}'(?![A-Za-z])"), "<q>"),
    (re.compile(r'"[^"\n]{0,80}"'), "<q>"),
)

#: Cheap proof that this really is a RimWorld log before any number is reported.
_SIGNATURES = ("RimWorld 1.", "Mono path[0]", "Ludeon Studios")


def _normalise(line: str) -> str:
    out = line.strip()
    for rx, rep in _MASKS:
        out = rx.sub(rep, out)
    return out[:200]


def _is_continuation(line: str) -> bool:
    return any(rx.match(line) for rx in _CONTINUATIONS)


def _is_opener(line: str) -> bool:
    return any(rx.match(line) for rx in _OPENERS)


def count_errors(path: str, top: int = 0):
    """-> Measurement. `top` > 0 also prints the commonest distinct openers."""
    if not os.path.exists(path):
        return Unmeasured(reason="no such file", artifact=path,
                          instrument="measure.count-errors")
    # 🔴 THE PATH MUST BE A PLAYER.LOG BEFORE ANY NUMBER IS REPORTED, and the
    # signature check below is NOT enough on its own: this project's own CLAUDE.md
    # contains both "RimWorld 1." and "Ludeon Studios" in ordinary prose and paths,
    # and the first version of this function happily returned MEASURED 0 for it.
    # A confident zero about the wrong file is precisely what this skill exists to
    # prevent, so classification comes first and REFUSES rather than measures.
    art = artifacts.classify(path)
    if art is None or art.kind != "playerlog":
        return Refused(
            reason="not a Player.log by name, so a count of its 'errors' would be "
                   "a number about the wrong question"
                   + (" (it classifies as %s)" % art.kind if art else ""),
            artifact=path, instrument="measure.count-errors",
            right_instrument="measure explain <path>, then the instrument it names")

    size = os.path.getsize(path)
    if size == 0:
        return Unmeasured(reason="the file is empty, which is not the same as "
                                 "a load with no errors",
                          artifact=path, instrument="measure.count-errors")

    digest = hashlib.sha256()
    seen_signature = False
    occurrences = 0
    distinct = {}
    lines = 0
    in_block = False

    with open(path, "rb") as fh:
        for raw in fh:
            digest.update(raw)
            lines += 1
            try:
                line = raw.decode("utf-8", "replace")
            except Exception:                       # pragma: no cover
                continue
            line = line.rstrip("\r\n")
            if not seen_signature and any(s in line for s in _SIGNATURES):
                seen_signature = True
            if not line.strip():
                in_block = False
                continue
            if _is_opener(line):
                occurrences += 1
                in_block = True
                key = _normalise(line)
                distinct[key] = distinct.get(key, 0) + 1
                continue
            if in_block and _is_continuation(line):
                continue
            in_block = False

    if not seen_signature:
        return Unmeasured(
            reason="no RimWorld signature line (RimWorld 1.x / Mono path / Ludeon "
                   "Studios) in the whole file, so this is not a Player.log and a "
                   "count of its 'errors' would be a plausible wrong number",
            artifact=path, instrument="measure.count-errors",
            remedy="point at the real Player.log, or read this file directly")

    against = "sha256:%s size=%d lines=%d" % (digest.hexdigest()[:16], size, lines)
    m = Measured(
        value=len(distinct),
        instrument="measure.count-errors",
        artifact=os.path.basename(path) + " distinct errors",
        against=against,
        evidence="%d occurrence(s) across %d distinct error(s)" % (occurrences, len(distinct)),
    )
    if top > 0:
        m.top = sorted(distinct.items(), key=lambda kv: -kv[1])[:top]   # type: ignore[attr-defined]
    return m
