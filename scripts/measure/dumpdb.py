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

from measure.result import Measured, Unmeasured, Refused, Report  # noqa: E402

SCHEMA_VERSION = 1
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
    def_type       TEXT PRIMARY KEY,   -- simple type name, e.g. AbilityDef
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
    def_type   TEXT NOT NULL,
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

CREATE INDEX idx_defs_name  ON defs(def_name);
CREATE INDEX idx_defs_type  ON defs(def_type);
CREATE INDEX idx_defs_pkg   ON defs(package_id);
CREATE INDEX idx_flags      ON def_flags(key, value);
CREATE INDEX idx_flags_def  ON def_flags(def_id);
CREATE INDEX idx_tags       ON def_tags(kind, tag);
CREATE INDEX idx_tags_def   ON def_tags(def_id);
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
#: from an earlier dump, not part of this load. `defs/` ACCUMULATES: RimDefDump
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

def iter_defs(path):
    """Yield each def object from a defs/<Type>.json without loading the graph.

    Also yields the file's own header/trailer facts, which are what let us tell
    a shadowed file from an honest one: the `defType` INSIDE the file is
    authoritative, the filename is not.

    Returns (header, generator). header holds defType and count.
    """
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    dec = json.JSONDecoder()

    # The header keys precede "defs". Decode just enough to read defType,
    # rather than the whole object.
    # Tolerate whitespace around the colon and bracket. The shipped dumper
    # writes compact JSON, but a hand-written or reformatted dump is still a
    # valid dump, and a reader that only accepts one spelling is brittle in a
    # way that reads as "the file is corrupt".
    mm = re.search(r'"defs"\s*:\s*\[', text)
    if mm is None:
        raise ValueError(f"{os.path.basename(path)} has no defs array")
    at, end = mm.start(), mm.end()

    head_text = text[: at] .rstrip()
    if head_text.endswith(","):
        head_text = head_text[:-1]
    try:
        header = json.loads(head_text + "}")
    except json.JSONDecodeError:
        header = {}

    # The trailing "count" is after the array closes.
    tail = text[-200:]
    file_count = None
    ct = tail.rfind('"count":')
    if ct >= 0:
        try:
            file_count = int(tail[ct + 8:].strip().rstrip("}").strip().rstrip(","))
        except ValueError:
            file_count = None
    header["fileCount"] = file_count

    def gen():
        idx = end
        n = len(text)
        while True:
            while idx < n and text[idx] in " \t\r\n,":
                idx += 1
            if idx >= n or text[idx] == "]":
                return
            obj, idx = dec.raw_decode(text, idx)
            yield obj

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


def build(dump_dir: str, db_path: str = None, only=None, progress=None) -> BuildStats:
    """Build defs.sqlite from a DefDump directory. Never touches the JSON."""
    dump_dir = os.path.abspath(dump_dir)
    if db_path is None:
        db_path = os.path.join(dump_dir, DB_NAME)
    manifest_path = os.path.join(dump_dir, "manifest.json")
    manifest, declared_order = read_manifest(manifest_path)
    collided, defs_lost = collision_report(declared_order)

    defs_dir = os.path.join(dump_dir, "defs")
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
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
    # The post-d7cf154 dumper writes defTypes[], mapping full name -> file. The
    # dump on disk today predates it, so this is absent and everything below
    # still has to work without it.
    def_types = {d.get("name") or d.get("fullName"): d
                 for d in (manifest.get("defTypes") or [])}
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
            header, it = iter_defs(path)
        except Exception as ex:                      # unreadable / truncated
            con.execute(
                "INSERT OR REPLACE INTO capture VALUES (?,?,?,?,?,?,?,?)",
                (stem, None, fname, declared.get(stem), None, 0,
                 COVERAGE_FAILED, f"cannot read: {ex}"),
            )
            stats.failed += 1
            continue

        inner_type = header.get("defType") or stem
        full_name = header.get("defTypeFull") or None
        file_count = header.get("fileCount")
        seen_types[inner_type] = fname

        # 🔴 Decide BEFORE ingesting. An orphan's defs must never reach the
        # `defs` table — being able to answer about them is the bug.
        if inner_type not in declared_order:
            n = sum(1 for _ in it)          # counted, so the refusal can say how many
            cov, reason = _coverage(inner_type, stem, declared_order, file_count, n)
            con.execute(
                "INSERT OR REPLACE INTO capture VALUES (?,?,?,?,?,?,?,?)",
                (inner_type, full_name, fname, None, file_count, 0, cov, reason),
            )
            stats.types_seen += 1
            stats.orphan += 1
            continue

        rows, flagrows, tagrows = [], [], []
        n = 0
        for d in it:
            did = next_id
            next_id += 1
            n += 1
            fields = d.get("fields") or {}
            rows.append((
                did,
                d.get("defName") or "",
                d.get("defType") or inner_type,
                d.get("defTypeFull") or full_name,
                d.get("label"),
                d.get("modName"),
                d.get("packageId"),
                d.get("shortHash"),
                json.dumps(d, separators=(",", ":"), ensure_ascii=False),
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
        _flush(con, rows, flagrows, tagrows)

        stats.types_seen += 1
        stats.defs_inserted += n
        cov, reason = _coverage(inner_type, stem, declared_order, file_count, n)
        if cov == COVERAGE_PARTIAL:
            stats.partial += 1
        elif cov == COVERAGE_SHADOWED:
            stats.shadowed += 1
        elif cov == COVERAGE_AMBIGUOUS:
            stats.ambiguous += 1
        con.execute(
            "INSERT OR REPLACE INTO capture VALUES (?,?,?,?,?,?,?,?)",
            (inner_type, full_name, fname, declared.get(inner_type),
             file_count, n, cov, reason),
        )
        if progress:
            progress(stem, n)

    # --- the 824-def case ------------------------------------------------
    # A type the manifest DECLARED but no file carries. Under the pre-d7cf154
    # dumper this is a filename collision: two types shared a simple name and
    # the loser was overwritten. It must read `absent`, never 0.
    for t, counts in declared_order.items():
        if t in seen_types:
            continue
        count = counts[-1]
        shadow = _shadowed_by(t, seen_types, def_types)
        if shadow:
            cov, reason = COVERAGE_SHADOWED, (
                f"declared {count} defs but no file carries defType={t}; "
                f"defs/{t}.json holds {shadow} instead — a filename collision "
                f"in the dumper. Fixed in d7cf154; that DLL is undeployed."
            )
            stats.shadowed += 1
        else:
            cov, reason = COVERAGE_ABSENT, (
                f"manifest declares {count} defs of this type and no file in "
                f"defs/ carries them"
            )
            stats.absent += 1
        con.execute(
            "INSERT OR REPLACE INTO capture VALUES (?,?,?,?,?,?,?,?)",
            (t, None, None, count, None, 0, cov, reason),
        )

    prov["types_declared"] = str(len(declared))
    prov["types_captured"] = str(stats.types_seen)
    prov["defs_total"] = str(stats.defs_inserted)
    con.executemany("INSERT INTO provenance VALUES (?,?)", sorted(prov.items()))
    con.commit()
    con.execute("ANALYZE")
    con.commit()
    con.close()
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
        con.executemany("INSERT INTO defs VALUES (?,?,?,?,?,?,?,?,?)", rows)
    if flagrows:
        con.executemany("INSERT INTO def_flags VALUES (?,?,?)", flagrows)
    if tagrows:
        con.executemany("INSERT INTO def_tags VALUES (?,?,?)", tagrows)


def _coverage(inner_type, stem, declared_order, file_count, loaded):
    """Decide what this file's capture is worth.

    ⚠️ Three sources agreeing is NOT the test, and believing it was is how the
    first cut of this module returned `MEASURED 0 AbilityDef` with everything
    green. The manifest count, the file's trailing `count` and the parsed rows
    are not independent — a filename collision corrupts all three identically.
    The duplicate-key evidence is checked FIRST, before any agreement counts
    for anything.
    """
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
            f"only, and the dump no longer records which type that was. "
            f"Fixed in d7cf154; that DLL is undeployed."
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
        if cap and cap != self.prov.get("captured_utc"):
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
                remedy="python3 src/RimMandrake/measure/cli.py build",
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
        row = self.con.execute(
            "SELECT coverage, reason, declared_count, loaded_count "
            "FROM capture WHERE def_type = ?", (def_type,)
        ).fetchone()
        if row is None:
            return Unmeasured(
                reason=f"no def type named {def_type} in this capture "
                       f"({self.prov.get('types_declared','?')} declared)",
                artifact=def_type,
                instrument="dumpdb.count",
                remedy="check the spelling with `types --like`; the dump only "
                       "holds types the running game had loaded",
            )
        coverage, reason, declared, loaded = row
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
                remedy="re-capture with a RimDefDump that includes d7cf154, "
                       "then rebuild the db",
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

    def sql(self, query: str, args=()):
        """Escape hatch. Returns rows, not a Measurement — the caller owns the
        interpretation, which is exactly the risk this module otherwise removes.
        """
        return list(self.con.execute(query, args))

    # ---- the trust migration -------------------------------------------
    def verify_against_json(self, dump_dir: str, limit_types=None) -> Report:
        """Re-read the JSON and check the db row for row.

        This is what makes writing both formats for one cycle mean something.
        """
        rep = Report()
        defs_dir = os.path.join(dump_dir, "defs")
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
            inner = header.get("defType") or stem
            n = sum(1 for _ in it)
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


def open_default() -> DumpDB:
    return DumpDB(os.path.join(default_dump_dir(), DB_NAME))
