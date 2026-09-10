#!/usr/bin/env python3
"""Does the selftest actually FAIL when the code is wrong?

⭐ WHY THIS FILE EXISTS. `selftest_measure.py` passing tells you the suite ran.
It does not tell you the suite can detect anything — and the whole of
`SKILL.md`'s 2026-08-26 entry is about five instruments that were calibrated on
the right answer, passed, and were wrong the first time. A test suite is an
instrument like any other, so it gets the same treatment: shown a KNOWN NEGATIVE
and required to notice.

Each mutation below is a plausible wrong version of the code — the kind a
refactor or a "simplification" would actually produce. For each one, the named
case MUST fail. A mutation that nothing catches is reported as a GAP, which is a
finding about the suite, not about the mutation.

    python3 scripts/mutate_check.py

Nothing is written to the real package: the whole `scripts/` tree is copied to a
temp dir and mutated there.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

#: (name, file, find, replace, the case that must catch it)
#:
#: `find` must match exactly once in the file — a mutation that silently applied
#: nowhere would "pass" every case and read as a gap in the suite, which is the
#: same class of error as a split that never happened reporting a speedup.
MUTATIONS = [
    (
        "reader drops the last record of every file",
        "measure/dumpdb.py",
        '        if buf[idx] == "]":\n            return',
        '        if buf[idx] == "]":\n            return\n'
        '        if fh is not None and idx and not buf[idx:].count("{") > 1:\n'
        '            return',
        "both_readers_give_byte_for_byte_the_same_answers",
    ),
    (
        "reader stops refilling, so a record spanning a boundary is lost",
        "measure/dumpdb.py",
        "            except ValueError:\n"
        "                if fh is None:\n"
        "                    raise\n"
        "                more = fh.read(window)\n"
        "                if not more:\n"
        "                    raise\n"
        "                buf, idx = buf[idx:] + more, 0",
        "            except ValueError:\n"
        "                raise",
        "a_window_smaller_than_one_record_still_reads_every_record",
    ),
    (
        "build re-serialises the record instead of storing the source span",
        "measure/dumpdb.py",
        "                    span,\n                ))",
        "                    json.dumps(d, separators=(',', ':'),\n"
        "                               ensure_ascii=False),\n                ))",
        "a_stored_record_is_what_the_producer_WROTE",
    ),
    (
        "an empty slice and a shadowed one both answer 0",
        "measure/dumpdb.py",
        "        if coverage in (COVERAGE_ABSENT, COVERAGE_SHADOWED, COVERAGE_FAILED):",
        "        if coverage in (COVERAGE_ABSENT, COVERAGE_FAILED):",
        "a_shadowed_type_reads_unmeasured_not_zero",
    ),
    (
        "an orphan's records are loaded into the index",
        "measure/dumpdb.py",
        "        if not entry and inner_type not in declared_order and stem not in declared_order:",
        "        if False:",
        "an_orphan_def_type_is_refused_and_its_defs_never_load",
    ),
    (
        "find is built on LIKE, so the caller's own % is a wildcard",
        "measure/dumpdb.py",
        '"instr(json, ?) > 0" for _ in forms',
        '"json LIKE \'%\'||?||\'%\'" for _ in forms',
        "find_is_a_literal_search_not_a_LIKE_PATTERN",
    ),
    (
        "find's zero is no longer gated on coverage",
        "measure/dumpdb.py",
        "            if blind or murky:",
        "            if False:",
        "find_zero_is_UNMEASURED_unless_every_slice_was_searchable",
    ),
    # ---- the 2026-09-09 finder sweep. Each pairs with the case that caught
    # the real defect, so the case cannot quietly stop testing for it.
    (
        "find's zero is gated on unreadable slices but not on unattributable hits",
        "measure/dumpdb.py",
        "            if blind or murky:",
        "            if blind:",
        "find_zero_refuses_when_a_hit_cannot_be_attributed",
    ),
    (
        "find's blind gate ignores the --type scope again",
        "measure/dumpdb.py",
        "cov, blind, n_scope = self._attribution(def_type)",
        "cov, blind, n_scope = self._attribution(None)",
        "a_scoped_find_is_not_refused_for_an_unrelated_broken_slice",
    ),
    (
        "a dropped slice keeps its tag and flag rows",
        "measure/dumpdb.py",
        '    con.execute("DELETE FROM def_tags WHERE def_id IN (%s)" % ids, (def_type,))\n'
        '    con.execute("DELETE FROM def_flags WHERE def_id IN (%s)" % ids, (def_type,))\n',
        "",
        "a_dropped_slice_takes_its_tags_and_flags_with_it",
    ),
    (
        "tag() counts rows from a slice the capture cannot vouch for",
        "measure/dumpdb.py",
        '            "JOIN defs d ON d.id = t.def_id WHERE t.kind=? AND t.tag=?",\n'
        "            (kind, tag),\n        ).fetchall()\n"
        "        solid = [r for r in rows if cov(r[1], r[2]) in self._VOUCHABLE]",
        '            "JOIN defs d ON d.id = t.def_id WHERE t.kind=? AND t.tag=?",\n'
        "            (kind, tag),\n        ).fetchall()\n"
        "        solid = rows",
        "get_is_coverage_gated_exactly_like_count_and_record",
    ),
    (
        "flag() counts rows from a slice the capture cannot vouch for",
        "measure/dumpdb.py",
        '            "JOIN defs d ON d.id = f.def_id WHERE f.key=? AND f.value=?",\n'
        "            (key, value),\n        ).fetchall()\n"
        "        solid = [r for r in rows if cov(r[1], r[2]) in self._VOUCHABLE]",
        '            "JOIN defs d ON d.id = f.def_id WHERE f.key=? AND f.value=?",\n'
        "            (key, value),\n        ).fetchall()\n"
        "        solid = rows",
        "get_is_coverage_gated_exactly_like_count_and_record",
    ),
    (
        "get() answers from a slice count and record both refuse",
        "measure/dumpdb.py",
        "        solid = [r for r in rows if cov(r[0], r[5]) in self._VOUCHABLE]",
        "        solid = rows",
        "get_is_coverage_gated_exactly_like_count_and_record",
    ),
    (
        "a slice's full_name is overwritten by its records' subclass",
        "measure/dumpdb.py",
        "                    full_name,\n"
        "                    concrete_full if concrete_full and concrete_full != full_name\n"
        "                    else None,",
        "                    concrete_full or full_name,\n"
        "                    None,",
        "a_slices_full_name_is_the_slices_not_its_records",
    ),
    (
        "records() binds one argument to two placeholders",
        "measure/dumpdb.py",
        "        args = (def_type, def_type) if dotted else (def_type,)",
        "        args = (def_type,)",
        "a_dotted_type_name_can_actually_be_asked_for_its_records",
    ),
    (
        "the manifest is read as plain utf-8, so a BOM kills the build",
        "measure/dumpdb.py",
        '    with open(path, "r", encoding="utf-8-sig") as fh:\n'
        "        manifest = json.loads",
        '    with open(path, "r", encoding="utf-8") as fh:\n'
        "        manifest = json.loads",
        "a_BOM_on_the_manifest_does_not_kill_the_whole_build",
    ),
    (
        "the build abandons a shadowed file without closing it",
        "measure/dumpdb.py",
        "            it.close()          # nothing will be read from it; see _DefIter",
        "            pass",
        "the_build_closes_a_file_it_reads_nothing_from",
    ),
    (
        "a file that fails at open is not counted as a type seen",
        "measure/dumpdb.py",
        "            stats.types_seen += 1\n            continue\n\n        inner_type",
        "            continue\n\n        inner_type",
        "types_captured_counts_every_capture_row_a_file_wrote",
    ),
    (
        "an empty capture reads as complete coverage",
        "measure/cli.py",
        "    if total == 0:",
        "    if False:",
        "a_capture_that_holds_nothing_is_not_a_clean_build",
    ),
    (
        "a build that captured nothing still reports MEASURED",
        "measure/cli.py",
        "    if stats.types_seen == 0 or (stats.defs_inserted == 0 and stats.failed):",
        "    if False:",
        "a_build_where_every_file_failed_is_not_MEASURED_zero",
    ),
    (
        "the multi-statement guard is a raw ';' substring test again",
        "measure/cli.py",
        "    try:\n        rows = db.sql(q)",
        '    if ";" in q.rstrip().rstrip(";"):\n'
        "        return emit(Refused(\n"
        '            reason="one statement at a time",\n'
        '            artifact="sql", instrument="dumpdb.sql",\n'
        '            right_instrument="run the statements separately"))\n'
        "    try:\n        rows = db.sql(q)",
        "a_semicolon_inside_a_literal_is_not_two_statements",
    ),
    (
        "the log reader assumes utf-8 whatever the file is",
        "measure/playerlog.py",
        "getincrementaldecoder(_sniff_encoding(head))",
        'getincrementaldecoder("utf-8")',
        "a_utf16_log_is_read_rather_than_dismissed_as_not_a_log",
    ),
    (
        "find searches only the raw literal, not its escaped forms",
        "measure/dumpdb.py",
        "        for f in (literal,\n"
        "                  json.dumps(literal, ensure_ascii=False)[1:-1],\n"
        "                  json.dumps(literal, ensure_ascii=True)[1:-1]):",
        "        for f in (literal,):",
        "find_searches_the_ESCAPED_form_of_a_literal_too",
    ),
    (
        "the manifest is parsed with plain json, losing the duplicate keys",
        "measure/dumpdb.py",
        "        manifest = json.loads(fh.read(), object_pairs_hook=_pairs_hook)",
        "        manifest = json.loads(fh.read())",
        "a_shadowed_type_reads_unmeasured_not_zero",
    ),
]


def run_suite(root):
    # 🔑 POINTED AT A DUMP THAT IS NOT THERE, ON PURPOSE. The live cases are
    # SKIPPED whenever the machine has no capture — so on almost every machine
    # they can never be the case that catches a mutation, and a detection that
    # depends on one operator's 877 MB defs.sqlite is not a detection. Skipping
    # them here also keeps a run to seconds per mutation instead of minutes: the
    # live suite alone takes over ten on this drive, times one run per mutation.
    env = dict(os.environ, RIMWORLD_DEFDUMP=os.path.join(root, "no_such_dump"))
    r = subprocess.run([sys.executable,
                        os.path.join(root, "selftest_measure.py")],
                       capture_output=True, text=True, env=env)
    failed = set(re.findall(r"^FAIL  (\S+)", r.stdout, re.M))
    errored = set(re.findall(r"^ERROR (\S+)", r.stdout, re.M))
    return failed, errored, r.stdout


def main():
    base = tempfile.mkdtemp(prefix="measure_mut_")
    clean = os.path.join(base, "clean")
    shutil.copytree(HERE, clean, ignore=shutil.ignore_patterns("__pycache__"))

    # ⚠️ THE CONTROL. If the unmutated suite does not pass, every "caught" below
    # is meaningless — the case was already red. This is the calibration step the
    # whole file is about, applied to the file itself.
    failed, errored, out = run_suite(clean)
    if failed or errored:
        print("CONTROL FAILED — the clean suite is not green, so nothing below "
              "can be interpreted:\n  %s" % sorted(failed | errored))
        return 1
    print("control: clean suite green\n")

    gaps = []
    for name, relpath, find, repl, expect in MUTATIONS:
        work = os.path.join(base, re.sub(r"\W+", "_", name)[:40])
        shutil.copytree(clean, work)
        p = os.path.join(work, relpath)
        text = open(p, encoding="utf-8").read()
        n = text.count(find)
        if n != 1:
            print("SKIP  %s\n        the mutation site matched %d times, not 1 — "
                  "the mutation was not applied, so 'caught' would be a lie"
                  % (name, n))
            gaps.append((name, "mutation site not found"))
            continue
        open(p, "w", encoding="utf-8").write(text.replace(find, repl))

        failed, errored, out = run_suite(work)
        caught = failed | errored
        if expect in caught:
            print("caught  %-58s by %s" % (name, expect))
        elif caught:
            print("CAUGHT ELSEWHERE  %s\n        expected %s, actually failed: %s"
                  % (name, expect, sorted(caught)[:4]))
        else:
            print("GAP     %-58s NOTHING FAILED" % name)
            gaps.append((name, "no case detected it"))

    shutil.rmtree(base, ignore_errors=True)
    print()
    if gaps:
        print("%d of %d mutations went undetected:" % (len(gaps), len(MUTATIONS)))
        for name, why in gaps:
            print("  - %s (%s)" % (name, why))
        return 1
    print("all %d mutations detected" % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
