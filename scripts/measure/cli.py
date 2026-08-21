"""`measure` — one question, one line.

    python3 src/RimMandrake/measure/cli.py count AbilityDef
    python3 src/RimMandrake/measure/cli.py coverage
    python3 src/RimMandrake/measure/cli.py tag Gun --kind weaponTags
    python3 src/RimMandrake/measure/cli.py get Gun_Revolver
    python3 src/RimMandrake/measure/cli.py csv world/ASHKARR_WORLDMAP_tiles.csv --where biome=Desert

Design constraint, from the analysis: **the correct path must be cheaper than
grep or it will be routed around.** So every subcommand prints ONE line by
default, and nothing prints a record unless asked. `--rows N` opts into more.

Exit status carries the tri-state for scripts:
    0  measured
    2  unmeasured  (the artifact does not hold the evidence)
    3  refused     (this instrument cannot judge the question)
"""

from __future__ import annotations

import argparse
import csv as _csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from measure.result import Measured, Unmeasured, Refused  # noqa: E402
from measure import artifacts  # noqa: E402
from measure.dumpdb import (  # noqa: E402
    DumpDB, DB_NAME, build, default_dump_dir,
)

EXIT = {"MEASURED": 0, "UNMEASURED": 2, "REFUSED": 3}


def emit(m) -> int:
    print(m.line())
    return EXIT[m.line().split(" ", 1)[0]]


def _db(args) -> DumpDB:
    path = args.db or os.path.join(args.dump or default_dump_dir(), DB_NAME)
    if not os.path.exists(path):
        print(Unmeasured(
            reason=f"no {DB_NAME} at {path}",
            artifact="dumpdb",
            remedy="python3 src/RimMandrake/measure/cli.py build",
        ).line())
        raise SystemExit(2)
    return DumpDB(path)


# --------------------------------------------------------------------------

def cmd_build(args) -> int:
    dump = args.dump or default_dump_dir()
    stats = build(dump, db_path=args.db, only=set(args.only) if args.only else None)
    print(
        f"MEASURED {stats.defs_inserted} defs built from {dump} "
        f"({stats.types_seen} types; absent={stats.absent} "
        f"shadowed={stats.shadowed} ambiguous={stats.ambiguous} "
        f"orphan={stats.orphan} partial={stats.partial} "
        f"failed={stats.failed}) via dumpdb.build"
    )
    return 0


def cmd_count(args) -> int:
    db = _db(args)
    return emit(db.count(args.def_type))


def cmd_types(args) -> int:
    db = _db(args)
    rows = db.types(args.like)
    if not rows:
        return emit(Unmeasured(reason="no type matches", artifact=args.like or "*",
                               instrument="dumpdb.types"))
    if args.rows:
        for t, cov, loaded, declared in rows[: args.rows]:
            print(f"{t:44s} {cov:9s} {loaded if loaded is not None else '-':>7} "
                  f"declared={declared if declared is not None else '-'}")
        if len(rows) > args.rows:
            print(f"... {len(rows) - args.rows} more (use --rows)")
        return 0
    return emit(Measured(
        value=len(rows), instrument="dumpdb.types",
        artifact=f"types matching {args.like}" if args.like else "def types",
        against=db.against,
        evidence="add --rows N to list them",
    ))


def cmd_coverage(args) -> int:
    db = _db(args)
    summary = db.coverage_summary()
    complete = summary.get("complete", 0)
    total = sum(summary.values())
    bad = total - complete
    detail = " ".join(f"{k}={v}" for k, v in sorted(summary.items()))
    if args.rows:
        rows = db.sql(
            "SELECT def_type, coverage, declared_count, reason FROM capture "
            "WHERE coverage <> 'complete' ORDER BY declared_count DESC")
        for t, cov, dc, reason in rows[: args.rows]:
            print(f"{t:44s} {cov:9s} declared={dc} — {reason}")
        if len(rows) > args.rows:
            print(f"... {len(rows) - args.rows} more")
        return 0
    if bad:
        # ⚠️ Say what each state COSTS, not just that it is not `complete`.
        # Only `ambiguous` still answers correctly; `shadowed`, `absent`,
        # `orphan`, `partial` and `failed` all refuse. A remedy line that
        # claims otherwise is itself a confident wrong answer.
        refusing = sum(v for k, v in summary.items()
                       if k in ("shadowed", "absent", "orphan", "partial",
                                "failed"))
        return emit(Unmeasured(
            reason=f"{refusing} of {total} def types cannot be counted at all "
                   f"and {bad - refusing} more answer without a cross-check "
                   f"({detail})",
            artifact="dump coverage",
            instrument="dumpdb.coverage",
            remedy="`coverage --rows 20` names them with the reason; a count "
                   "for a shadowed/absent type is UNMEASURED, never zero",
        ))
    return emit(Measured(value=total, instrument="dumpdb.coverage",
                         artifact="def types complete", against=db.against))


def cmd_get(args) -> int:
    db = _db(args)
    return emit(db.get(args.def_name))


def cmd_tag(args) -> int:
    db = _db(args)
    return emit(db.tag(args.tag, kind=args.kind))


def cmd_flag(args) -> int:
    db = _db(args)
    return emit(db.flag(args.key, value=args.value))


def cmd_verify(args) -> int:
    db = _db(args)
    dump = args.dump or default_dump_dir()
    rep = db.verify_against_json(dump, limit_types=set(args.only) if args.only else None)
    bad = rep.unmeasured
    if args.rows:
        print(rep.text())
    if bad:
        return emit(Unmeasured(
            reason=f"{len(bad)} of {len(rep.rows)} types disagree between "
                   f"json and sqlite (first: {bad[0].artifact})",
            artifact="json-vs-sqlite",
            instrument="dumpdb.verify_against_json",
            remedy="rebuild the db, and do NOT stop writing the json",
        ))
    return emit(Measured(
        value=len(rep.rows), instrument="dumpdb.verify_against_json",
        artifact="types agreeing json-vs-sqlite", against=db.against))


def cmd_sql(args) -> int:
    db = _db(args)
    q = args.query.strip()
    if not q.lower().startswith("select"):
        return emit(Refused(
            reason="only SELECT is allowed; the db is opened read-only",
            artifact="sql", instrument="dumpdb.sql",
            right_instrument="rebuild with `build` if the data is wrong"))
    rows = db.sql(q)
    if args.rows:
        for r in rows[: args.rows]:
            print("\t".join("" if c is None else str(c) for c in r))
        if len(rows) > args.rows:
            print(f"... {len(rows) - args.rows} more")
        return 0
    if len(rows) == 1 and len(rows[0]) == 1:
        return emit(Measured(value=rows[0][0], instrument="dumpdb.sql",
                             artifact=q[:60], against=db.against))
    return emit(Measured(value=len(rows), instrument="dumpdb.sql",
                         artifact=f"rows from {q[:50]}", against=db.against,
                         evidence="add --rows N to see them"))


def cmd_csv(args) -> int:
    """Counting rows in a world CSV without grep counting the header too."""
    path = args.path
    if not os.path.exists(path):
        return emit(Unmeasured(reason="no such file", artifact=path,
                               instrument="measure.csv"))
    col = val = None
    if args.where:
        if "=" not in args.where:
            return emit(Refused(reason="--where wants col=value", artifact=path,
                                instrument="measure.csv"))
        col, val = args.where.split("=", 1)
    with open(path, newline="", encoding="utf-8") as fh:
        rd = _csv.DictReader(fh)
        if col and col not in (rd.fieldnames or []):
            return emit(Unmeasured(
                reason=f"no column named {col}; columns are "
                       f"{', '.join(rd.fieldnames or [])}",
                artifact=path, instrument="measure.csv",
                remedy="name a real column"))
        n = 0
        total = 0
        for row in rd:
            total += 1
            if col is None or row.get(col) == val:
                n += 1
    return emit(Measured(
        value=n, instrument="measure.csv", artifact=f"{os.path.basename(path)}"
        + (f" {col}={val}" if col else " rows"),
        against=f"{total} data rows, header excluded"))


def cmd_explain(args) -> int:
    """What IS this artifact, and what may speak about it."""
    art = artifacts.classify(args.path)
    if art is None:
        big = artifacts.is_big(args.path)
        return emit(Measured(
            value="unregistered", instrument="artifacts.classify",
            artifact=args.path,
            evidence="large — consider registering it" if big else "read it whole"))
    print(f"{args.path} is a {art.kind}")
    print(f"  encoding : {art.encoding}")
    print(f"  use      : {art.instrument}")
    print(f"  literal grep ok: {'yes' if art.literal_scan_ok else 'no'}")
    if art.incident:
        print(f"  incident : {art.incident}")
    return 0


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="measure",
        description="One question about a large artifact, one line back.")
    ap.add_argument("--dump", help="DefDump directory (default: the live one)")
    ap.add_argument("--db", help=f"path to {DB_NAME}")
    ap.add_argument("--rows", type=int, default=0,
                    help="print up to N records instead of one summary line")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="build defs.sqlite from a DefDump")
    p.add_argument("--only", nargs="*", help="only these def types")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("count", help="how many defs of a type")
    p.add_argument("def_type")
    p.set_defaults(fn=cmd_count)

    p = sub.add_parser("types", help="which def types exist")
    p.add_argument("like", nargs="?")
    p.set_defaults(fn=cmd_types)

    p = sub.add_parser("coverage", help="which types are NOT fully captured")
    p.set_defaults(fn=cmd_coverage)

    p = sub.add_parser("get", help="does this defName exist, and as what")
    p.add_argument("def_name")
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("tag", help="how many defs carry a tag")
    p.add_argument("tag")
    p.add_argument("--kind", default="weaponTags")
    p.set_defaults(fn=cmd_tag)

    p = sub.add_parser("flag", help="how many defs the ENGINE classifies this way")
    p.add_argument("key")
    p.add_argument("--value", default="true")
    p.set_defaults(fn=cmd_flag)

    p = sub.add_parser("verify", help="check sqlite against the json, type by type")
    p.add_argument("--only", nargs="*")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("sql", help="read-only SELECT escape hatch")
    p.add_argument("query")
    p.set_defaults(fn=cmd_sql)

    p = sub.add_parser("csv", help="count rows in a CSV without counting the header")
    p.add_argument("path")
    p.add_argument("--where", help="col=value")
    p.set_defaults(fn=cmd_csv)

    p = sub.add_parser("explain", help="what is this artifact and what may read it")
    p.add_argument("path")
    p.set_defaults(fn=cmd_explain)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
