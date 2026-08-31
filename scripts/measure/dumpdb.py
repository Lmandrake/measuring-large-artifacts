"""The def dump as SQLite — option C of SCANNED_ARTIFACTS_CANNOT_LIE_1.

Why SQLite and not JSONL (owner's choice, 2026-08-21): `NULL`, `0` and "no row"
are three different things without anyone maintaining a convention, and a count
is one number that costs no parsing and no context. The price, accepted openly,
is that the dump stops being diffable and greppable.

⚠️ **Both formats are written for one capture cycle.** This module never deletes
the JSON. `verify_against_json()` re-reads the source and checks it row for row;
the JSON stops being written only when they have agreed on a real capture. A
format migration that is also a trust migration is not a single step.

The schema exists to make the failures that happened IMPOSSIBLE rather than
unlikely:

  * `capture.coverage` is a tri-state per def type, so a type the dump never
    wrote reads `absent`, not `0`. That is the 824-def loss.
  * `def_tags` is a real table, so tag -> weapon -> cut-status is a join rather
    than an index rebuilt by hand. That is what weapon_tag_audit got wrong.
  * `provenance` carries the mod-set fingerprint, so an answer says which world
    it is an answer about.

Reading a 331 MB single-line JSON file: json.load would build the whole object
graph at once. Instead raw_decode walks the `defs` array one element at a time,
so peak memory is the file's text plus one def.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from measure.result import (Measured, Unmeasured, Refused, Report,
                            UnmeasuredError)  # noqa: E402

SCHEMA_VERSION = 3
DB_NAME = "defs.sqlite"

SCHEMA = """
PRAGMA journal_mode = OFF;
PRAGMA synchronous = OFF;

CREATE TABLE provenance (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE mods (
    load_order INTEGER,
    name       TEXT,
    package_id TEXT,
    root_dir   TEXT
);

-- One row per def type the dump CLAIMED to capture. coverage is the whole
-- point of this table: it is how absence stops being indistinguishable from
-- ignorance.
CREATE TABLE capture (
    -- 🔴 The IDENTITY of this capture slice, and it is NOT the simple name.
    -- It was, and that was the bug: `def_type TEXT PRIMARY KEY` cannot hold two
    -- types sharing a simple name, so a producer that had correctly separated
    -- `Verse.AbilityDef` from `VFECore.Abilities.AbilityDef` into two files had
    -- its work discarded on arrival — both rows collapsed and BOTH types' defs
    -- were deleted. Measured 2026-08-21 against a capture from the fixed
    -- producer: 630 AbilityDefs on disk, 0 in the table.
    -- It is the full name when the manifest's `defTypes` index resolves one,
    -- and the simple name otherwise (older captures, which cannot say).
    capture_key    TEXT PRIMARY KEY,
    def_type       TEXT NOT NULL,      -- simple type name, e.g. AbilityDef
    full_name      TEXT,               -- namespace-qualified, when known
    source_file    TEXT,               -- the defs/<x>.json it came from
    declared_count INTEGER,            -- what manifest.json said
    file_count     INTEGER,            -- what the file's own trailing "count" said
    loaded_count   INTEGER,            -- rows actually inserted here
    coverage       TEXT NOT NULL,      -- complete|partial|absent|failed|shadowed
    reason         TEXT
);

CREATE TABLE defs (
    id         INTEGER PRIMARY KEY,
    def_name   TEXT NOT NULL,
    -- 🔴 The type the dump ENUMERATED, i.e. what this slice is about. NOT the
    -- record's own reported class. A GeneDef whose concrete class is a subclass
    -- is still one of the 3845 GeneDefs, and storing the subclass here made
    -- `count GeneDef` (3845, from the slice) disagree with a COUNT(*) over this
    -- column (3825). One tool, two answers — found by stress test 2026-08-21.
    def_type   TEXT NOT NULL,
    -- The record's own reported class, when it differs. Real information, but a
    -- different question, so it gets its own column.
    concrete_type TEXT,
    full_name  TEXT,
    label      TEXT,
    mod_name   TEXT,
    package_id TEXT,
    short_hash INTEGER,
    json       TEXT NOT NULL
);

-- The engine's own COMPUTED classification (the `is` block). Not XML fields —
-- ThingDef.IsWeapon is C# logic, and an offline scan can only approximate it.
-- value is 'true' / 'false' / '<failed:Exception>' so a property that threw is
-- distinguishable from one that is false.
CREATE TABLE def_flags (
    def_id INTEGER NOT NULL REFERENCES defs(id),
    key    TEXT NOT NULL,
    value  TEXT
);

-- Tags as rows, so "which weapons carry this tag" is a join and "how many
-- survived the cut" cannot be answered by an index someone rebuilt by hand.
CREATE TABLE def_tags (
    def_id INTEGER NOT NULL REFERENCES defs(id),
    kind   TEXT NOT NULL,   -- weaponTags | tradeTags | apparelTags | thingCategories | techHediffsTags
    tag    TEXT NOT NULL
);

CREATE INDEX idx_capture_ty ON capture(def_type);
"""

#: 🔑 Created AFTER the bulk load, not with the tables. Every row inserted while
#: an index exists pays to maintain its B-tree; measured 2026-08-31 on the 96.5 MB
#: benchmark, the insert phase went 1.97s -> 1.21s and rebuilding all eight
#: indexes afterwards cost 0.54s, for ~18% off the total.
#:
#: ⚠️ `idx_capture_ty` is deliberately NOT in here. `capture` has one row per def
#: TYPE — hundreds, not hundreds of thousands — so it costs nothing to maintain,
#: and `build`'s own repair paths query it while the load is still running.
#:
#: ⚠️ What this trades away: the `shadowed` and `failed` branches below run
#: `DELETE FROM defs WHERE def_type = ?` DURING the load, and without
#: `idx_defs_type` that is a full table scan. It is a rare path — 13 collisions
#: on the real dump — and correctness there does not depend on speed, but a
#: capture that is mostly damaged will build slower than it used to.
INDEXES = """
CREATE INDEX idx_defs_name  ON defs(def_name);
CREATE INDEX idx_defs_type  ON defs(def_type);
CREATE INDEX idx_defs_conc  ON defs(concrete_type);
CREATE INDEX idx_defs_pkg   ON defs(package_id);
CREATE INDEX idx_flags      ON def_flags(key, value);
CREATE INDEX idx_flags_def  ON def_flags(def_id);
CREATE INDEX idx_tags       ON def_tags(kind, tag);
CREATE INDEX idx_tags_def   ON def_tags(def_id);
"""

#: Set before the first table exists, because `page_size` cannot be changed
#: afterwards without a VACUUM. Larger pages mean fewer overflow chains for the
#: `json` column, which is the biggest thing in the file.
#:
#: 🔴 THIS LIST IS SHORT BECAUSE IT WAS MEASURED, AND IT SHOULD STAY SHORT.
#: The first cut of it carried the four pragmas every "fast SQLite insert"
#: article recommends. Measured 2026-08-31 on the 96.5 MB benchmark, one at a
#: time, best of two runs:
#:
#:     none                  1.92s   48 MB peak
#:     page_size=8192        1.78s   49 MB      <- kept
#:     temp_store=MEMORY     1.94s   94 MB      <- slower AND +46 MB
#:     locking_mode=EXCL     1.93s   48 MB      <- no effect, real risk
#:     cache_size=-262144    1.78s  326 MB      <- 4% for 233 MB
#:
#: ⚠️ `cache_size` is the one to remember. 256 MB of page cache bought 4% of
#: build time and gave back, exactly, the memory the windowed reader had just
#: saved — a performance change that quietly cancelled the other performance
#: change, with both looking like wins in isolation. Advice tuned for inserting
#: 100M synthetic rows is not advice about this workload.
BUILD_PRAGMAS = """
PRAGMA page_size = 8192;
"""

#: Which list-valued fields become def_tags rows. Keyed by the field name as it
#: appears in the dump's `fields` object.
TAG_FIELDS = {
    "weaponTags": "weaponTags",
    "tradeTags": "tradeTags",
    "techHediffsTags": "techHediffsTags",
    "thingCategories": "thingCategories",
    "apparelTags": "apparelTags",
}

COVERAGE_COMPLETE = "complete"
COVERAGE_PARTIAL = "partial"
COVERAGE_ABSENT = "absent"
COVERAGE_FAILED = "failed"
COVERAGE_SHADOWED = "shadowed"
#: 🔴 A file in defs/ that THIS capture's manifest never declared — a leftover
#: from an earlier dump, not part of this load. `defs/` ACCUMULATES: the producer
#: writes a file per type that exists now and never deletes the file for a type
#: that has stopped existing. Measured 2026-08-21: all 19 such files were
#: 126–243 HOURS older than the manifest, while every declared file was written
#: within 17.8 seconds of it.
#: ⚠️ Their defs are NOT loaded, and this is the difference between a tool that
#: is careful and one that fails toward success — a dead defName in the index
#: makes a patch referencing a REMOVED def validate clean. The rule and its
#: measurement are `skills/rimworld-modding/scripts/validate_patch.py`'s, from
#: 2026-08-13; this module was wrong for an hour by ignoring it.
COVERAGE_ORPHAN = "orphan"
#: The name collided but every loser wrote ZERO defs, so nothing was lost. The
#: COUNT is right; only which TYPE owns it is unrecorded. Kept distinct from
#: `shadowed` on purpose — refusing to answer a question that has a correct
#: answer is the unearned refusal the analysis warned would get routed around.
COVERAGE_AMBIGUOUS = "ambiguous"


# --------------------------------------------------------------------------
# reading the manifest without losing what JSON throws away
# --------------------------------------------------------------------------

class DupDict(dict):
    """A JSON object that remembers the keys it was given more than once.

    🔴 **This class is the load-bearing part of the whole module, and it exists
    because of a measured failure.** `manifest.json` holds 532 `defCounts`
    entries under 517 distinct names — the dumper wrote a line per def TYPE, and
    13 simple names were claimed by more than one type. Plain `json.load` keeps
    the last value silently, so `defCounts["AbilityDef"]` reads 0 where 612 defs
    were written first and overwritten twice.

    That is why a "three independent sources agree" check does not save you
    here: the manifest count, the file's own trailing `count` and the parsed
    rows all say 0, and they agree **because they share the failure**. The only
    surviving evidence of the loss is the duplicate keys themselves, and it is
    destroyed at parse time by every reader that does not do this.
    """

    __slots__ = ("pairs",)


def _pairs_hook(pairs):
    d = DupDict(pairs)
    d.pairs = list(pairs)
    return d


def read_manifest(path):
    """Load manifest.json, preserving duplicate defCounts keys.

    Returns (manifest, declared_order) where declared_order maps a simple type
    name to the list of counts written under it, IN WRITE ORDER. A list longer
    than one is a collision, and everything but the last entry was lost.
    """
    with open(path, "r", encoding="utf-8") as fh:
        manifest = json.loads(fh.read(), object_pairs_hook=_pairs_hook)
    order = {}
    dc = manifest.get("defCounts")
    pairs = getattr(dc, "pairs", None)
    if pairs is None:
        pairs = list(dc.items()) if dc else []
    for k, v in pairs:
        order.setdefault(k, []).append(v)
    return manifest, order


def collision_report(declared_order):
    """(collided names -> counts, total defs lost). Offline, no game needed."""
    coll = {k: v for k, v in declared_order.items() if len(v) > 1}
    lost = sum(sum(v[:-1]) for v in coll.values())
    return coll, lost


# --------------------------------------------------------------------------
# streaming read
# --------------------------------------------------------------------------

#: How much of a defs file is held in memory at once, in CHARACTERS.
#:
#: 🔑 Measured 2026-08-31 (`bench/read_bench.py`, 96.5 MB synthetic file): the
#: old whole-file read peaked at **306 MB of RSS for a 96 MB file** — ~3.2x —
#: because at peak the raw bytes and the decoded `str` are both live, and one
#: non-ASCII character in one label widens the whole `str` to 2 bytes per
#: character. Extrapolated to the real 331 MB `defs/ThingDef.json` that is about
#: **1.05 GB of RAM to answer `count ThingDef`**.
#: A sliding window is the same speed, and the size of the window IS the memory:
#: 17 MB @ 16 KiB, **24 MB @ 256 KiB**, 219 MB @ 4 MiB. The jump at 4 MiB is
#: superlinear and unexplained, which is its own argument for staying small.
WINDOW = 1 << 18

_DEFS_ARRAY = re.compile(r'"defs"\s*:\s*\[')


def _file_count(path):
    """The `count` the file declares after its array closes, or None.

    Read from the last 200 BYTES rather than from the decoded text, so it costs
    the same whichever reader is walking the records. The tail is
    `],"count":123}` — pure ASCII in every dump this has seen — but it is decoded
    with `replace` because a multi-byte character cut in half by the 200-byte
    boundary must not raise; it must simply not match.
    """
    with open(path, "rb") as fh:
        try:
            fh.seek(-200, os.SEEK_END)
        except OSError:                      # a file shorter than 200 bytes
            fh.seek(0)
        tail = fh.read().decode("utf-8", "replace")
    ct = tail.rfind('"count":')
    if ct < 0:
        return None
    try:
        return int(tail[ct + 8:].strip().rstrip("}").strip().rstrip(","))
    except ValueError:
        return None


def _read_header(fh, path, window):
    """-> (header, the text already read that follows `"defs":[`).

    Shared by both readers on purpose. The header carries the only authoritative
    statement of what type a file holds, so the two readers must not be able to
    disagree about it — if this logic were duplicated, the differential case in
    the selftest would be comparing two copies of it rather than the walking it
    is meant to compare.
    """
    # utf-8-sig, not utf-8: a BOM makes the header parse fail, and the old
    # fallback then trusted the FILENAME over the file's own defType — which
    # this module states outright is not authoritative. A BOM'd file invented a
    # def type that never existed. Found by red team 2026-08-21.
    buf = fh.read(window)
    # Tolerate whitespace around the colon and bracket. The shipped dumper
    # writes compact JSON, but a hand-written or reformatted dump is still a
    # valid dump, and a reader that only accepts one spelling is brittle in a
    # way that reads as "the file is corrupt".
    #
    # ⚠️ GROW rather than assume the header fits the window. A pretty-printed
    # dump, or a window someone lowered to bound memory further, can push
    # `"defs":[` past the first chunk — and a reader that then said "has no defs
    # array" would report a healthy file as damaged.
    while True:
        mm = _DEFS_ARRAY.search(buf)
        if mm is not None:
            break
        more = fh.read(window)
        if not more:
            raise ValueError(f"{os.path.basename(path)} has no defs array")
        buf += more

    head_text = buf[: mm.start()].rstrip()
    if head_text.endswith(","):
        head_text = head_text[:-1]
    try:
        header = json.loads(head_text + "}")
    except json.JSONDecodeError as ex:
        # ⛔ Do NOT fall back to the filename. The inner defType is the
        # authority; if it cannot be read, the file is damaged and must say so.
        raise ValueError(
            f"{os.path.basename(path)}: header is unreadable ({ex}), so the "
            f"authoritative defType is unknown. The filename is NOT a "
            f"substitute for it.")
    return header, buf[mm.end():]


def _walk(buf, fh, window):
    """Yield (def object, its SOURCE SPAN) for each record in the array.

    ⭐ The span is the second value, and it is why this is more than a memory
    change. `raw_decode` already reports where the record ended, so the exact
    text the producer wrote is in hand — and `build` used to throw it away and
    call `json.dumps` on the parsed object to rebuild an equivalent string. That
    re-serialisation was **half the entire read cost** (1.11s -> 0.53s on the
    96.5 MB benchmark) and what it bought was a *canonicalised* record where what
    is wanted is the producer's own.

    `fh is None` means the whole array is already in `buf` — the reference
    reader. Otherwise the buffer is refilled on demand and never exceeds
    `window` plus one record.
    """
    dec = json.JSONDecoder()
    idx = 0
    while True:
        # Separators first, refilling if the buffer runs out mid-gap.
        while True:
            while idx < len(buf) and buf[idx] in " \t\r\n,":
                idx += 1
            if idx < len(buf):
                break
            if fh is None:
                return
            more = fh.read(window)
            if not more:
                return
            buf, idx = buf[idx:] + more, 0
        if buf[idx] == "]":
            return
        # ⚠️ A record may be LARGER than the window, and a record may be cut in
        # half by the window boundary. Both look identical to `raw_decode` — it
        # raises — and both are answered by reading more. Only when the file is
        # exhausted does the failure become real, and that is exactly the
        # truncated-file case `build` is required to RECORD rather than die on.
        while True:
            try:
                obj, end = dec.raw_decode(buf, idx)
                break
            except ValueError:
                if fh is None:
                    raise
                more = fh.read(window)
                if not more:
                    raise
                buf, idx = buf[idx:] + more, 0
        yield obj, buf[idx:end]
        idx = end


class _StringSource:
    """Makes an already-read `str` look like the text stream `_read_header`
    wants, so the reference reader shares that logic instead of copying it."""

    __slots__ = ("_text", "_at")

    def __init__(self, text):
        self._text, self._at = text, 0

    def read(self, n):
        out = self._text[self._at: self._at + n]
        self._at += len(out)
        return out


def iter_defs(path, window=WINDOW):
    """Yield each def from a defs/<Type>.json without loading the graph.

    Also returns the file's own header/trailer facts, which are what let us tell
    a shadowed file from an honest one: the `defType` INSIDE the file is
    authoritative, the filename is not.

    Returns (header, generator of (def_object, source_span)).

    `window=None` selects the **reference reader**, which reads the whole file
    into one `str` exactly as every version before 2026-08-31 did. It is kept,
    and must keep working, for the same reason the selftest keeps a naive
    manifest parse around: it is the independent second route the differential
    case compares against. ⛔ It is not the default and should not be used to
    answer anything — `WINDOW` records what it costs.
    """
    fh = open(path, "r", encoding="utf-8-sig")
    try:
        if window is None:
            text = fh.read()
            header, rest = _read_header(_StringSource(text), path, len(text) + 1)
            gen_fh, gen_window = None, 0
        else:
            header, rest = _read_header(fh, path, window)
            gen_fh, gen_window = fh, window
        header["fileCount"] = _file_count(path)
    except BaseException:
        fh.close()
        raise

    def gen():
        # 🔴 The handle is closed by the GENERATOR, not by a `with` around the
        # header parse. The old reader could use `with` because it had already
        # read the whole file before yielding anything; this one is still reading
        # while the caller iterates, and `build` abandons the generator on a
        # shadowed or damaged file. Without this `finally`, every abandoned file
        # leaked a descriptor until GC — 536 files per build, and on Windows an
        # open handle is also what makes the final `os.replace` fail.
        try:
            for pair in _walk(rest, gen_fh, gen_window):
                yield pair
        finally:
            fh.close()

    return header, gen()


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

@dataclass
class BuildStats:
    types_seen: int = 0
    defs_inserted: int = 0
    absent: int = 0
    shadowed: int = 0
    partial: int = 0
    failed: int = 0
    orphan: int = 0
    ambiguous: int = 0


def modlist_fingerprint(mods) -> str:
    """Stable hash of the mod SET — the thing an answer is an answer about.

    Order-independent on purpose: a load-order change alters which mod wins a
    def, but this fingerprint answers 'same mods?', and load order is carried
    separately in the mods table.
    """
    ids = sorted((m.get("packageId") or m.get("name") or "") for m in mods)
    h = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()
    return h[:16]


def build(dump_dir: str, db_path: str = None, only=None, progress=None,
          window=WINDOW) -> BuildStats:
    """Build defs.sqlite from a DefDump directory. Never touches the JSON.

    `window` is passed straight to `iter_defs`, so `window=None` builds via the
    reference reader. That exists for the differential case in the selftest and
    for nothing else: two builds of one capture, one per reader, must produce
    identical answers.
    """
    dump_dir = os.path.abspath(dump_dir)
    if db_path is None:
        db_path = os.path.join(dump_dir, DB_NAME)
    manifest_path = os.path.join(dump_dir, "manifest.json")
    manifest, declared_order = read_manifest(manifest_path)
    collided, defs_lost = collision_report(declared_order)

    defs_dir = os.path.join(dump_dir, "defs")
    # 🔴 Build to a TEMP file and rename at the end. The old code removed the
    # db and rebuilt in place, so for the ~60 s of a rebuild every reader in
    # every other window saw a missing file, then a partial one — observed as
    # `database is locked` and then `database disk image is malformed`. A
    # rename is atomic on the same filesystem, so a reader sees either the old
    # db or the new one and never a half-written one. It also means a crashed
    # build leaves the previous db intact instead of debris.
    final_path = db_path
    db_path = final_path + ".building"
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.executescript(BUILD_PRAGMAS)
    con.executescript(SCHEMA)

    mods = manifest.get("mods", [])
    fp = modlist_fingerprint(mods)
    con.executemany(
        "INSERT INTO mods VALUES (?,?,?,?)",
        [(m.get("loadOrder"), m.get("name"), m.get("packageId"), m.get("rootDir"))
         for m in mods],
    )

    prov = {
        "schema_version": str(SCHEMA_VERSION),
        "source_dump": dump_dir,
        "tool": manifest.get("tool", ""),
        "tool_version": manifest.get("toolVersion", ""),
        "mode": manifest.get("mode", ""),
        "captured_utc": manifest.get("capturedUtc", ""),
        "game_version": manifest.get("gameVersion", ""),
        "mod_count": str(manifest.get("modCount", len(mods))),
        "modlist_fingerprint": fp,
        "builder": "measure/dumpdb.py",
    }

    #: The LAST value written under each name — what plain json.load would have
    #: given. Kept only to reproduce the old reading; never trusted for a
    #: collided name.
    declared = {k: v[-1] for k, v in declared_order.items()}
    # A fixed producer writes defTypes[], mapping full name -> file, which says
    # WHICH type won each collided name. Older captures lack it, so everything
    # below still has to work without it — knowing only THAT a collision
    # happened, not who won.
    def_types = {d.get("name") or d.get("fullName"): d
                 for d in (manifest.get("defTypes") or [])}
    # 🔴 Keyed on FILE, because that is the only field of `defTypes` that is
    # UNIQUE. The map above is keyed on `name`, which for a collided name keeps
    # only the last of three — the same shape of loss this package exists to
    # stop, one level up. Keep both: `def_types` answers "is there an index at
    # all", `by_file` answers "which type is THIS file", which is the question
    # that resolves the collision.
    by_file = {d.get("file"): d for d in (manifest.get("defTypes") or [])
               if d.get("file")}
    prov["has_def_types_map"] = "1" if def_types else "0"
    prov["defcount_entries"] = str(sum(len(v) for v in declared_order.values()))
    prov["defcount_names"] = str(len(declared_order))
    prov["collided_names"] = str(len(collided))
    prov["defs_lost_to_collision"] = str(defs_lost)
    # A dump whose manifest carries no defTypes map cannot say WHICH full type
    # won each collided name — only that a collision happened. Recorded so an
    # answer can say how blind it is.
    prov["collision_blind"] = "0" if def_types else "1"

    stats = BuildStats()
    seen_types = {}          # def_type (from INSIDE the file) -> source file
    files = sorted(os.listdir(defs_dir)) if os.path.isdir(defs_dir) else []
    next_id = 1

    for fname in files:
        if not fname.endswith(".json"):
            continue
        stem = fname[:-5]
        if only and stem not in only:
            continue
        path = os.path.join(defs_dir, fname)
        try:
            header, it = iter_defs(path, window=window)
        except Exception as ex:                      # unreadable / truncated
            con.execute(
                "INSERT OR REPLACE INTO capture VALUES (?,?,?,?,?,?,?,?,?)",
                (stem, stem, None, fname, declared.get(stem), None, 0,
                 COVERAGE_FAILED, f"cannot read: {ex}"),
            )
            stats.failed += 1
            continue

        inner_type = header.get("defType") or stem
        full_name = header.get("defTypeFull") or None
        file_count = header.get("fileCount")
        # ⭐ WHICH type is this file? A fixed producer answers it outright, and
        # this is the whole point of the `defTypes` index: the loser of a simple
        # -name collision is written as `<FullName>.json` and the index maps
        # that file to its full type. When the index resolves it, the capture is
        # keyed on the FULL name and the two slices coexist. When there is no
        # index — every capture before 2026-08-21 — nothing has changed and the
        # simple name is still the only identity available.
        entry = by_file.get(fname)
        if entry:
            full_name = entry.get("fullName") or full_name
            inner_type = entry.get("name") or inner_type
        key = (full_name or inner_type) if entry else inner_type
        # ⚠️ The manifest's `defCounts` is keyed on the FILE STEM in a fixed
        # producer and on the simple name in an old one. Try the stem first —
        # for the 517 uncollided types they are the same string anyway.
        declared_here = declared.get(stem)
        if declared_here is None:
            declared_here = declared.get(inner_type)
        if key in seen_types:
            # Two FILES claiming the same IDENTITY. Refuse rather than merge:
            # nothing can say which file owns which records. Found by red team
            # 2026-08-21, when `defs` rows accumulated while `capture` was
            # overwritten — count said 3 where the table held 6.
            #
            # ⚠️ **`key` is the full name whenever the manifest resolves it**, so
            # a producer that separated `Verse.AbilityDef` from
            # `VFECore.Abilities.AbilityDef` no longer lands here at all. That
            # was the second half of the same bug: this branch fired on a
            # capture that HAD recorded which records belong to which, deleted
            # both slices, and reported them in the build total anyway — 630
            # AbilityDefs on disk, 0 in the table. Measured 2026-08-21.
            n_gone = con.execute(
                "SELECT COUNT(*) FROM defs WHERE def_type = ?",
                (inner_type,)).fetchone()[0]
            con.execute("DELETE FROM defs WHERE def_type = ?", (inner_type,))
            stats.defs_inserted -= n_gone     # never report rows we just removed
            con.execute(
                "INSERT OR REPLACE INTO capture VALUES (?,?,?,?,?,?,?,?,?)",
                (key, inner_type, full_name, f"{seen_types[key]}, {fname}",
                 declared_here, None, 0, COVERAGE_SHADOWED,
                 f"two files claim defType={inner_type} "
                 f"({seen_types[key]} and {fname}); the dump does not "
                 f"record which records belong to which, so neither is counted"))
            stats.shadowed += 1
            continue
        seen_types[key] = fname

        # 🔴 Decide BEFORE ingesting. An orphan's defs must never reach the
        # `defs` table — being able to answer about them is the bug.
        # ⚠️ `entry` means the manifest's defTypes index names this file, which
        # is a declaration by a better route than defCounts — so an indexed file
        # is never an orphan even when defCounts is keyed on the other string.
        if not entry and inner_type not in declared_order and stem not in declared_order:
            n = sum(1 for _ in it)          # counted, so the refusal can say how many
            cov, reason = _coverage(inner_type, stem, declared_order, file_count, n)
            con.execute(
                "INSERT OR REPLACE INTO capture VALUES (?,?,?,?,?,?,?,?,?)",
                (key, inner_type, full_name, fname, None, file_count, 0, cov, reason),
            )
            stats.types_seen += 1
            stats.orphan += 1
            continue

        rows, flagrows, tagrows = [], [], []
        n = 0
        try:
            for d, span in it:
                did = next_id
                next_id += 1
                n += 1
                fields = d.get("fields") or {}
                concrete = d.get("defType")
                rows.append((
                    did,
                    d.get("defName") or "",
                    inner_type,
                    concrete if concrete and concrete != inner_type else None,
                    d.get("defTypeFull") or full_name,
                    d.get("label"),
                    d.get("modName"),
                    d.get("packageId"),
                    d.get("shortHash"),
                    # ⭐ The producer's own bytes, not a re-serialisation of the
                    # parse. `json.dumps(d, …)` here was half the read cost, and
                    # it stored a canonicalised record — key order and float
                    # spelling as Python would write them — in a column whose
                    # whole job is to hand back what the dump actually said.
                    # ⚠️ Two dbs are therefore no longer byte-comparable. The
                    # invariant that replaces that is asserted in the selftest:
                    # `json.loads(span) == d` for every record.
                    span,
                ))
                isblock = d.get("is")
                if isinstance(isblock, dict):
                    for k, v in isblock.items():
                        flagrows.append((did, k, _flagval(v)))
                for field_name, kind in TAG_FIELDS.items():
                    vals = fields.get(field_name)
                    if isinstance(vals, list):
                        for v in vals:
                            tag = _tagval(v)
                            if tag:
                                tagrows.append((did, kind, tag))
                if len(rows) >= 5000:
                    _flush(con, rows, flagrows, tagrows)
                    rows, flagrows, tagrows = [], [], []
        except Exception as ex:
            # 🔴 Truncation raises HERE, during iteration, not at open() — so
            # wrapping only the open left COVERAGE_FAILED unreachable for the
            # commonest damage, and one bad file aborted the entire build.
            _flush(con, rows, flagrows, tagrows)
            con.execute(
                "INSERT OR REPLACE INTO capture VALUES (?,?,?,?,?,?,?,?,?)",
                (key, inner_type, full_name, fname, declared_here,
                 file_count, n, COVERAGE_FAILED,
                 f"read failed after {n} defs: {ex}"))
            con.execute("DELETE FROM defs WHERE def_type = ?", (inner_type,))
            stats.failed += 1
            stats.types_seen += 1
            seen_types[key] = fname
            continue
        _flush(con, rows, flagrows, tagrows)

        stats.types_seen += 1
        stats.defs_inserted += n
        cov, reason = _coverage(inner_type, stem, declared_order, file_count, n,
                                resolved=bool(entry), declared_here=declared_here)
        if cov == COVERAGE_PARTIAL:
            stats.partial += 1
        elif cov == COVERAGE_SHADOWED:
            stats.shadowed += 1
        elif cov == COVERAGE_AMBIGUOUS:
            stats.ambiguous += 1
        con.execute(
            "INSERT OR REPLACE INTO capture VALUES (?,?,?,?,?,?,?,?,?)",
            (key, inner_type, full_name, fname, declared_here,
             file_count, n, cov, reason),
        )
        if progress:
            progress(stem, n)

    # --- the 824-def case ------------------------------------------------
    # A type the manifest DECLARED but no file carries. With a producer that
    # keys on the simple name this is a collision: two types shared a name and
    # the loser was overwritten. It must read `absent`, never 0.
    already = {r[0] for r in con.execute("SELECT def_type FROM capture")}
    already |= {r[0] for r in con.execute("SELECT capture_key FROM capture")}
    # ⚠️ And every FILE STEM a resolved capture consumed. A fixed producer keys
    # defCounts on the stem, so `VFECore_Abilities_AbilityDef` appears there as
    # a name — with no file whose header carries it, since the header holds the
    # SIMPLE name. Without this it was swept in as a phantom `absent` type.
    already |= {os.path.splitext(f)[0] for f in seen_types.values()}
    for t, counts in declared_order.items():
        if t in seen_types or t in already:
            # A row already exists — typically `failed` from a damaged file.
            # Overwriting it with `absent` made stats.failed disagree with the
            # table, so the two told different stories about the same type.
            continue
        count = counts[-1]
        shadow = _shadowed_by(t, seen_types, def_types)
        if shadow:
            cov, reason = COVERAGE_SHADOWED, (
                f"declared {count} defs but no file carries defType={t}; "
                f"defs/{t}.json holds {shadow} instead — a filename collision "
                f"in the producer."
            )
            stats.shadowed += 1
        else:
            cov, reason = COVERAGE_ABSENT, (
                f"manifest declares {count} defs of this type and no file in "
                f"defs/ carries them"
            )
            stats.absent += 1
        con.execute(
            "INSERT OR REPLACE INTO capture VALUES (?,?,?,?,?,?,?,?,?)",
            (t, t, None, None, count, None, 0, cov, reason),
        )

    # 🔑 Indexes here — after every row is in, before provenance goes in. The
    # ordering is not cosmetic: provenance is the completion marker, so a build
    # that dies during index creation leaves a db with no provenance, which
    # `DumpDB` already refuses as debris. Creating them after provenance instead
    # would leave a db that answers, from unindexed tables, and calls itself
    # complete.
    con.executescript(INDEXES)

    # Written LAST on purpose: its presence is what marks the build complete,
    # and DumpDB refuses a db without it.
    prov["types_declared"] = str(len(declared))
    prov["types_captured"] = str(stats.types_seen)
    prov["defs_total"] = str(stats.defs_inserted)
    con.executemany("INSERT INTO provenance VALUES (?,?)", sorted(prov.items()))
    con.commit()
    con.execute("ANALYZE")
    con.commit()
    con.close()
    os.replace(db_path, final_path)      # atomic; readers never see a partial db
    return stats


def _flagval(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _tagval(v):
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return v.get("defName") or v.get("$ref") or None
    return None


def _flush(con, rows, flagrows, tagrows):
    if rows:
        con.executemany("INSERT INTO defs VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    if flagrows:
        con.executemany("INSERT INTO def_flags VALUES (?,?,?)", flagrows)
    if tagrows:
        con.executemany("INSERT INTO def_tags VALUES (?,?,?)", tagrows)


def _coverage(inner_type, stem, declared_order, file_count, loaded,
              resolved=False, declared_here=None):
    """Decide what this file's capture is worth.

    ⚠️ Three sources agreeing is NOT the test, and believing it was is how the
    first cut of this module returned `MEASURED 0 AbilityDef` with everything
    green. The manifest count, the file's trailing `count` and the parsed rows
    are not independent — a filename collision corrupts all three identically.
    The duplicate-key evidence is checked FIRST, before any agreement counts
    for anything.
    """
    if resolved:
        # ⭐ The manifest's `defTypes` index named which type owns this FILE, so
        # the simple-name collision is answered, not merely detected. Nothing
        # was lost and nothing is ambiguous — the only question left is whether
        # this file parsed completely, which is the ordinary check below.
        note = (f"simple name '{inner_type}' is shared, resolved by the "
                f"manifest's defTypes index to defs/{stem}.json")
        if file_count is not None and loaded != file_count:
            return COVERAGE_PARTIAL, (
                f"file declares {file_count} defs, {loaded} parsed; {note}")
        if declared_here is not None and loaded != declared_here:
            return COVERAGE_PARTIAL, (
                f"manifest declares {declared_here} defs, {loaded} parsed; {note}")
        return COVERAGE_COMPLETE, (note if stem != inner_type else None)

    written = declared_order.get(inner_type)
    if written is None:
        return COVERAGE_ORPHAN, (
            f"defs/{stem}.json exists but this capture's manifest never "
            f"declared it — a leftover from an earlier dump, since defs/ "
            f"accumulates and is never pruned. Its {loaded} defs are NOT "
            f"loaded: a dead defName in the index makes a patch referencing a "
            f"REMOVED def validate clean."
        )
    if len(written) > 1:
        lost = sum(written[:-1])
        if lost == 0:
            return COVERAGE_AMBIGUOUS, (
                f"the simple name '{inner_type}' was written {len(written)} "
                f"times, counts {written} in write order, but every earlier "
                f"writer held 0 defs — so NO def was lost and {loaded} is the "
                f"right count. What the dump no longer records is which of the "
                f"{len(written)} types these {loaded} defs belong to."
            )
        return COVERAGE_SHADOWED, (
            f"the simple name '{inner_type}' was written {len(written)} times "
            f"by different def types, counts {written} in write order; each "
            f"write overwrote defs/{stem}.json and the manifest entry, losing "
            f"{lost} defs. The {loaded} defs here belong to the LAST writer "
            f"only, and the dump no longer records which type that was."
        )
    dc = written[0]
    if inner_type != stem:
        # The file's own defType disagrees with its name. Under the old dumper
        # this file WON a collision; it is complete for what it holds, and the
        # loser is recorded separately as shadowed.
        note = f"written as defs/{stem}.json (simple-name collision)"
    else:
        note = None
    if file_count is not None and loaded != file_count:
        return COVERAGE_PARTIAL, (
            f"file declares {file_count} defs, {loaded} parsed"
            + (f"; {note}" if note else "")
        )
    if dc is not None and loaded != dc:
        return COVERAGE_PARTIAL, (
            f"manifest declares {dc} defs, {loaded} parsed"
            + (f"; {note}" if note else "")
        )
    return COVERAGE_COMPLETE, note


def _shadowed_by(missing_type, seen_types, def_types):
    """Which type is sitting in the file this one should have had."""
    for t, fname in seen_types.items():
        if fname == missing_type + ".json" and t != missing_type:
            return t
    return None


# --------------------------------------------------------------------------
# query — every public function returns a Measurement, never a bare number
# --------------------------------------------------------------------------

class DumpDB:
    def __init__(self, db_path: str, check_currency: bool = True):
        self.path = db_path
        if not os.path.exists(db_path):
            raise FileNotFoundError(db_path)
        self.con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.prov = dict(self.con.execute("SELECT key, value FROM provenance"))
        sv = self.prov.get("schema_version")
        if sv and int(sv) != SCHEMA_VERSION:
            # 🔑 This is why a DERIVED artifact must never be frozen: freezing
            # it freezes its BUGS. Schema 1 keyed rows on each record's concrete
            # subclass, so `count` and a COUNT(*) over the rows disagreed. A db
            # pinned at schema 1 would serve that disagreement forever against
            # an otherwise-correct capture. Rebuilding is cheap; being wrong is
            # not.
            raise ValueError(
                f"{db_path} was built by schema v{sv}; this code is v"
                f"{SCHEMA_VERSION}. Re-run `measure build` — the db is derived "
                f"from the capture and is meant to be rebuilt, never frozen.")
        if not self.prov.get("schema_version"):
            # `build` writes provenance LAST, so a db with none is the debris of
            # a crashed build. Left unchecked it reported "MEASURED 0 def types
            # complete", exit 0 — a capture that measured nothing claiming to be
            # whole. Found by red team 2026-08-21.
            raise ValueError(
                f"{db_path} has no provenance — it is the debris of an "
                f"interrupted build, not an empty capture. Re-run `measure "
                f"build`.")
        self.stale = self._staleness() if check_currency else None

    def _staleness(self):
        """Is this db still an answer about the dump that is on disk NOW?

        🔑 **Fingerprint, not timestamp.** An mtime says when a file was
        written, which is not the same question — a re-capture that happens to
        produce identical bytes is not stale, and a touched file is not fresh.
        The manifest's own `capturedUtc` and the mod-set fingerprint are what
        the answer is an answer ABOUT, so they are what gets compared.

        Returns a reason string if stale, else None. A db whose source dump has
        been deleted or moved is NOT stale — it is simply the only record left,
        and saying otherwise would make an archived dump unusable.
        """
        src = self.prov.get("source_dump")
        if not src:
            return None
        manifest_path = os.path.join(src, "manifest.json")
        if not os.path.exists(manifest_path):
            return None
        try:
            manifest, order = read_manifest(manifest_path)
        except Exception as ex:
            return f"the source manifest is unreadable ({ex})"
        cap = manifest.get("capturedUtc", "")
        if not cap:
            # Absent stamp is NOT a pass. Without it only the mod-set
            # fingerprint remains, and a re-capture on the same inputs has the
            # same fingerprint — so a changed dump would read current.
            return ("the source manifest carries no capturedUtc, so this db's "
                    "currency cannot be established")
        if cap != self.prov.get("captured_utc"):
            return (f"the dump on disk was re-captured at {cap}; this db is "
                    f"built from {self.prov.get('captured_utc')}")
        fp = modlist_fingerprint(manifest.get("mods", []))
        if fp != self.prov.get("modlist_fingerprint"):
            return (f"the dump on disk now has mod set {fp}; this db is built "
                    f"from {self.prov.get('modlist_fingerprint')}")
        return None

    def _guard(self, artifact):
        """Every public answer passes through here first."""
        if self.stale:
            return Unmeasured(
                reason=f"this defs.sqlite is stale — {self.stale}",
                artifact=artifact,
                instrument="dumpdb",
                remedy="measure build",
            )
        return None

    # ---- provenance -----------------------------------------------------
    @property
    def against(self) -> str:
        return (
            f"{os.path.basename(self.path)} "
            f"mods={self.prov.get('mod_count','?')}/"
            f"{self.prov.get('modlist_fingerprint','?')} "
            f"captured={self.prov.get('captured_utc','?')}"
        )

    def close(self):
        self.con.close()

    # ---- the question that started this ---------------------------------
    def count(self, def_type: str):
        """How many defs of this type. `0` here can only mean measured zero."""
        stale = self._guard(def_type)
        if stale:
            return stale
        rows = self.con.execute(
            "SELECT coverage, reason, declared_count, loaded_count, capture_key "
            "FROM capture WHERE def_type = ? OR capture_key = ?",
            (def_type, def_type)
        ).fetchall()
        # ⭐ A simple name can now name MORE THAN ONE slice, because a resolved
        # collision keeps both instead of discarding both. That is a better
        # problem than the one it replaces: the defs are all present, and the
        # question "how many AbilityDefs" simply has two honest answers.
        # ⛔ Do NOT sum them — they are different types that happen to share a
        # short name, and adding them invents a quantity nothing measured.
        if len(rows) > 1:
            parts = ", ".join(f"{r[4]} ({r[3]})" for r in rows)
            return Unmeasured(
                reason=f"'{def_type}' is the simple name of {len(rows)} distinct "
                       f"def types in this capture: {parts}. They are different "
                       f"types and their counts must not be added.",
                artifact=def_type,
                instrument="dumpdb.count",
                remedy="ask for the one you mean by its full name, e.g. "
                       + " or ".join(f"`measure count {r[4]}`" for r in rows[:2]),
            )
        row = rows[0] if rows else None
        if row is None:
            return Unmeasured(
                reason=f"no def type named {def_type} in this capture "
                       f"({self.prov.get('types_declared','?')} declared)",
                artifact=def_type,
                instrument="dumpdb.count",
                remedy="check the spelling with `measure types <substring>`; the dump only "
                       "holds types the running game had loaded",
            )
        coverage, reason, declared, loaded = row[0], row[1], row[2], row[3]
        if coverage == COVERAGE_AMBIGUOUS:
            return Measured(
                value=loaded, instrument="dumpdb.count", artifact=def_type,
                against=self.against,
                evidence="name shared with another def type that held 0 defs; "
                         "the count is right, the owning type is unrecorded",
            )

        if coverage == COVERAGE_ORPHAN:
            return Unmeasured(
                reason=f"coverage=orphan: {reason}",
                artifact=def_type,
                instrument="dumpdb.count",
                remedy="this type was NOT in the load — the mod providing it "
                       "was removed. If you expected it, the mod list is the "
                       "thing to check, not the dump",
            )
        if coverage in (COVERAGE_ABSENT, COVERAGE_SHADOWED, COVERAGE_FAILED):
            return Unmeasured(
                reason=f"coverage={coverage}: {reason}",
                artifact=def_type,
                instrument="dumpdb.count",
                remedy="re-capture with a producer that keys on the "
                       "fully-qualified type name, then `measure build`",
            )
        m = Measured(
            value=loaded,
            instrument="dumpdb.count",
            artifact=def_type,
            against=self.against,
        )
        if coverage == COVERAGE_PARTIAL:
            return Unmeasured(
                reason=f"coverage=partial: {reason}",
                artifact=def_type,
                instrument="dumpdb.count",
                remedy="the file is truncated or the manifest disagrees; "
                       "re-capture before trusting a number here",
            )
        return m

    def types(self, like: str = None):
        q = "SELECT def_type, coverage, loaded_count, declared_count FROM capture"
        args = ()
        if like:
            q += " WHERE def_type LIKE ?"
            args = (f"%{like}%",)
        q += " ORDER BY def_type"
        return list(self.con.execute(q, args))

    def coverage_summary(self):
        return dict(self.con.execute(
            "SELECT coverage, COUNT(*) FROM capture GROUP BY coverage"))

    def get(self, def_name: str):
        stale = self._guard(def_name)
        if stale:
            return stale
        rows = self.con.execute(
            "SELECT def_type, label, mod_name, package_id, short_hash "
            "FROM defs WHERE def_name = ?", (def_name,)
        ).fetchall()
        if not rows:
            return Unmeasured(
                reason="no def with this name in the capture",
                artifact=def_name,
                instrument="dumpdb.get",
                remedy="absence here is only as good as coverage — run "
                       "`coverage` first and check the type is complete",
            )
        return Measured(
            value=len(rows),
            instrument="dumpdb.get",
            artifact=def_name,
            against=self.against,
            evidence="; ".join(f"{r[0]} '{r[1]}' from {r[2]}" for r in rows[:3]),
        )

    def tag(self, tag: str, kind: str = "weaponTags"):
        """How many defs carry this tag — the join weapon_tag_audit lacked.

        A tag with no rows is genuinely ambiguous, and says so: Cherry Picker
        NEUTERS a cut def rather than deleting it, so a fully-cut tag is ABSENT
        from a dump-built index rather than empty in it. That made
        `emptied by the cut: 0` arithmetically guaranteed.
        """
        stale = self._guard(f"{kind}:{tag}")
        if stale:
            return stale
        n = self.con.execute(
            "SELECT COUNT(DISTINCT def_id) FROM def_tags WHERE kind=? AND tag=?",
            (kind, tag),
        ).fetchone()[0]
        if n == 0:
            known = self.con.execute(
                "SELECT COUNT(*) FROM def_tags WHERE kind=?", (kind,)
            ).fetchone()[0]
            if known == 0:
                return Unmeasured(
                    reason=f"no {kind} were captured at all, so a zero here "
                           f"says nothing about {tag}",
                    artifact=f"{kind}:{tag}",
                    instrument="dumpdb.tag",
                    remedy="re-capture; the dumper may not be emitting this field",
                )
            return Refused(
                reason=f"zero defs carry {tag}, but a cut def is NEUTERED and "
                       f"not deleted, so 'no rows' cannot distinguish "
                       f"'never existed' from 'cut to nothing'",
                artifact=f"{kind}:{tag}",
                instrument="dumpdb.tag",
                right_instrument="cross-check against the Cherry Picker key "
                                 "list before concluding the tag is dead",
            )
        return Measured(
            value=n, instrument="dumpdb.tag",
            artifact=f"{kind}:{tag}", against=self.against,
        )

    def flag(self, key: str, value: str = "true"):
        stale = self._guard(f"is.{key}={value}")
        if stale:
            return stale
        n = self.con.execute(
            "SELECT COUNT(DISTINCT def_id) FROM def_flags WHERE key=? AND value=?",
            (key, value),
        ).fetchone()[0]
        known = self.con.execute(
            "SELECT COUNT(*) FROM def_flags WHERE key=?", (key,)
        ).fetchone()[0]
        if known == 0:
            return Unmeasured(
                reason=f"the `is` block never carried {key} in this capture",
                artifact=f"is.{key}={value}",
                instrument="dumpdb.flag",
                remedy="only ThingDefs carry the `is` block; check the type",
            )
        return Measured(
            value=n, instrument="dumpdb.flag",
            artifact=f"is.{key}={value}", against=self.against,
            evidence=f"of {known} defs carrying the flag",
        )

    def record(self, def_name: str, def_type: str = None):
        """The full record for one name, as a Measurement carrying the dict.

        🔑 Without this, every tool that needs a record's FIELDS had to drop to
        `sql()` and hand-decode the json column — which puts it outside the
        typed guarantee and back to interpreting raw rows. That was the single
        biggest gap in this package: it could tell you how many, but not what.

        The value is the parsed record. Coverage still gates it: a record from
        a shadowed, orphan or partial slice is refused, because "here is the
        record" implies "and it is the whole story", which it would not be.
        """
        stale = self._guard(def_name)
        if stale:
            return stale
        q = ("SELECT def_type, json FROM defs WHERE def_name = ?"
             + (" AND def_type = ?" if def_type else ""))
        args = (def_name,) + ((def_type,) if def_type else ())
        rows = self.con.execute(q, args).fetchall()
        if not rows:
            return Unmeasured(
                reason="no record with this name in the capture",
                artifact=def_name, instrument="dumpdb.record",
                remedy="absence is only as good as coverage — run `coverage` "
                       "and check the type is complete before concluding it "
                       "does not exist")
        if len(rows) > 1 and not def_type:
            return Refused(
                reason=f"{len(rows)} records share this name across types "
                       f"({', '.join(sorted({r[0] for r in rows}))})",
                artifact=def_name, instrument="dumpdb.record",
                right_instrument="pass def_type= to disambiguate")
        dt, blob = rows[0]
        cov = self.con.execute(
            "SELECT coverage, reason FROM capture WHERE def_type = ?",
            (dt,)).fetchone()
        if cov and cov[0] not in (COVERAGE_COMPLETE, COVERAGE_AMBIGUOUS):
            return Unmeasured(
                reason=f"the {dt} slice is {cov[0]}: {cov[1]}",
                artifact=def_name, instrument="dumpdb.record",
                remedy="the record may exist, but this capture cannot vouch "
                       "for it; re-capture before relying on its fields")
        return Measured(value=json.loads(blob), instrument="dumpdb.record",
                        artifact=def_name, against=self.against,
                        evidence=f"a {dt} record")

    def records(self, def_type: str, limit: int = None):
        """Every record of a type, as a Measurement carrying a list.

        Refuses for exactly the reasons `count` refuses — iterating a slice the
        capture cannot vouch for is how a partial dump becomes a confident
        census.
        """
        n = self.count(def_type)
        if not n.ok:
            return n
        q = ("SELECT json FROM defs WHERE def_type = ? OR full_name = ?"
             if "." in def_type else "SELECT json FROM defs WHERE def_type = ?")
        args = (def_type,)
        if limit:
            q += " LIMIT ?"
            args += (limit,)
        out = [json.loads(r[0]) for r in self.con.execute(q, args)]
        return Measured(value=out, instrument="dumpdb.records",
                        artifact=def_type, against=self.against,
                        evidence=f"{len(out)} of {n.unwrap()} records")

    def sql(self, query: str, args=()):
        """Escape hatch. Returns raw rows — the caller owns the interpretation.

        🔴 It still honours the staleness guard, and it raises rather than
        answering from a stale artifact. The CLI must NEVER wrap what comes back
        here in a `Measured`: a raw row carries no coverage, so
        `SELECT loaded_count … WHERE def_type='AbilityDef'` returns 0 — the
        package's own canonical wrong number — while `count` on the same db in
        the same second refuses. Found by red team 2026-08-21.
        """
        if self.stale:
            raise UnmeasuredError(f"defs.sqlite is stale — {self.stale}")
        return list(self.con.execute(query, args))

    # ---- the trust migration -------------------------------------------
    def verify_against_json(self, dump_dir: str, limit_types=None) -> Report:
        """Re-read the JSON and check the db row for row.

        This is what makes writing both formats for one cycle mean something.
        """
        rep = Report()
        defs_dir = os.path.join(dump_dir, "defs")

        # 🔴 A FILE, NOT A SIMPLE NAME, IS WHAT THIS COMPARES.
        # Once the producer resolves a filename collision it writes `AbilityDef.json`
        # for `RimWorld.AbilityDef` AND `VEF.Abilities.AbilityDef.json` for the other
        # one. Both carry the simple name `AbilityDef`, so keying the comparison on
        # that name asked "does this ONE file hold every AbilityDef in the db" — and
        # of course it does not: 612 in one file, 18 in the other, 630 in the db.
        # ⚠️ It reported 22 disagreements on this machine's dump and every one was
        # the instrument's arithmetic, not a real json-vs-sqlite gap. Measured
        # 2026-08-22. The `capture` table already records which file each type came
        # from, so `source_file` is the join and `full_name` is what to count.
        by_file = {}
        for src, full, cov in self.con.execute(
                "SELECT source_file, full_name, coverage FROM capture "
                "WHERE source_file IS NOT NULL"):
            by_file[src] = (full, cov)

        for fname in sorted(os.listdir(defs_dir)):
            if not fname.endswith(".json"):
                continue
            stem = fname[:-5]
            if limit_types and stem not in limit_types:
                continue
            try:
                header, it = iter_defs(os.path.join(defs_dir, fname))
            except Exception as ex:
                rep.add(Unmeasured(reason=str(ex), artifact=stem,
                                   instrument="verify_against_json"))
                continue

            full, cov = by_file.get(fname, (None, None))
            # ⚠️ Older captures have no `source_file`, so fall back to the simple
            # name exactly as before. Those captures also predate the collision fix,
            # which is why the fallback is right rather than merely tolerant.
            inner = full or header.get("defType") or stem
            if cov is None:
                row = self.con.execute(
                    "SELECT coverage FROM capture WHERE def_type = ?", (inner,)
                ).fetchone()
                cov = row[0] if row else None
            n = sum(1 for _ in it)
            if cov == COVERAGE_ORPHAN:
                # Excluded deliberately, not lost. Reporting it as a
                # json-vs-sqlite disagreement would send the reader to
                # "rebuild the db", which cannot and must not change it.
                continue
            if full:
                db_n = self.con.execute(
                    "SELECT COUNT(*) FROM defs WHERE full_name = ?", (full,)
                ).fetchone()[0]
            else:
                db_n = self.con.execute(
                    "SELECT COUNT(*) FROM defs WHERE def_type = ?", (inner,)
                ).fetchone()[0]
            if n == db_n:
                rep.add(Measured(value=n, instrument="verify_against_json",
                                 artifact=inner, against=self.against))
            else:
                rep.add(Unmeasured(
                    reason=f"json has {n}, sqlite has {db_n}",
                    artifact=inner, instrument="verify_against_json",
                    remedy="rebuild the db; do NOT stop writing the json"))
        return rep


def default_dump_dir() -> str:
    """Where the live dump lands. The repo never holds it — it is 646 MB."""
    env = os.environ.get("RIMWORLD_DEFDUMP")
    if env:
        return env
    return ("/mnt/c/Users/Mandrake/AppData/LocalLow/Ludeon Studios/"
            "RimWorld by Ludeon Studios/DefDump")


#: A dated capture directory: the ISO-8601 instant it was taken, with ':' made
#: filesystem safe. `2026-08-21T22-44-59Z`.
_CAPTURE_ID = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")


def split_capture_layout(path: str):
    """-> (root, source) for a dump that may or may not use dated captures.

    🔴 A DUMP CAN HOLD MANY CAPTURES, AND THE DATABASE IS NOT ONE OF THEM.
    A producer that keeps history writes `<root>/captures/<id>/manifest.json`
    and prunes old ids. `defs.sqlite` is DERIVED from whichever capture is
    current, so it belongs at `<root>` — outside every capture — or pruning a
    capture would delete the database and rebuilding the database would look
    like a new capture.

    ⇒ Reading and writing want DIFFERENT directories, which is the whole reason
    this exists:
        root   where `defs.sqlite` lives
        source where `manifest.json` and `defs/` live — the newest capture

    A flat dump with no `captures/` returns the same path twice, so a caller
    written against this works on both layouts with no flag day.

    `path` may be the root or a capture; both are understood.
    """
    path = os.path.abspath(path)
    head, tail = os.path.split(path)
    if os.path.basename(head) == "captures" and _CAPTURE_ID.match(tail):
        return os.path.dirname(head), path          # handed a capture
    caps = os.path.join(path, "captures")
    try:
        ids = sorted(d for d in os.listdir(caps)
                     if _CAPTURE_ID.match(d) and os.path.isdir(os.path.join(caps, d)))
    except OSError:
        return path, path                           # flat layout, or no dump at all
    if not ids:
        return path, path
    # Fixed-width ISO-8601, so an ordinal sort IS chronological. No date parsing.
    return path, os.path.join(caps, ids[-1])


def open_default() -> DumpDB:
    return DumpDB(os.path.join(default_dump_dir(), DB_NAME))
