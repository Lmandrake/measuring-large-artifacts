"""Where does `measure build` spend its SQLite time, and how big is the result.

Variants, all inserting IDENTICAL rows:
  now        the shipped schema: 8 indexes created BEFORE the bulk load
  after      same indexes, created AFTER the load
  after+prag after + cache_size/temp_store/page_size/locking_mode
  zlib       after+prag, with the `json` column zlib-compressed
  nojson     after+prag, with no json column at all (offsets only)

Prints build seconds, db bytes, and a query-side probe so a "faster" variant
that made reads slower cannot hide.
"""
import json, os, sqlite3, sys, time, zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import read_bench as rb  # noqa: E402  (sys.argv guard makes this safe)

SRC = sys.argv[1] if len(sys.argv) > 1 else "big.json"

TABLES = """
CREATE TABLE defs (
    id INTEGER PRIMARY KEY, def_name TEXT NOT NULL, def_type TEXT NOT NULL,
    concrete_type TEXT, full_name TEXT, label TEXT, mod_name TEXT,
    package_id TEXT, short_hash INTEGER, json %s NOT NULL);
CREATE TABLE def_flags (def_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT);
CREATE TABLE def_tags (def_id INTEGER NOT NULL, kind TEXT NOT NULL, tag TEXT NOT NULL);
"""

TABLES_NOJSON = """
CREATE TABLE defs (
    id INTEGER PRIMARY KEY, def_name TEXT NOT NULL, def_type TEXT NOT NULL,
    concrete_type TEXT, full_name TEXT, label TEXT, mod_name TEXT,
    package_id TEXT, short_hash INTEGER,
    src_file TEXT NOT NULL, src_off INTEGER NOT NULL, src_len INTEGER NOT NULL);
CREATE TABLE def_flags (def_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT);
CREATE TABLE def_tags (def_id INTEGER NOT NULL, kind TEXT NOT NULL, tag TEXT NOT NULL);
"""

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

TAG_FIELDS = ("weaponTags", "tradeTags", "techHediffsTags", "thingCategories",
              "apparelTags")


def build(variant, path, dbp):
    if os.path.exists(dbp):
        os.remove(dbp)
    con = sqlite3.connect(dbp)
    con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
    if "prag" in variant or variant in ("zlib", "nojson"):
        con.executescript("PRAGMA page_size=8192; PRAGMA cache_size=-262144;"
                          "PRAGMA temp_store=MEMORY; PRAGMA locking_mode=EXCLUSIVE;")
    con.executescript(TABLES_NOJSON if variant == "nojson"
                      else TABLES % ("BLOB" if variant == "zlib" else "TEXT"))
    if variant == "now":
        con.executescript(INDEXES)

    t0 = time.perf_counter()
    rows, flags, tags = [], [], []
    did = 0
    # 🔴 A source offset is in BYTES; `len(blob)` is in CHARACTERS. The first cut
    # of this used the character length and produced `Unterminated string` on the
    # 200th record of a file with any non-ASCII in it — i.e. an offset index
    # silently addresses the WRONG BYTES the moment the dump stops being ASCII.
    with open(path, "rb") as _fh:
        _head = _fh.read(4096)
    off = _head.index(b'"defs":[') + len(b'"defs":[')
    for obj, blob in rb.v_chunked(path, chunk=1 << 18):
        did += 1
        f = obj.get("fields") or {}
        if variant == "nojson":
            # ⚠️ BOTH the offset and the length must be in bytes. Storing the
            # character length was the second half of the same bug.
            nbytes = len(blob.encode("utf-8"))
            rows.append((did, obj["defName"], "ThingDef", None, None,
                         obj.get("label"), obj.get("modName"),
                         obj.get("packageId"), obj.get("shortHash"),
                         os.path.basename(path), off, nbytes))
            off += nbytes + 1
        else:
            payload = (zlib.compress(blob.encode("utf-8"), 1) if variant == "zlib"
                       else blob)
            rows.append((did, obj["defName"], "ThingDef", None, None,
                         obj.get("label"), obj.get("modName"),
                         obj.get("packageId"), obj.get("shortHash"), payload))
        for k, v in (obj.get("is") or {}).items():
            flags.append((did, k, "true" if v else "false"))
        for fld in TAG_FIELDS:
            for v in f.get(fld) or ():
                if isinstance(v, str):
                    tags.append((did, fld, v))
        if len(rows) >= 5000:
            _flush(con, rows, flags, tags, variant)
            rows, flags, tags = [], [], []
    _flush(con, rows, flags, tags, variant)
    ins = time.perf_counter() - t0

    t1 = time.perf_counter()
    if variant != "now":
        con.executescript(INDEXES)
    idx = time.perf_counter() - t1
    con.commit()
    con.execute("ANALYZE")
    con.commit()
    con.close()
    return ins, idx, did


def _flush(con, rows, flags, tags, variant):
    if rows:
        n = 12 if variant == "nojson" else 10
        con.executemany("INSERT INTO defs VALUES (%s)" % ",".join("?" * n), rows)
    if flags:
        con.executemany("INSERT INTO def_flags VALUES (?,?,?)", flags)
    if tags:
        con.executemany("INSERT INTO def_tags VALUES (?,?,?)", tags)


def probe(variant, dbp, path):
    """Query side, so a build win that cost a read is visible."""
    con = sqlite3.connect("file:%s?mode=ro" % dbp, uri=True)
    t = time.perf_counter()
    con.execute("SELECT COUNT(*) FROM defs WHERE def_type='ThingDef'").fetchone()
    con.execute("SELECT COUNT(DISTINCT def_id) FROM def_tags "
                "WHERE kind='weaponTags' AND tag='Gun'").fetchone()
    cnt = time.perf_counter() - t

    names = [r[0] for r in con.execute(
        "SELECT def_name FROM defs ORDER BY id LIMIT 200")]
    t = time.perf_counter()
    for nm in names:
        if variant == "nojson":
            r = con.execute("SELECT src_off, src_len FROM defs WHERE def_name=?",
                            (nm,)).fetchone()
            with open(path, "rb") as fh:
                fh.seek(r[0]); json.loads(fh.read(r[1]))
        else:
            r = con.execute("SELECT json FROM defs WHERE def_name=?", (nm,)).fetchone()
            blob = r[0]
            json.loads(zlib.decompress(blob) if variant == "zlib" else blob)
    rec = time.perf_counter() - t
    con.close()
    return cnt, rec / len(names) * 1000


print("source: %s (%.1f MB)" % (SRC, os.path.getsize(SRC) / 1048576))
print("%-11s %7s %7s %7s %9s %9s %9s" %
      ("variant", "insert", "index", "total", "db MB", "count ms", "rec ms"))
for variant in ("now", "after", "after+prag", "zlib", "nojson"):
    dbp = "/tmp/mlabench/%s.sqlite" % variant.replace("+", "_")
    ins, idx, n = build(variant, SRC, dbp)
    cnt, rec = probe(variant, dbp, SRC)
    print("%-11s %6.2fs %6.2fs %6.2fs %8.1f %8.2f %8.3f"
          % (variant, ins, idx, ins + idx, os.path.getsize(dbp) / 1048576,
             cnt * 1000, rec))
