# Command and API reference

## Contents
- [CLI](#cli)
- [Exit status](#exit-status)
- [Python API](#python-api)
- [The Measurement types](#the-measurement-types)
- [Querying a built artifact](#querying-a-built-artifact)
- [`find` — the literal question, answered honestly](#find--the-literal-question-answered-honestly)
- [Self-test](#self-test)
- [Proving the self-test can fail](#proving-the-self-test-can-fail)

## CLI

`bin/measure` is a shim over `scripts/measure/cli.py`; either form works.

| command | answers |
|---|---|
| `measure build` | build the queryable form from a capture. Never deletes the source. |
| `measure count <Type>` | how many records of a type |
| `measure coverage` | which slices are NOT fully captured, and why |
| `measure coverage --rows 20` | name them individually with reasons |
| `measure types [LIKE]` | which types exist |
| `measure get <name>` | does this identifier exist, and as what |
| `measure find <literal> [--type T]` | does this exact string occur, and in how many records |
| `measure record <name> [--type T]` | the full record, still coverage-gated |
| `measure tag <tag> [--kind K]` | how many records carry a tag (a join, not an index) |
| `measure flag <key> [--value V]` | how many records the producer classified this way |
| `measure csv <path> --where col=value` | count rows without counting the header |
| `measure count-errors <log> [--top N]` | how many DISTINCT errors in a log, not how many stack-trace lines |
| `measure verify` | check the built form against the source, record by record |
| `measure explain <path>` | what IS this file, and what may read it |
| `measure sql "SELECT …"` | read-only escape hatch |

Flags: `--dump <dir>`, `--db <path>`, `--rows N`. `--rows` is accepted **either
before or after the subcommand** — `measure coverage --rows 20` and
`measure --rows 20 coverage` are the same command.

`--rows N` is the opt-in to detail. Without it every command prints **one line**,
because a question that costs a screenful is a question nobody asks twice.
⚠️ Detail never changes the verdict: `coverage --rows 20` exits with the same
code as `coverage`.

## Exit status

| code | meaning |
|---|---|
| 0 | `MEASURED` |
| 2 | `UNMEASURED` — the artifact does not carry the evidence |
| 3 | `REFUSED` — this instrument cannot judge the question |
| 64 | usage error — your command was malformed. **Not a measurement.** |

🔑 64 rather than argparse's default 2 is deliberate: 2 already means
`UNMEASURED`, so a typo would be indistinguishable from a real finding and a
shell caller would treat its own bug as evidence.

So a shell caller can branch on ignorance without parsing text:

```bash
if measure count "$T" >/dev/null; then echo "known"; else echo "cannot say"; fi
```

## Python API

```python
import sys; sys.path.insert(0, "<skill>/scripts")
from measure.dumpdb import DumpDB, build, open_default
from measure.result import Measured, Unmeasured, Refused

db = open_default()
m = db.count("ThingDef")

if m.ok:                 # ✅ test the KIND of answer
    n = m.unwrap()
else:
    print(m.line())      # carries the reason and the remedy

# ⛔ never: if m.value: ...   Measured(0) is falsy and Unmeasured has no .value
```

`unwrap()` raises `UnmeasuredError` on anything that is not `Measured`, so it is
the right call when a wrong answer must stop the program rather than propagate.

## The Measurement types

All render to a single line via `.line()`, and `str()` does the same.

```python
Measured(value, instrument, artifact, against="", evidence="")
Unmeasured(reason, artifact, instrument="", remedy="")
Refused(reason, artifact, right_instrument="", instrument="")
```

- `against` — what the answer is an answer *about* (capture stamp, fingerprint)
- `evidence` — short, e.g. "3 of 12 rows". Never a record dump.
- `remedy` / `right_instrument` — what to run instead. Treat these as required
  in spirit: a dead-end refusal sends the reader straight back to `grep`.

`Report` collects many measurements. Its `.total()` deliberately **refuses to
sum** when any row is unmeasured — summing across a partial capture is exactly
how a gap becomes a confident number.

## Querying a built artifact

`DumpDB(path, check_currency=True)` — on open it compares the built form's
recorded provenance against the source's current provenance and sets `.stale`.
Every public answer passes through that guard, so a stale artifact returns
`UNMEASURED` rather than an old number.

Staleness is decided by **capture stamp and input fingerprint, never mtime**. A
built form whose source has been deleted is *not* stale — it is the only record
left, and refusing to read it would help nobody.

`record(name, def_type=None)` and `records(def_type, limit=None)` return the
parsed record(s) as the Measurement's value, so a tool that needs FIELDS does not
have to drop out of the typed guarantee to get them. Both are coverage-gated: a
record from a shadowed or orphan slice is refused, because handing one over
implies the capture can vouch for it. A name shared across types refuses rather
than picking one — `Gun_Revolver` is a real `ThingDef`, `SymbolDef` **and**
`WeaponAdjustmentDef`.

`.sql(query)` returns raw rows rather than a Measurement, on purpose: the caller
owns the interpretation, which is the risk the rest of the module removes. Use it
knowingly.

## `find` — the literal question, answered honestly

`find(literal, def_type=None)` is the one command that exists because a
**refusal named no instrument**. The def dump's `literal_scan_ok` is False and
its registered instrument is `measure count <DefType>`, which answers something
else — so "does `Gun_Revolver` appear anywhere in here" had no cheap honest
route, and by this skill's own rule that makes the refusal an obstacle.

⚠️ **It is not faster than grep and does not claim to be.** Measured ~0.1 s per
96 MB of source against ~0.11 s for `grep -o` on the same bytes. Three things
are what it buys:

- **A zero is coverage-gated.** If any slice is shadowed, absent, failed or
  orphaned, "no match" is `UNMEASURED` — *not found where I could look*, which
  is a different claim from *not present*. Only a capture whose every slice is
  complete can return `MEASURED 0`.
- **Hits are attributed** to a record and its type, so a hit can be followed up
  with `measure record`. A grep hit in a 331 MB single-line file cannot.
- **It is genuinely literal.** Built on `instr`, not `LIKE`, for two measured
  reasons: `LIKE` is case-insensitive for ASCII in SQLite, so `find gun_a` would
  match `Gun_A`; and `%`/`_` arriving inside the caller's own string are
  wildcards — `LIKE '%100%%'` returned **1862 rows** on the benchmark where the
  answer was 0.

🔴 **The search is over the dump's ENCODED text, not over decoded field values.**
A producer using `ensure_ascii=True` writes `café` as `caf\u00e9`, so a naive
search for `café` returns a confident zero about a record that plainly contains
it. `find` searches every encoding of the literal a JSON writer might have
produced and says in its evidence how many forms it tried.

## Self-test

```bash
python3 <skill>/scripts/selftest_measure.py
```

Two halves. **Synthetic** cases build a tiny artifact in a temp dir containing a
deliberate collision, a genuinely-empty slice and an orphan — these run anywhere,
with no real capture present. **Live** cases run against a real built artifact if
one exists and *skip with a printed reason* if not; a skip is never counted as a
pass.

The most important case asserts that a **naive parse still produces the
misleading value**. That is what stops someone "simplifying" the careful
duplicate-preserving parse back into the obvious one and reintroducing the
original bug with green tests.

### The hostile fixture and the differential

`make_hostile_dump()` builds **one** capture carrying every failure this package
has been burned by, simultaneously: a lossy collision, a genuinely-empty slice,
an orphan, a BOM'd file whose name disagrees with its header, a truncated file,
a pretty-printed file, escaped non-ASCII, and a record whose text contains
`},{`, an escaped quote and a literal `%`.

Its coverage states are **pinned by name**, so a change that accidentally makes
the fixture healthy fails loudly instead of passing vacuously — the `0/0`
failure from `SKILL.md`, where a sweep with no cases buildable rendered
ignorance as a measurement.

`t_both_readers_give_byte_for_byte_the_same_answers` is the gate for any
performance change to the build: it builds the fixture with both readers and
compares every answer — counts, coverage, types, get, record **contents**, tags,
flags and find.

⚠️ The comparison is built from **unwrapped values**, not from `.line()`.
`Measured._render` deliberately collapses a record to `<record: 7 fields>` to keep
a question to one line, so a surface made of rendered lines would compare two dbs
as identical while every stored field differed.

## Proving the self-test can fail

```bash
python3 <skill>/scripts/mutate_check.py
```

A suite that has only ever been shown correct code has been **run, not tested**.
This copies `scripts/` to a temp dir, introduces one plausible wrong version of
the code at a time — the reader silently dropping a record, `find` rebuilt on
`LIKE`, an orphan's records loaded, the manifest parsed with plain `json` — and
requires the named case to notice.

Three properties make it an instrument rather than a ritual:

- **A control run first.** If the unmutated suite is not green, nothing after it
  can be interpreted, and it says so and stops.
- **The mutation site must match exactly once.** A mutation that silently applied
  nowhere would "pass" every case and read as a gap in the suite — the same shape
  as a shard split that never happened reporting a speedup.
- **An undetected mutation is reported as a GAP**, which is a finding about the
  suite, not about the mutation.
