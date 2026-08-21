# Building an artifact that cannot lie

Read this when you are **designing or changing** a dump, export, cache or
capture — not when you are merely querying one. It is the part that decides
whether the reader ever *can* get a right answer.

## Contents

- [The four properties](#the-four-properties)
- [Choosing a format](#choosing-a-format)
- [The keying mistake, in detail](#the-keying-mistake-in-detail)
- [Coverage as a first-class field](#coverage-as-a-first-class-field)
- [Provenance](#provenance)
- [Accumulation and pruning](#accumulation-and-pruning)
- [Adding an artifact class to the registry](#adding-an-artifact-class-to-the-registry)
- [Migrating a format that people already trust](#migrating-a-format-that-people-already-trust)

## The four properties

An artifact meant to be *measured* rather than read needs four things. Missing
any one of them pushes a guess onto every future reader.

1. **Keys that cannot collide.** If two records can claim the same key, one will
   be lost silently.
2. **Per-slice coverage.** For every slice the artifact claims to cover, a field
   saying whether it actually did — and if not, why.
3. **Provenance.** What this is an answer *about*: input set, fingerprint,
   capture time, producing tool and version.
4. **A pruning story.** Either the writer cleans what no longer exists, or the
   reader can tell fresh entries from leftovers.

## Choosing a format

The trade is real and worth making deliberately rather than by habit.

| | JSON (one blob) | JSONL + index | SQLite |
|---|---|---|---|
| read one record without loading all | ✗ | ✓ | ✓ |
| count without parsing | ✗ | ✗ | ✓ |
| `NULL` vs `0` vs absent distinguishable | by convention | by convention | **by schema** |
| diffable / greppable | ✓ | ✓ | ✗ |
| hand-repairable | ✓ | ✓ | ✗ |
| joins correct by construction | ✗ | ✗ | ✓ |
| dependency | none | none | stdlib (`sqlite3`) |

**Rules of thumb.** Under a few MB, keep JSON and add coverage + provenance —
the read cost is not the problem, so a format change buys little. Once no human
or agent can read it whole, the read cost *is* the problem, and the answer is
line-addressability (JSONL) or a real query engine (SQLite). Choose SQLite when
the artifact must express *absence* precisely, or when the interesting questions
are joins ("which X carry tag Y, and did Y survive the cut") — those are exactly
the questions a hand-rebuilt index gets wrong.

⚠️ **Do not reach for SQLite reflexively.** A 1.7 MB CSV does not earn a
database. The deliverable is a trustworthy *measurement*, and often that is a
twenty-line script that prints one line.

## The keying mistake, in detail

This is the one that destroys data, so it gets its own section.

A capture writes one file (or one map entry) per type, keyed by the type's
**simple name**. Two types in different namespaces share a simple name. The
second write silently replaces the first. Nothing errors, because at the moment
of writing, each write is individually valid.

Then the damage compounds in a way that defeats the obvious defence:

```
defs/AbilityDef.json         <- written 3 times, last writer had 0 records
manifest.defCounts           <- "AbilityDef" written 3 times, JSON keeps the last
the file's own record count  <- belongs to the last writer
```

A reader that cross-checks all three sees `0 == 0 == 0` and reports **complete**.
The check passes. 630 records are gone. This is the "agreement is not
correctness" lesson in its purest form: three sources, one shared failure.

**The only surviving evidence was the duplicate keys themselves** — 532 entries
under 517 names — and `json.load` destroys that at parse time, because a dict
cannot hold a duplicate key. Recovering it requires reading the pairs before they
become a dict:

```python
class DupDict(dict):
    __slots__ = ("pairs",)

def _pairs_hook(pairs):
    d = DupDict(pairs)
    d.pairs = list(pairs)          # every pair, including the shadowed ones
    return d

manifest = json.loads(text, object_pairs_hook=_pairs_hook)
order = {}
for k, v in manifest["defCounts"].pairs:
    order.setdefault(k, []).append(v)      # [612, 18, 0] survives
```

**Fixes, in preference order:** key on the fully-qualified name; or detect the
collision at write time and refuse; or, failing both, preserve the duplicates so
a reader can still see what happened. The third is a forensic tool, not a design.

## Coverage as a first-class field

Give every slice a coverage state. These five carry their weight; add more only
when a real case demands it.

| state | meaning | may a count be returned? |
|---|---|---|
| `complete` | captured, and the counts agree | yes |
| `partial` | captured, counts disagree | no — say by how much |
| `absent` | claimed but nothing written | no |
| `shadowed` | key collided and records were lost | no |
| `orphan` | present but not part of THIS capture | no — and do not load its records |

🔑 **Distinguish an earned refusal from an unearned one.** In the real case, 13
keys collided but only 8 lost records — for the other 5, the loser held nothing,
so the count is right and only the *owning identity* is unknown. Refusing all 13
would have been over-strict, and over-strictness is how the instrument gets
abandoned. Those 5 answer, with the ambiguity stated in the evidence line.

## Provenance

Store, in the artifact:

- **a fingerprint of the input set** — order-independent if order does not change
  the result, so that a reordering is not mistaken for a change
- **the capture time** as the producer recorded it, not the file's mtime
- **the producing tool and its version**
- **where it came from**, so staleness can be checked later

Then every answer quotes it. `MEASURED 24904 ThingDef @ mods=578/e0f11692 captured=…`
is re-derivable; `24904` is a rumour.

## Accumulation and pruning

If the writer creates one file per slice and never deletes, the directory is a
**union of every capture ever taken**, not a snapshot. Detect it by comparing
each part against what this capture declared:

- declared and present → real
- declared, absent → a gap
- **present, undeclared → an orphan**, and its records must not be loaded

The measurement that settles it: compare each part's write time against the
manifest's. In the real case every genuine part was written within **17.8
seconds** of the manifest, and every orphan was **126–243 hours** older. That is
not a marginal call, and it is worth actually running rather than assuming.

⚠️ Loading orphan records is not a cosmetic error. A stale identifier in the
index makes a reference to a *removed* thing validate clean — the failure
direction that hides real breakage.

## Adding an artifact class to the registry

`scripts/measure/artifacts.py` holds one table describing each artifact class,
read by both the instrument and any guard rail, so advice and enforcement cannot
drift. Add an entry with:

- `patterns` — globs matched against the forward-slash path
- `encoding` — **why a byte scan returns a wrong number.** Write the mechanism,
  not the verdict; the reader needs to be able to judge their own case.
- `instrument` — the command that DOES answer. Never leave this vague.
- `literal_scan_ok` — is a literal-string search of this legitimate?
- `ours` — can we change this format at all? Third-party and OS-owned formats
  can only ever get a reader, never a redesign.
- `incident` — what actually went wrong, with the numbers. This is what makes a
  refusal persuasive rather than bureaucratic.

Then add a case to `scripts/selftest_measure.py` so the classification is pinned.

## Migrating a format that people already trust

A format change is also a **trust** change, and should not be a single step.

1. Write the new form **alongside** the old. Delete nothing.
2. Verify the new against the old, record by record, and keep that check
   runnable.
3. Point readers at the new form only once the check is green on a real capture.
4. Retire the old form last, deliberately, as its own change.

And validate the new instrument against **a case whose answer you already know
independently** — not against a second reading of the same artifact. Two
independent routes landing on the same integer is the strongest cheap evidence
available; two readings of one corrupted source landing together is worthless.
