# Command and API reference

## Contents
- [CLI](#cli)
- [Exit status](#exit-status)
- [Python API](#python-api)
- [The Measurement types](#the-measurement-types)
- [Querying a built artifact](#querying-a-built-artifact)
- [Self-test](#self-test)

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
| `measure tag <tag> [--kind K]` | how many records carry a tag (a join, not an index) |
| `measure flag <key> [--value V]` | how many records the producer classified this way |
| `measure csv <path> --where col=value` | count rows without counting the header |
| `measure verify` | check the built form against the source, record by record |
| `measure explain <path>` | what IS this file, and what may read it |
| `measure sql "SELECT …"` | read-only escape hatch |

Global flags: `--dump <dir>`, `--db <path>`, `--rows N`.

`--rows N` is the opt-in to detail. Without it every command prints **one line**,
because a question that costs a screenful is a question nobody asks twice.

## Exit status

| code | meaning |
|---|---|
| 0 | `MEASURED` |
| 2 | `UNMEASURED` — the artifact does not carry the evidence |
| 3 | `REFUSED` — this instrument cannot judge the question |

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

`.sql(query)` returns raw rows rather than a Measurement, on purpose: the caller
owns the interpretation, which is the risk the rest of the module removes. Use it
knowingly.

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
