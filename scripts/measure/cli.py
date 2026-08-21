"""`measure` — one question, one line.

    measure count AbilityDef
    measure coverage
    measure tag Gun --kind weaponTags
    measure get Gun_Revolver
    measure csv world/ASHKARR_WORLDMAP_tiles.csv --where biome=Desert

Design constraint, from the analysis: **the correct path must be cheaper than
grep or it will be routed around.** So every subcommand prints ONE line by
default, and nothing prints a record unless asked. `--rows N` opts into more.

Exit status carries the tri-state for scripts:
    0  measured
    2  unmeasured  (the artifact does not hold the evidence)
    3  refused     (this instrument cannot judge the question)
   64  usage error (your command was malformed — NOT a measurement)

⚠️ 64 rather than argparse's default 2 is deliberate: a typo must never be
readable as "unmeasured", or a caller branching on exit status treats its own
bug as a finding.
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

#: 🔴 A malformed command must NOT exit 2. argparse's default is 2, which is
#: this tool's code for UNMEASURED — so a typo would be indistinguishable from
#: "the artifact does not carry the evidence", and a shell caller branching on
#: exit status would silently treat its own bug as a real measurement. Found by
#: a cold reader following references/api.md, 2026-08-21.
EXIT_USAGE = 64          # sysexits.h EX_USAGE


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(EXIT_USAGE)


def emit(m) -> int:
    print(m.line())
    return EXIT[m.line().split(" ", 1)[0]]


def _db(args) -> DumpDB:
    path = args.db or os.path.join(args.dump or default_dump_dir(), DB_NAME)
    if not os.path.exists(path):
        print(Unmeasured(
            reason=f"no {DB_NAME} at {path}",
            artifact="dumpdb",
            remedy="measure build",
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

    def _detail():
        """🔑 Print detail, but NEVER instead of the verdict.

        `--rows` asks for more output; it does not change the answer. An earlier
        cut returned 0 here, so `coverage --rows 20` exited success on the same
        question that `coverage` reported UNMEASURED — the detail flag silently
        flipped the finding. Found by a cold reader, 2026-08-21.
        """
        if not args.rows:
            return
        rows = db.sql(
            "SELECT def_type, coverage, declared_count, reason FROM capture "
            "WHERE coverage <> 'complete' ORDER BY declared_count DESC")
        for t, cov, dc, reason in rows[: args.rows]:
            print(f"{t:44s} {cov:9s} declared={dc} — {reason}")
        if len(rows) > args.rows:
            print(f"... {len(rows) - args.rows} more")

    if bad:
        # ⚠️ Say what each state COSTS, not just that it is not `complete`.
        # Only `ambiguous` still answers correctly; `shadowed`, `absent`,
        # `orphan`, `partial` and `failed` all refuse. A remedy line that
        # claims otherwise is itself a confident wrong answer.
        refusing = sum(v for k, v in summary.items()
                       if k in ("shadowed", "absent", "orphan", "partial",
                                "failed"))
        _detail()
        return emit(Unmeasured(
            reason=f"{refusing} of {total} def types cannot be counted at all "
                   f"and {bad - refusing} more answer without a cross-check "
                   f"({detail})",
            artifact="dump coverage",
            instrument="dumpdb.coverage",
            remedy="`coverage --rows 20` names them with the reason; a count "
                   "for a shadowed/absent type is UNMEASURED, never zero",
        ))
    _detail()
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


def cmd_record(args) -> int:
    db = _db(args)
    m = db.record(args.def_name, def_type=args.type)
    if m.ok and args.rows:
        import json as _j
        print(_j.dumps(m.unwrap(), indent=2)[: args.rows * 200])
    return emit(m)


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
    """The escape hatch — and deliberately NOT a measurement.

    🔴 An earlier cut wrapped whatever came back in `Measured` and stamped the
    artifact's provenance on it. `SELECT loaded_count FROM capture WHERE
    def_type='AbilityDef'` then printed `MEASURED 0` with exit 0 — the exact
    wrong number this package exists to prevent — while `count AbilityDef` on
    the same db in the same second refused. A raw row carries no coverage, so it
    cannot be a measurement. Found by red team 2026-08-21.

    Output is labelled RAW and exits 3 (REFUSED): the rows are real, but this
    command cannot vouch for what they mean. That is the caller's job, which is
    the whole point of an escape hatch.
    """
    db = _db(args)
    q = args.query.strip()
    low = q.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return emit(Refused(
            reason="only SELECT (or a WITH ... SELECT) is allowed; the db is "
                   "opened read-only",
            artifact="sql", instrument="dumpdb.sql",
            right_instrument="rebuild with `measure build` if the data is wrong"))
    if ";" in q.rstrip().rstrip(";"):
        return emit(Refused(
            reason="one statement at a time",
            artifact="sql", instrument="dumpdb.sql",
            right_instrument="run the statements separately"))
    try:
        rows = db.sql(q)
    except Exception as ex:
        return emit(Unmeasured(
            reason=str(ex), artifact="sql", instrument="dumpdb.sql",
            remedy="fix the query, or `measure build` if the db is stale"))
    limit = args.rows or 20
    for r in rows[:limit]:
        print("\t".join("" if c is None else str(c) for c in r))
    if len(rows) > limit:
        print(f"... {len(rows) - limit} more (use --rows)")
    print(f"RAW {len(rows)} row(s) from a read-only query @ {db.against} — "
          f"NOT a measurement: raw rows carry no coverage, so a 0 here may mean "
          f"'not captured'. Use `count`/`coverage` for an answer you can quote.")
    return 3


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
        # "I do not know what this is" is precisely the state the tri-state
        # exists to keep out of MEASURED.
        return emit(Refused(
            reason="not a registered artifact class, so this skill has nothing "
                   "to say about how to read it"
                   + (" — and it is large enough to be worth registering"
                      if big else "; small enough to just read"),
            artifact=args.path, instrument="artifacts.classify",
            right_instrument="read it directly, or add it to "
                             "scripts/measure/artifacts.py"))
    print(f"{args.path} is a {art.kind}")
    print(f"  encoding : {art.encoding}")
    print(f"  use      : {art.instrument}")
    print(f"  literal grep ok: {'yes' if art.literal_scan_ok else 'no'}")
    if art.incident:
        print(f"  incident : {art.incident}")
    return 0


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = _Parser(
        prog="measure",
        description="One question about a large artifact, one line back.")
    ap.add_argument("--dump", help="DefDump directory (default: the live one)")
    ap.add_argument("--db", help=f"path to {DB_NAME}")
    ap.add_argument("--rows", type=int, default=0,
                    help="print up to N records instead of one summary line")

    def _rows(p):
        """Accept --rows after the subcommand too.

        The docs and the tool's own remedy text both say `coverage --rows 20`,
        and a reader who types what they were told must not get a usage error.
        A different dest keeps the subparser default from clobbering the global.
        """
        p.add_argument("--rows", type=int, default=None, dest="rows_sub",
                       help=argparse.SUPPRESS)
        return p
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = _rows(sub.add_parser("build", help="build defs.sqlite from a DefDump"))
    p.add_argument("--only", nargs="*", help="only these def types")
    p.set_defaults(fn=cmd_build)

    p = _rows(sub.add_parser("count", help="how many defs of a type"))
    p.add_argument("def_type")
    p.set_defaults(fn=cmd_count)

    p = _rows(sub.add_parser("types", help="which def types exist"))
    p.add_argument("like", nargs="?")
    p.set_defaults(fn=cmd_types)

    p = _rows(sub.add_parser("coverage", help="which types are NOT fully captured"))
    p.set_defaults(fn=cmd_coverage)

    p = _rows(sub.add_parser("get", help="does this defName exist, and as what"))
    p.add_argument("def_name")
    p.set_defaults(fn=cmd_get)

    p = _rows(sub.add_parser("tag", help="how many defs carry a tag"))
    p.add_argument("tag")
    p.add_argument("--kind", default="weaponTags")
    p.set_defaults(fn=cmd_tag)

    p = _rows(sub.add_parser("flag", help="how many defs the ENGINE classifies this way"))
    p.add_argument("key")
    p.add_argument("--value", default="true")
    p.set_defaults(fn=cmd_flag)

    p = _rows(sub.add_parser("verify", help="check sqlite against the json, type by type"))
    p.add_argument("--only", nargs="*")
    p.set_defaults(fn=cmd_verify)

    p = _rows(sub.add_parser("sql", help="read-only SELECT escape hatch"))
    p.add_argument("query")
    p.set_defaults(fn=cmd_sql)

    p = _rows(sub.add_parser("csv", help="count rows in a CSV without counting the header"))
    p.add_argument("path")
    p.add_argument("--where", help="col=value")
    p.set_defaults(fn=cmd_csv)

    p = _rows(sub.add_parser("explain", help="what is this artifact and what may read it"))
    p.add_argument("path")
    p.set_defaults(fn=cmd_explain)

    args = ap.parse_args(argv)
    if getattr(args, "rows_sub", None) is not None:
        args.rows = args.rows_sub
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
