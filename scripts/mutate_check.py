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
        "the manifest is parsed with plain json, losing the duplicate keys",
        "measure/dumpdb.py",
        "        manifest = json.loads(fh.read(), object_pairs_hook=_pairs_hook)",
        "        manifest = json.loads(fh.read())",
        "a_shadowed_type_reads_unmeasured_not_zero",
    ),
]


def run_suite(root):
    r = subprocess.run([sys.executable,
                        os.path.join(root, "selftest_measure.py")],
                       capture_output=True, text=True)
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
