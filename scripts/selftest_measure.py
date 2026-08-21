#!/usr/bin/env python3
"""Selftest for the measure package — SCANNED_ARTIFACTS_CANNOT_LIE_1.

⭐ WHY THIS TEST EXISTS AT ALL. The rule the whole item rests on is
*"validate the instrument against a case whose answer you already know."*
An instrument that enforces that rule for everyone else and is not itself
validated is the joke writing itself. So this file asks questions whose answers
were established by hand, and fails if the new path returns a plausible wrong
number — which is precisely what the seven old instruments did.

The cases fall into two halves:

  * **synthetic** — a tiny DefDump built in a temp dir, holding a deliberate
    filename collision. These run anywhere, with no game and no live dump.
  * **live** — asked of the real defs.sqlite if one exists, and SKIPPED with a
    line saying so if it does not. A skip is not a pass and is not printed as one.

    python3 skills/measuring-large-artifacts/scripts/selftest_measure.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)          # this skill's scripts/ dir holds the package

from measure import artifacts                                      # noqa: E402
from measure.dumpdb import (
    SCHEMA_VERSION,                                       # noqa: E402
    DumpDB, DB_NAME, build, default_dump_dir, iter_defs,
)
from measure.result import (                                       # noqa: E402
    Measured, Refused, Report, Unmeasured, UnmeasuredError,
)

PASS, FAIL, SKIP = [], [], []


def case(name, fn):
    try:
        fn()
        PASS.append(name)
        print("ok    %s" % name)
    except _Skip as e:
        SKIP.append(name)
        print("skip  %s\n        %s" % (name, e))
    except AssertionError as e:
        FAIL.append(name)
        print("FAIL  %s\n        %s" % (name, e))
    except Exception as e:
        # An infrastructure failure (a locked db, a missing mount) used to
        # escape and kill the run mid-alphabet, so no "N/M passed" line printed
        # and the remaining cases silently never ran. A crashed suite must be
        # loud, and must be distinguishable from a finished one.
        FAIL.append(name)
        print("ERROR %s\n        %s: %s" % (name, type(e).__name__, e))


class _Skip(Exception):
    pass


# --------------------------------------------------------------------------
# a synthetic DefDump, built to contain the exact failure that lost 824 defs
# --------------------------------------------------------------------------

def make_dump(tmp):
    """Two types share the simple name `ThingDef`; one overwrites the other.

    Verse.ThingDef wins the file. Mod.ThingDef declared 3 defs in the manifest
    and has nowhere to live. That is the 2026-08-21 incident in miniature.
    """
    defs_dir = os.path.join(tmp, "defs")
    os.makedirs(defs_dir)

    def write(fname, def_type, names, full=None, extra=None):
        body = []
        for i, n in enumerate(names):
            d = {
                "defName": n, "defType": def_type, "defTypeFull": full or def_type,
                "label": n.lower(), "shortHash": 1000 + i,
                "modName": "Core", "packageId": "ludeon.rimworld",
                "fields": extra.get(n, {}) if extra else {},
            }
            if extra and n in (extra.get("_is") or {}):
                d["is"] = extra["_is"][n]
            body.append(d)
        obj = {"defType": def_type, "defTypeFull": full or def_type,
               "defs": body, "count": len(body)}
        with open(os.path.join(defs_dir, fname), "w", encoding="utf-8") as fh:
            json.dump(obj, fh, separators=(",", ":"))

    write("ThingDef.json", "ThingDef", ["Gun_A", "Gun_B", "Rock"],
          full="Verse.ThingDef",
          extra={"Gun_A": {"weaponTags": ["Gun", "SimpleGun"]},
                 "Gun_B": {"weaponTags": ["Gun"]},
                 "Rock": {},
                 "_is": {"Gun_A": {"weapon": True, "apparel": False},
                         "Gun_B": {"weapon": True, "apparel": False},
                         "Rock": {"weapon": False, "apparel": False}}})
    write("BiomeDef.json", "BiomeDef", ["Desert", "Tundra"])
    write("EmptyDef.json", "EmptyDef", [])

    # ⚠️ Written as RAW TEXT, not json.dump, because the failure being
    # reproduced IS a duplicate key — and no Python dict can hold one.
    # defCounts declares AbilityDef three times exactly as the real manifest
    # does: 612 defs written first, then 18, then 0, each overwriting the last.
    manifest_text = (
        '{"tool":"RimDefDump","toolVersion":"1.0","mode":"all",'
        '"capturedUtc":"2026-08-21T00:00:00Z","gameVersion":"1.6.test",'
        '"modCount":2,'
        '"mods":[{"loadOrder":1,"name":"Core","packageId":"ludeon.rimworld"},'
        '{"loadOrder":2,"name":"Mod","packageId":"some.mod"}],'
        '"defCounts":{"ThingDef":3,"BiomeDef":2,"EmptyDef":0,'
        '"AbilityDef":612,"AbilityDef":18,"AbilityDef":0}}'
    )
    with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
        fh.write(manifest_text)
    # AbilityDef.json exists and is EMPTY — the last writer won the file too.
    write("AbilityDef.json", "AbilityDef", [])
    return tmp


def synthetic_db():
    tmp = tempfile.mkdtemp(prefix="measure_st_")
    make_dump(tmp)
    build(tmp)
    return tmp, DumpDB(os.path.join(tmp, DB_NAME))


# --------------------------------------------------------------------------
# the three types are not interchangeable — this is the whole design
# --------------------------------------------------------------------------

def t_measured_zero_is_not_unmeasured():
    """`0` must be a real answer and `ok`. Truth-testing the value is the bug."""
    m = Measured(value=0, instrument="t", artifact="EmptyDef")
    assert m.ok, "Measured(0) must be ok"
    assert m.unwrap() == 0
    assert m.line().startswith("MEASURED 0"), m.line()
    u = Unmeasured(reason="never captured", artifact="EmptyDef")
    assert not u.ok
    assert u.line().startswith("UNMEASURED"), u.line()
    assert not hasattr(u, "value"), "Unmeasured must not carry a value at all"


def t_unwrap_raises_rather_than_returning_a_number():
    try:
        Unmeasured(reason="r", artifact="a").unwrap()
    except UnmeasuredError:
        return
    raise AssertionError("Unmeasured.unwrap() returned instead of raising")


def t_a_report_refuses_to_total_across_an_unmeasured_row():
    """Summing over a partial capture is how a gap becomes a confident number."""
    r = Report()
    r.add(Measured(value=5, instrument="t", artifact="A"))
    r.add(Unmeasured(reason="absent", artifact="B"))
    tot = r.total()
    assert not tot.ok, "a total was produced across an unmeasured subject: %s" % tot.line()
    assert "B" in tot.line(), tot.line()
    r2 = Report()
    r2.add(Measured(value=5, instrument="t", artifact="A"))
    r2.add(Measured(value=7, instrument="t", artifact="B"))
    assert r2.total().unwrap() == 12


def t_every_refusal_names_the_right_instrument():
    """A refusal with no cheap alternative is an obstacle, and gets routed around."""
    for art in artifacts.REGISTRY:
        assert art.instrument, "%s has no instrument" % art.kind
        txt = artifacts.refusal_text(art, "grep", "x")
        assert art.instrument in txt, "%s refusal does not name its instrument" % art.kind
        assert "Use instead" in txt


# --------------------------------------------------------------------------
# the synthetic dump — the 824-def loss, reproduced and then caught
# --------------------------------------------------------------------------

def t_a_shadowed_type_reads_unmeasured_not_zero():
    """THE case. AbilityDef declared 612, no file holds it. It must not read 0."""
    tmp, db = synthetic_db()
    try:
        got = db.count("AbilityDef")
        assert not got.ok, (
            "a def type the dump never wrote returned a NUMBER: %s — this is "
            "exactly the 824-def loss, and the schema was supposed to make it "
            "impossible" % got.line())
        assert "612" in got.line(), got.line()
        assert got.line().startswith("UNMEASURED"), got.line()
        cov = dict(db.sql("SELECT def_type, coverage FROM capture"))
        assert cov["AbilityDef"] in ("absent", "shadowed"), cov["AbilityDef"]
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_a_genuinely_empty_type_reads_measured_zero():
    """The other half of the same property: real zero must still be answerable."""
    tmp, db = synthetic_db()
    try:
        got = db.count("EmptyDef")
        assert got.ok, "a captured type with no defs must read MEASURED 0: %s" % got.line()
        assert got.unwrap() == 0
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_counts_match_the_json_they_came_from():
    tmp, db = synthetic_db()
    try:
        assert db.count("ThingDef").unwrap() == 3
        assert db.count("BiomeDef").unwrap() == 2
        rep = db.verify_against_json(tmp)
        assert not rep.unmeasured, rep.text()
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_tags_are_a_join_not_a_hand_built_index():
    tmp, db = synthetic_db()
    try:
        assert db.tag("Gun").unwrap() == 2
        assert db.tag("SimpleGun").unwrap() == 1
        dead = db.tag("NoSuchTag")
        assert not dead.ok, (
            "a tag no def carries returned a number; it must refuse, because a "
            "Cherry Picker cut NEUTERS a def rather than deleting it: %s" % dead.line())
        assert isinstance(dead, Refused), dead.line()
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_engine_computed_flags_survive_the_round_trip():
    """The `is` block is C# logic, not XML. Losing it is Cause B all over."""
    tmp, db = synthetic_db()
    try:
        assert db.flag("weapon").unwrap() == 2
        assert db.flag("weapon", value="false").unwrap() == 1
        assert not db.flag("nosuchflag").ok
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_provenance_says_what_the_answer_is_an_answer_about():
    tmp, db = synthetic_db()
    try:
        assert db.prov["mod_count"] == "2"
        assert len(db.prov["modlist_fingerprint"]) == 16
        assert db.prov["captured_utc"] == "2026-08-21T00:00:00Z"
        assert "mods=2" in db.count("ThingDef").line(), db.count("ThingDef").line()
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_fingerprint_is_order_independent_but_membership_sensitive():
    from measure.dumpdb import modlist_fingerprint as fp
    a = [{"packageId": "x"}, {"packageId": "y"}]
    assert fp(a) == fp(list(reversed(a))), "load order changed the fingerprint"
    assert fp(a) != fp(a + [{"packageId": "z"}]), "an added mod did not change it"


# --------------------------------------------------------------------------
# the registry, shared by the library and the hook
# --------------------------------------------------------------------------

def t_classify_recognises_every_artifact_we_have_burned_on():
    cases = {
        "/mnt/c/Users/Mandrake/AppData/LocalLow/Ludeon Studios/RimWorld by "
        "Ludeon Studios/DefDump/defs/ThingDef.json": "defdump",
        "C:\\Users\\x\\Saves\\Ash.rws": "savegame",
        "world/ASHKARR_WORLDMAP_tiles.csv": "worldcsv",
        "/x/y/Player.log": "playerlog",
        "src/x/1.6/Assemblies/JawaBench.dll": "assembly",
    }
    for path, kind in cases.items():
        got = artifacts.classify(path)
        assert got is not None, "unclassified: %s" % path
        assert got.kind == kind, "%s -> %s, wanted %s" % (path, got.kind, kind)
    assert artifacts.classify("scripts/measure/cli.py") is None
    assert artifacts.classify("README.md") is None


def t_the_two_formats_we_do_not_own_are_marked_as_such():
    """No format work is possible on the .rws or a third-party DLL, ever."""
    by = {a.kind: a for a in artifacts.REGISTRY}
    assert by["savegame"].ours is False
    assert by["assembly"].ours is False
    assert by["defdump"].ours is True


# --------------------------------------------------------------------------
# the CSV instrument — grep counts the header, this does not
# --------------------------------------------------------------------------

def t_csv_count_excludes_the_header():
    tmp = tempfile.mkdtemp(prefix="measure_csv_")
    try:
        p = os.path.join(tmp, "t.csv")
        open(p, "w", encoding="utf-8").write(
            "tile,biome\n1,Desert\n2,Desert\n3,Tundra\n")
        sys.path.insert(0, HERE)
        from measure import cli
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["csv", p, "--where", "biome=Desert"])
        line = buf.getvalue().strip()
        assert line.startswith("MEASURED 2 "), line
        assert "3 data rows" in line, line
        # and the word "biome" appears in the header, which grep would count
        assert open(p, encoding="utf-8").read().count("Desert") == 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# live — only if a real db has been built
# --------------------------------------------------------------------------

def _live_db_path():
    """The live db, or a SKIP that says which — absent, or built by an older
    schema.

    🔑 **A schema bump must not make this suite unrunnable.** `defs.sqlite` is
    DERIVED and the documented remedy is `measure build`; until someone runs it
    the live db is simply not a thing this code can read, which is a check that
    COULD NOT RUN rather than one that failed. Reporting it as ERROR made nine
    tests red on a working tree the moment SCHEMA_VERSION moved 2 -> 3, and a
    red suite nobody can green is a suite people stop reading.
    """
    p = os.path.join(default_dump_dir(), DB_NAME)
    if not os.path.exists(p):
        raise _Skip("no %s yet — run `measure build` (this is a SKIP, not a pass)" % p)
    try:
        import sqlite3
        con = sqlite3.connect(p)
        sv = dict(con.execute("SELECT key, value FROM provenance")).get(
            "schema_version")
        con.close()
    except Exception:
        sv = None
    if sv is not None and int(sv) != SCHEMA_VERSION:
        raise _Skip("the live db is schema v%s and this code is v%d — run "
                    "`measure build` to rebuild it (SKIP, not a pass)"
                    % (sv, SCHEMA_VERSION))
    return p


def _live():
    return DumpDB(_live_db_path())


def t_live_abilitydef_is_the_known_wrong_answer_and_is_caught():
    """On the CURRENT dump, taken with the pre-d7cf154 dumper, AbilityDef is a
    known casualty: the manifest declares it and no file carries it. The old
    path said 0. Anything but UNMEASURED here is a regression."""
    db = _live()
    try:
        declared = db.sql(
            "SELECT declared_count, coverage FROM capture WHERE def_type='AbilityDef'")
        if not declared:
            raise _Skip("this capture does not declare AbilityDef")
        dc, cov = declared[0]
        got = db.count("AbilityDef")
        if cov == "complete":
            raise _Skip("this dump was taken with the FIXED dumper — AbilityDef "
                        "is captured, so the collision case cannot be exercised")
        assert not got.ok, (
            "AbilityDef returned a number on a dump that never captured it: %s"
            % got.line())
    finally:
        db.close()


def t_live_thingdef_matches_the_manifest():
    db = _live()
    try:
        got = db.count("ThingDef")
        assert got.ok, got.line()
        dc = db.sql("SELECT declared_count FROM capture WHERE def_type='ThingDef'")[0][0]
        assert got.unwrap() == dc, "sqlite %s vs manifest %s" % (got.unwrap(), dc)
        assert got.unwrap() > 20000, "implausibly few ThingDefs: %s" % got.unwrap()
    finally:
        db.close()


def t_live_a_count_costs_under_100_tokens():
    """Goal 3, measured rather than asserted: one line, and a short one."""
    db = _live()
    try:
        line = db.count("ThingDef").line()
        assert "\n" not in line, "a count printed more than one line"
        assert len(line) < 400, "%d chars is more than a count should cost" % len(line)
    finally:
        db.close()


def t_a_plain_json_load_of_the_manifest_would_lose_the_evidence():
    """🔴 THE REGRESSION GUARD. Do not "simplify" read_manifest to json.load.

    The first cut of this module did exactly that, passed 16/16, and answered
    `MEASURED 0 AbilityDef` — reproducing the very failure it was written to
    prevent. The duplicate keys are the ONLY surviving evidence that 824 defs
    were written and overwritten, and json.load destroys them at parse time.
    """
    from measure.dumpdb import read_manifest, collision_report
    tmp = tempfile.mkdtemp(prefix="measure_dup_")
    try:
        make_dump(tmp)
        path = os.path.join(tmp, "manifest.json")

        naive = json.load(open(path, encoding="utf-8"))
        assert naive["defCounts"]["AbilityDef"] == 0, (
            "the fixture no longer reproduces the failure — it must hold a "
            "duplicate key whose LAST value is the misleading one")

        _, order = read_manifest(path)
        assert order["AbilityDef"] == [612, 18, 0], order.get("AbilityDef")
        coll, lost = collision_report(order)
        assert set(coll) == {"AbilityDef"}, coll
        assert lost == 630, lost
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_agreement_between_corrupted_sources_is_not_completeness():
    """All three sources say 0 for AbilityDef, and all three are wrong together.

    The file's trailing count, the manifest's last value, and the parsed rows
    agree exactly — because a filename collision corrupts all three identically.
    Coverage must NOT read `complete` on that agreement.
    """
    tmp, db = synthetic_db()
    try:
        cov, loaded, fc, dc = db.sql(
            "SELECT coverage, loaded_count, file_count, declared_count "
            "FROM capture WHERE def_type='AbilityDef'")[0]
        assert (loaded, fc, dc) == (0, 0, 0), (loaded, fc, dc)
        assert cov == "shadowed", (
            "three corrupted sources agreed on 0 and coverage read %r" % cov)
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_live_the_824_defs_are_found_offline_with_no_game():
    """The real dump, the real number, no RimWorld and no new DLL required."""
    from measure.dumpdb import read_manifest, collision_report
    path = os.path.join(default_dump_dir(), "manifest.json")
    if not os.path.exists(path):
        raise _Skip("no live dump at %s" % path)
    _, order = read_manifest(path)
    coll, lost = collision_report(order)
    if not coll:
        raise _Skip("this dump has no collisions — taken with the FIXED dumper")
    assert lost == 824, (
        "the collision loss is %d, not the 824 established by hand on "
        "2026-08-21; one of the two numbers is wrong and it matters which" % lost)
    assert len(coll) == 13, len(coll)
    assert coll["AbilityDef"] == [612, 18, 0], coll["AbilityDef"]


def t_a_stale_db_refuses_every_answer():
    """A re-captured dump must not be answered from the old db.

    🔑 Fingerprint, not timestamp: the check is on the manifest's own
    capturedUtc and the mod-set hash, not on any file's mtime.
    """
    tmp, db = synthetic_db()
    try:
        assert db.stale is None, db.stale
        db.close()
        # re-capture: same defs, new timestamp
        text = open(os.path.join(tmp, "manifest.json"), encoding="utf-8").read()
        open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8").write(
            text.replace("2026-08-21T00:00:00Z", "2026-08-22T09:00:00Z"))
        db = DumpDB(os.path.join(tmp, DB_NAME))
        assert db.stale, "a re-captured dump did not read as stale"
        for m in (db.count("ThingDef"), db.get("Gun_A"),
                  db.tag("Gun"), db.flag("weapon")):
            assert not m.ok, "a stale db answered with a number: %s" % m.line()
            assert "stale" in m.line(), m.line()
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_a_mod_list_change_makes_the_db_stale_too():
    tmp, db = synthetic_db()
    try:
        db.close()
        text = open(os.path.join(tmp, "manifest.json"), encoding="utf-8").read()
        open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8").write(
            text.replace('"packageId":"some.mod"', '"packageId":"other.mod"'))
        db = DumpDB(os.path.join(tmp, DB_NAME))
        assert db.stale and "mod set" in db.stale, db.stale
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_an_archived_db_whose_dump_is_gone_is_not_stale():
    """Deleting the source dump must not make its record unreadable."""
    tmp, db = synthetic_db()
    try:
        db.close()
        os.remove(os.path.join(tmp, "manifest.json"))
        db = DumpDB(os.path.join(tmp, DB_NAME))
        assert db.stale is None, db.stale
        assert db.count("ThingDef").unwrap() == 3
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_live_a_collision_that_lost_nothing_is_not_refused():
    """8 names lost defs; 5 collided with an empty loser and lost none.

    Refusing to answer a question that HAS a correct answer is the unearned
    refusal the analysis warned would get the whole tool routed around. The
    split is pinned because the 8 are exactly the 824.
    """
    db = _live()
    try:
        summary = db.coverage_summary()
        if not summary.get("shadowed") and not summary.get("ambiguous"):
            raise _Skip("this dump has no collisions — taken with the FIXED dumper")
        assert summary.get("shadowed") == 8, summary
        assert summary.get("ambiguous") == 5, summary
        lost = int(db.prov["defs_lost_to_collision"])
        assert lost == 824, lost
        # a shadowed name refuses; an ambiguous one answers and says why
        assert not db.count("AbilityDef").ok
        amb = db.count("SymbolDef")
        assert amb.ok and amb.unwrap() == 9099, amb.line()
        assert "owning type is unrecorded" in amb.line(), amb.line()
    finally:
        db.close()


def t_an_orphan_def_type_is_refused_and_its_defs_never_load():
    """🔴 defs/ ACCUMULATES. A file for a type this load did not have is a
    leftover, and its defNames must never enter the index.

    Measured 2026-08-21 on the live dump: all 19 undeclared files were 126-243
    HOURS older than the manifest, while every declared file was written within
    17.8 seconds of it. They are stale, not a gap in the capture — and this
    module reported them as MEASURED for an hour before that was checked.

    The rule is `skills/rimworld-modding/scripts/validate_patch.py`'s, from
    2026-08-13: a dead defName in the index makes a patch that references a
    REMOVED def validate clean. Fail-toward-success.
    """
    tmp = tempfile.mkdtemp(prefix="measure_orph_")
    try:
        make_dump(tmp)
        # a type the manifest never declares — a leftover from a removed mod
        defs_dir = os.path.join(tmp, "defs")
        with open(os.path.join(defs_dir, "DeadModDef.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"defType": "DeadModDef", "count": 1, "defs": [
                {"defName": "GhostFromARemovedMod", "defType": "DeadModDef",
                 "fields": {}}]}, fh)
        build(tmp)
        db = DumpDB(os.path.join(tmp, DB_NAME))
        try:
            got = db.count("DeadModDef")
            assert not got.ok, "an orphan type returned a number: %s" % got.line()
            assert "orphan" in got.line(), got.line()
            ghost = db.get("GhostFromARemovedMod")
            assert not ghost.ok, (
                "a defName from a REMOVED mod is in the index — this is the "
                "fail-toward-success bug validate_patch.py guards against: %s"
                % ghost.line())
            assert db.sql("SELECT COUNT(*) FROM defs "
                          "WHERE def_type='DeadModDef'")[0][0] == 0
        finally:
            db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_live_the_db_holds_exactly_what_the_manifest_DECLARES():
    """The strongest single check available: two numbers derived by wholly
    different routes must land on the same integer."""
    from measure.dumpdb import read_manifest
    db = _live()
    try:
        path = os.path.join(default_dump_dir(), "manifest.json")
        _, order = read_manifest(path)
        declared_sum = sum(v[-1] for v in order.values())
        actual = db.sql("SELECT COUNT(*) FROM defs")[0][0]
        assert actual == declared_sum, (
            "the db holds %d defs, the manifest declares %d — orphan files or "
            "a parse gap" % (actual, declared_sum))
    finally:
        db.close()


def t_count_and_the_rows_table_can_never_disagree():
    """🔴 One tool must not give two answers to one question.

    `count X` reads the slice's total; a COUNT(*) over the rows reads the table.
    When the rows were keyed on each record's own reported CLASS instead of the
    type the dump enumerated, those diverged — 3845 vs 3825 for one real type,
    and 24 vs 0 for another. Both looked authoritative. Found by stress test
    2026-08-21, and it is exactly the failure this whole package exists to stop,
    committed by the package itself.
    """
    tmp, db = synthetic_db()
    try:
        for (t,) in db.sql("SELECT def_type FROM capture "
                           "WHERE coverage IN ('complete','ambiguous')"):
            c = db.count(t)
            if not c.ok:
                continue
            rows = db.sql("SELECT COUNT(*) FROM defs WHERE def_type=?", (t,))[0][0]
            assert c.unwrap() == rows, (
                "count(%s)=%s but the rows table holds %s" % (t, c.unwrap(), rows))
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_live_count_and_rows_agree_for_every_countable_type():
    db = _live()
    try:
        bad = []
        for (t,) in db.sql("SELECT def_type FROM capture "
                           "WHERE coverage IN ('complete','ambiguous')"):
            c = db.count(t)
            if not c.ok:
                continue
            rows = db.sql("SELECT COUNT(*) FROM defs WHERE def_type=?", (t,))[0][0]
            if c.unwrap() != rows:
                bad.append((t, c.unwrap(), rows))
        assert not bad, "%d types disagree with their own rows: %s" % (
            len(bad), bad[:5])
    finally:
        db.close()


def t_live_verify_reconciles_with_coverage():
    """verify and coverage must not accuse the artifact of different damage.

    A reader who is told 42 types disagree but 32 are damaged cannot act on
    either number, and correctly does nothing — which makes both useless.
    """
    db = _live()
    try:
        rep = db.verify_against_json(default_dump_dir())
        bad = [r.artifact for r in rep.unmeasured]
        assert not bad, (
            "verify reports %d disagreements that coverage does not explain: %s"
            % (len(bad), bad[:5]))
    finally:
        db.close()


def t_a_usage_error_never_wears_the_unmeasured_exit_code():
    """Exit 2 means 'the artifact does not carry the evidence'. A typo is not
    that, and a caller branching on status must not read its own bug as a
    finding."""
    import subprocess
    cli = os.path.join(HERE, "measure", "cli.py")
    for argv in (["count"], ["notacommand"], ["--rows", "x", "coverage"]):
        r = subprocess.run([sys.executable, cli] + argv,
                           capture_output=True, text=True)
        assert r.returncode not in (0, 2, 3), (
            "%s exited %d, colliding with a measurement code" % (argv, r.returncode))


def t_detail_flags_never_change_the_verdict():
    """`--rows` asks for more output; it must not turn UNMEASURED into success."""
    import subprocess
    cli = os.path.join(HERE, "measure", "cli.py")
    _live_db_path()
    bare = subprocess.run([sys.executable, cli, "coverage"],
                          capture_output=True, text=True).returncode
    detail = subprocess.run([sys.executable, cli, "coverage", "--rows", "3"],
                            capture_output=True, text=True).returncode
    assert bare == detail, (
        "coverage exits %d but coverage --rows exits %d" % (bare, detail))


# --------------------------------------------------------------------------
# red team, 2026-08-21 — five criticals. Each is pinned here.
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# A FIXED producer's capture — `defTypes` resolves the collision, 2026-08-21.
#
# 🔴 These lock in the bug that a producer fix ALONE could not close. RimDefDump
# d7cf154 separated `Verse.AbilityDef` from `VFECore.Abilities.AbilityDef` into
# two files and added a `defTypes` index saying which is which — and this reader
# threw the work away, because `capture` was keyed on the SIMPLE name and could
# not hold two rows. Measured on a faithful synthetic before the load that would
# have produced it: 630 AbilityDefs on disk, **0** in the table, and `build`
# reporting 615 while the table held 3.
# --------------------------------------------------------------------------

def make_fixed_dump(tmp):
    """A capture in the shape RimDefDump d7cf154 writes.

    Faithful to the producer, checked against DefDumper.cs rather than assumed:
      * the file HEADER carries the SIMPLE name (`DefDumper.cs:510`), so both
        AbilityDef files say `defType: "AbilityDef"` — the index is the only
        thing that separates them
      * `defCounts` is keyed on the FILE STEM (`DefDumper.cs:183-186`), not the
        type name, so there are no duplicate keys any more
      * the loser is written as `SafeFileName(FullName).json` (`:479`)
    """
    defs_dir = os.path.join(tmp, "defs")
    os.makedirs(defs_dir, exist_ok=True)

    def write(stem, simple, full, n):
        body = [{"defName": "%s_%d" % (stem, i), "defType": simple,
                 "defTypeFull": full, "label": None, "shortHash": 5000 + i,
                 "modName": "Core", "packageId": "ludeon.rimworld", "fields": {}}
                for i in range(n)]
        with open(os.path.join(defs_dir, stem + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"defType": simple, "defTypeFullName": full,
                       "defs": body, "count": n}, fh, separators=(",", ":"))

    write("ThingDef", "ThingDef", "Verse.ThingDef", 3)
    write("AbilityDef", "AbilityDef", "Verse.AbilityDef", 612)
    write("VFECore_Abilities_AbilityDef", "AbilityDef",
          "VFECore.Abilities.AbilityDef", 18)
    manifest = {
        "tool": "RimDefDump", "toolVersion": "1.0", "mode": "all",
        "capturedUtc": "2026-08-22T09:00:00Z", "gameVersion": "1.6 test",
        "modCount": 1,
        "mods": [{"loadOrder": 1, "name": "Core", "packageId": "ludeon.rimworld",
                  "rootDir": "/x"}],
        "defCounts": {"ThingDef": 3, "AbilityDef": 612,
                      "VFECore_Abilities_AbilityDef": 18},
        "defTypes": [
            {"name": "ThingDef", "fullName": "Verse.ThingDef",
             "assembly": "Assembly-CSharp", "file": "ThingDef.json", "count": 3},
            {"name": "AbilityDef", "fullName": "Verse.AbilityDef",
             "assembly": "Assembly-CSharp", "file": "AbilityDef.json", "count": 612},
            {"name": "AbilityDef", "fullName": "VFECore.Abilities.AbilityDef",
             "assembly": "VFECore", "file": "VFECore_Abilities_AbilityDef.json",
             "count": 18}],
        "defTypeCollisions": ["AbilityDef"],
        "defTypeCount": 3,
    }
    with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    return tmp


def _fixed():
    tmp = tempfile.mkdtemp(prefix="measure_fixed_")
    make_fixed_dump(tmp)
    build(tmp)
    return tmp, DumpDB(os.path.join(tmp, DB_NAME))


def t_a_resolved_collision_keeps_BOTH_types():
    """The one the producer fix exists for. Neither slice may be dropped."""
    tmp, db = _fixed()
    try:
        rows = {r[0]: r for r in db.sql(
            "SELECT capture_key, def_type, loaded_count, coverage FROM capture")}
        assert set(rows) == {"Verse.ThingDef", "Verse.AbilityDef",
                             "VFECore.Abilities.AbilityDef"}, sorted(rows)
        for k, r in rows.items():
            assert r[3] == "complete", "%s is %s, not complete" % (k, r[3])
        assert rows["Verse.AbilityDef"][2] == 612
        assert rows["VFECore.Abilities.AbilityDef"][2] == 18
        total = db.sql("SELECT COUNT(*) FROM defs")[0][0]
        assert total == 633, "the table holds %d defs, expected 633" % total
    finally:
        db.close(); shutil.rmtree(tmp, ignore_errors=True)


def t_build_reports_the_rows_it_actually_holds():
    """🔴 `build` announced 615 while the table held 3. defs_inserted was counted
    before the shadowed rows were deleted, so the summary and the table told
    different stories about the same capture — this package's own named failure
    mode, inside the package."""
    tmp = tempfile.mkdtemp(prefix="measure_fixed_")
    try:
        make_fixed_dump(tmp)
        stats = build(tmp)
        db = DumpDB(os.path.join(tmp, DB_NAME))
        try:
            held = db.sql("SELECT COUNT(*) FROM defs")[0][0]
        finally:
            db.close()
        assert stats.defs_inserted == held, (
            "build reported %d defs, the table holds %d"
            % (stats.defs_inserted, held))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_a_full_name_counts_exactly():
    tmp, db = _fixed()
    try:
        m = db.count("Verse.AbilityDef")
        assert m.ok and m.unwrap() == 612, m
        m2 = db.count("VFECore.Abilities.AbilityDef")
        assert m2.ok and m2.unwrap() == 18, m2
    finally:
        db.close(); shutil.rmtree(tmp, ignore_errors=True)


def t_a_shared_simple_name_refuses_and_never_sums():
    """⛔ 612 + 18 = 630 is a quantity nothing measured. The honest answer names
    both types and hands over the two commands that DO have answers."""
    tmp, db = _fixed()
    try:
        m = db.count("AbilityDef")
        assert not m.ok, "a shared simple name returned a number: %s" % m
        text = str(m)
        assert "630" not in text, "it summed two different types: %s" % text
        assert "Verse.AbilityDef" in text and "VFECore.Abilities.AbilityDef" in text
        assert "measure count Verse.AbilityDef" in text, (
            "the refusal does not hand over a command that works: %s" % text)
    finally:
        db.close(); shutil.rmtree(tmp, ignore_errors=True)


def t_the_fixed_shape_invents_no_phantom_types():
    """`defCounts` keyed on the file stem put `VFECore_Abilities_AbilityDef` in
    the manifest as a NAME, with no file whose header carries it — and the
    absent-sweep swept it in as a type that does not exist."""
    tmp, db = _fixed()
    try:
        keys = [r[0] for r in db.sql("SELECT capture_key FROM capture")]
        assert "VFECore_Abilities_AbilityDef" not in keys, (
            "a file stem was recorded as a def type: %s" % keys)
        assert len(keys) == 3, keys
    finally:
        db.close(); shutil.rmtree(tmp, ignore_errors=True)


def t_the_escape_hatch_never_mints_a_measurement():
    """🔴 `sql` returned `MEASURED 0` for the canonical wrong number, exit 0,
    with the artifact's provenance stamped on it — while `count` on the same db
    in the same second refused. A raw row carries no coverage."""
    import subprocess
    cli = os.path.join(HERE, "measure", "cli.py")
    _live_db_path()
    r = subprocess.run(
        [sys.executable, cli, "sql",
         "SELECT loaded_count FROM capture WHERE def_type='AbilityDef'"],
        capture_output=True, text=True)
    assert "MEASURED" not in r.stdout, r.stdout
    assert "RAW" in r.stdout, r.stdout
    assert r.returncode == 3, r.returncode


def t_a_crashed_build_leaves_nothing_that_answers():
    """🔴 build writes provenance LAST. A db without it is debris, and it used
    to report `MEASURED 0 def types complete` — a capture that measured nothing
    claiming to be whole."""
    tmp = tempfile.mkdtemp(prefix="measure_wreck_")
    try:
        make_dump(tmp)
        build(tmp)
        db_path = os.path.join(tmp, DB_NAME)
        import sqlite3
        con = sqlite3.connect(db_path)
        con.execute("DELETE FROM provenance")     # simulate the crash
        con.commit(); con.close()
        try:
            DumpDB(db_path)
        except ValueError as ex:
            assert "interrupted build" in str(ex), str(ex)
            return
        raise AssertionError("a db with no provenance opened and answered")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_a_truncated_file_is_recorded_not_fatal():
    """🔴 The generator is consumed outside the open(), so truncation raised
    mid-iteration: COVERAGE_FAILED was unreachable and one bad file aborted the
    whole build."""
    tmp = tempfile.mkdtemp(prefix="measure_trunc_")
    try:
        make_dump(tmp)
        p = os.path.join(tmp, "defs", "BiomeDef.json")
        text = open(p, encoding="utf-8").read()
        open(p, "w", encoding="utf-8").write(text[: len(text) // 2])
        stats = build(tmp)            # must NOT raise
        db = DumpDB(os.path.join(tmp, DB_NAME))
        try:
            cov = dict(db.sql("SELECT def_type, coverage FROM capture"))
            assert cov["BiomeDef"] == "failed", cov["BiomeDef"]
            assert not db.count("BiomeDef").ok
            assert db.count("ThingDef").unwrap() == 3, "one bad file broke the rest"
            assert stats.failed == 1, stats
        finally:
            db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_two_files_claiming_one_type_are_refused_not_merged():
    """🔴 `capture` is keyed on def_type but `defs` rows accumulate, so count
    said 3 where the table held 6 — the keying mistake this package is written
    about, committed in the reader."""
    tmp = tempfile.mkdtemp(prefix="measure_dup2_")
    try:
        make_dump(tmp)
        d = os.path.join(tmp, "defs")
        for name in ("DupA", "DupB"):
            with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as fh:
                json.dump({"defType": "DupDefX", "count": 3, "defs": [
                    {"defName": f"{name}{i}", "defType": "DupDefX", "fields": {}}
                    for i in range(3)]}, fh)
        text = open(os.path.join(tmp, "manifest.json"), encoding="utf-8").read()
        open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8").write(
            text.replace('"ThingDef":3', '"ThingDef":3,"DupDefX":3'))
        build(tmp)
        db = DumpDB(os.path.join(tmp, DB_NAME))
        try:
            got = db.count("DupDefX")
            assert not got.ok, "two files sharing a type returned a number: %s" % got.line()
            rows = db.sql("SELECT COUNT(*) FROM defs WHERE def_type='DupDefX'")[0][0]
            assert rows == 0, "records from an ambiguous pair were ingested: %d" % rows
        finally:
            db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_a_bom_does_not_make_the_filename_authoritative():
    """🔴 A BOM broke the header parse, and the old fallback trusted the
    FILENAME — inventing a def type that never existed and marking the real one
    absent. This module states the inner defType is the authority."""
    tmp = tempfile.mkdtemp(prefix="measure_bom_")
    try:
        make_dump(tmp)
        p = os.path.join(tmp, "defs", "Alpha.json")
        with open(p, "w", encoding="utf-8-sig") as fh:
            json.dump({"defType": "BetaDef", "count": 1, "defs": [
                {"defName": "B1", "defType": "BetaDef", "fields": {}}]}, fh)
        build(tmp)
        db = DumpDB(os.path.join(tmp, DB_NAME))
        try:
            types = {r[0] for r in db.sql("SELECT def_type FROM capture")}
            assert "Alpha" not in types, "the filename was invented as a type"
            assert "BetaDef" in types, types
        finally:
            db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_a_manifest_with_no_capture_stamp_is_not_silently_current():
    """🔴 The freshness check was `if cap and ...`, so a missing stamp skipped
    it entirely and left only a fingerprint that a re-capture reproduces."""
    tmp, db = synthetic_db()
    try:
        db.close()
        text = open(os.path.join(tmp, "manifest.json"), encoding="utf-8").read()
        open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8").write(
            text.replace('"capturedUtc":"2026-08-21T00:00:00Z",', ""))
        db = DumpDB(os.path.join(tmp, DB_NAME))
        assert db.stale, "a manifest with no capturedUtc read as current"
        assert not db.count("ThingDef").ok
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_a_record_stays_inside_the_typed_guarantee():
    """Retrieving FIELDS used to mean dropping to raw sql, i.e. leaving the
    guarantee. `record()`/`records()` keep coverage in front of the data."""
    tmp, db = synthetic_db()
    try:
        r = db.record("Gun_A")
        assert r.ok, r.line()
        assert r.unwrap()["defName"] == "Gun_A"
        assert "<record:" in r.line(), "a record blew the one-line budget: %s" % r.line()
        assert "\n" not in r.line()

        rs = db.records("ThingDef")
        assert rs.ok and len(rs.unwrap()) == 3, rs.line()
        assert "<3 records>" in rs.line(), rs.line()

        # a slice the capture cannot vouch for must not hand over records
        assert not db.records("AbilityDef").ok
        assert not db.record("NoSuchName").ok
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_an_ambiguous_name_refuses_rather_than_picking_one():
    """Three real def types can share a defName. Returning one silently is a
    confident wrong answer of exactly the kind this package exists to stop."""
    db = _live()
    try:
        r = db.record("Gun_Revolver")
        if r.ok:
            raise _Skip("this capture has no name shared across types")
        assert isinstance(r, Refused), r.line()
        assert "def_type=" in r.line(), r.line()
        one = db.record("Gun_Revolver", def_type="ThingDef")
        assert one.ok and one.unwrap()["defType"] == "ThingDef"
    finally:
        db.close()


def t_a_rebuild_never_shows_a_reader_a_partial_db():
    """build wrote in place, so for the ~60 s of a rebuild every other window
    saw a missing then a half-written file — observed as `database is locked`
    and `disk image is malformed`."""
    from measure.dumpdb import build as _b
    import inspect
    src = inspect.getsource(_b)
    assert "os.replace(" in src, "build no longer renames atomically"
    assert ".building" in src, "build no longer writes to a temp path"


def t_every_documented_command_actually_exists():
    """A subcommand was documented, implemented, and never registered — and the
    suite stayed green, because nothing checked the CLI's surface against the
    docs. That is the same shape as everything else here: an artifact (the docs)
    asserting something nobody measured.
    """
    import re
    import subprocess
    api = os.path.join(os.path.dirname(HERE), "references", "api.md")
    if not os.path.exists(api):
        raise _Skip("no references/api.md beside this skill")
    documented = set(re.findall(r"^\| `measure ([a-z]+)", open(api, encoding="utf-8").read(),
                                re.M))
    assert documented, "could not parse any commands out of api.md"
    cli = os.path.join(HERE, "measure", "cli.py")
    helptext = subprocess.run([sys.executable, cli, "--help"],
                              capture_output=True, text=True).stdout
    registered = set(re.findall(r"[{,]([a-z]+)", helptext.split("...")[0]))
    missing = sorted(documented - registered)
    assert not missing, "documented but not registered: %s" % missing


def t_every_registered_command_is_documented():
    """The other direction: a command nobody can discover is a command nobody
    uses, and this skill's whole premise is that the correct path must be the
    easy one."""
    import re
    import subprocess
    api = os.path.join(os.path.dirname(HERE), "references", "api.md")
    if not os.path.exists(api):
        raise _Skip("no references/api.md beside this skill")
    text = open(api, encoding="utf-8").read()
    cli = os.path.join(HERE, "measure", "cli.py")
    helptext = subprocess.run([sys.executable, cli, "--help"],
                              capture_output=True, text=True).stdout
    registered = set(re.findall(r"[{,]([a-z]+)", helptext.split("...")[0]))
    undocumented = sorted(c for c in registered
                          if f"`measure {c}" not in text)
    assert not undocumented, "registered but undocumented: %s" % undocumented


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("t_"):
            case(k[2:], v)
    print("\n%d/%d passed, %d skipped"
          % (len(PASS), len(PASS) + len(FAIL), len(SKIP)))
    sys.exit(1 if FAIL else 0)
