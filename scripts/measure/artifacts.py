"""What each large artifact IS, and which instrument may speak about it.

One registry, read by two consumers that must never disagree:

  * the Measurement library (option D) — to refuse a question it cannot judge
  * the PreToolUse hook (option E) — to refuse a byte scan before the wrong
    number exists

Keeping them on one table is the point. A hook that refuses `grep` while naming
an instrument that does not answer the question is worse than no hook.

⚠️ Scope is EVERY large scanned artifact — owner, 2026-08-21. Not the def dump
alone. For the two we do not own — the `.rws` and third-party DLLs — no format
change is ever possible, so the answer there is D + E only: a correct reader,
and a hook that says so.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Artifact:
    kind: str
    #: Glob patterns, matched against the path with forward slashes. `*` here
    #: crosses directory separators, like DEPLOY_HOLD.txt and unlike a shell.
    patterns: tuple
    #: Why a byte scan of this returns a plausible wrong number.
    encoding: str
    #: The instrument that DOES answer questions about it. Named in every
    #: refusal, because a refusal without a cheap alternative is an obstacle.
    instrument: str
    #: Questions a byte scan answers correctly anyway. A literal-string grep of
    #: a savegame is legitimate; a semantic one is not.
    literal_scan_ok: bool = False
    #: Can this artifact's format be changed by us at all?
    ours: bool = True
    notes: str = ""
    #: The measured incident that put it on this list, if there was one.
    incident: str = ""


REGISTRY = (
    Artifact(
        kind="defdump",
        # ⚠️ `manifest.json` is deliberately NOT matched. It is ~320 KB of
        # pretty-printed JSON — grepping it is legitimate and often correct,
        # and refusing it would be an unearned refusal on the one file in the
        # dump a human CAN read. (The manifest's trap is `json.load` silently
        # dropping duplicate defCounts keys, which a grep actually exposes.)
        patterns=("*/DefDump/defs/*", "*/DefDump/animals.json"),
        encoding=(
            "one JSON object per def type on a SINGLE line, up to 331 MB — "
            "a grep hit is a fragment of a record, never a record, and the "
            "line count is always 1"
        ),
        instrument="python3 src/RimMandrake/measure/cli.py count <DefType>",
        literal_scan_ok=False,
        ours=True,
        incident=(
            "the dump keyed files on the SIMPLE type name; 532 types share 517 "
            "names, 13 files overwrote each other and 824 defs were destroyed. "
            "AbilityDef read 0 having written 612."
        ),
    ),
    Artifact(
        kind="savegame",
        patterns=("*.rws",),
        encoding=(
            "plain XML wrapping base64/raw-DEFLATE map grids of 2-byte def "
            "shortHashes — biomes, terrain and things are INDICES into a "
            "compressed grid, not defNames, so they are not present as text"
        ),
        instrument=(
            "python3 src/RimMandrake/Utils/rimbench/savemap.py, or the bridge "
            "for a live game"
        ),
        literal_scan_ok=True,
        ours=False,
        notes=(
            "Ludeon's format; no format work is possible or planned. Grepping "
            "for a literal string is legitimate — grep '<def>NAME</def>', not "
            "the bare defName. Asking a COUNT of anything grid-borne is not."
        ),
        incident=(
            "grep for biome defNames returned 2 where the answer was 3 / 233 / 31"
        ),
    ),
    Artifact(
        kind="assembly",
        patterns=("*.dll",),
        encoding=(
            ".NET metadata: attribute strings, method bodies and custom-attribute "
            "blobs are NOT UTF-16 literals, so `strings -a -el` sees a minority "
            "of them"
        ),
        instrument=(
            "the bridge's own tool list for JawaBench, or a metadata reader; "
            "never `strings` for a census"
        ),
        literal_scan_ok=True,
        ours=False,
        notes=(
            "Ours are built from source in src/, so read the .cs instead. For "
            "third-party assemblies a literal-string grep is fine; a COUNT is not."
        ),
        incident="`strings -a -el` on the companion DLL found 16 of 115 tool names",
    ),
    Artifact(
        kind="worldcsv",
        patterns=("world/*.csv", "*/world/*.csv"),
        encoding=(
            "CSV with a header row; a grep count includes the header and any "
            "value that merely CONTAINS the token in another column"
        ),
        instrument="python3 src/RimMandrake/measure/cli.py csv <path> --where col=value",
        literal_scan_ok=True,
        ours=True,
        notes=(
            "The tiles CSV is 1.7 MB / ~20k rows — readable whole by a script, "
            "never by an agent. It is frozen against ashkarr_paint.py."
        ),
    ),
    Artifact(
        kind="playerlog",
        patterns=("*/Player.log", "*/Player-prev.log", "*Player.log"),
        encoding=(
            "multi-line stack traces and repeated identical lines; a grep -c "
            "counts LINES, and one error spanning 30 lines is not 30 errors"
        ),
        instrument="python3 src/RimMandrake/Utils/harvest_log.py",
        literal_scan_ok=True,
        ours=False,
        notes=(
            "Grepping for a literal error string to see IF it occurred is the "
            "correct use. Counting occurrences to decide severity is not."
        ),
    ),
)


#: Scanning tools that return a number with no error when they do not
#: understand the encoding. This is the list the hook gates on.
BLIND_SCANNERS = ("grep", "egrep", "fgrep", "rg", "ag", "strings", "wc", "awk", "sed")


def classify(path: str):
    """Return the Artifact this path is, or None.

    Matching is on the forward-slash form so the same pattern works for a
    /mnt/c path, a repo-relative path and a Windows path someone pasted.
    """
    if not path:
        return None
    p = path.replace("\\", "/")
    # A bare relative path should still match "world/*.csv".
    candidates = [p, p.lstrip("./")]
    if not p.startswith("/"):
        candidates.append("/" + p)
    for art in REGISTRY:
        for pat in art.patterns:
            for cand in candidates:
                if fnmatch.fnmatch(cand, pat):
                    return art
    return None


def is_big(path: str, threshold: int = 2_000_000) -> bool:
    """Size is the middle link in the causal chain, so it is also a trigger.

    An artifact small enough to read whole does not need an instrument; the
    honest answer there is "read it".
    """
    try:
        return os.path.getsize(path) >= threshold
    except OSError:
        return False


def refusal_text(art: Artifact, tool: str, path: str) -> str:
    """The message a refusal shows. It must teach, and it must name the fix."""
    article = "an" if art.kind[0] in "aeiou" else "a"
    lines = [
        f"BLIND SCAN REFUSED — `{tool}` on {article} {art.kind} ({path})",
        "",
        f"Why: {art.encoding}.",
    ]
    if art.incident:
        lines += ["", f"This already happened: {art.incident}"]
    lines += ["", f"Use instead: {art.instrument}"]
    if art.literal_scan_ok:
        lines += [
            "",
            "A LITERAL-string scan of this artifact is legitimate — searching "
            "whether an exact string occurs. What is refused is a semantic "
            "question (a count, a census, 'which defs/biomes/tools are here').",
            "If you are searching for a literal string and know why the count "
            "does not matter, re-run with MEASURE_ALLOW_SCAN=1 in the "
            "environment and say in your next message that you did.",
        ]
    else:
        lines += [
            "",
            "There is no legitimate byte scan of this artifact: it holds no "
            "line structure to count and no record a hit can be read back to. "
            "MEASURE_ALLOW_SCAN=1 overrides this if you are certain.",
        ]
    return "\n".join(lines)
