# Incidents — the wrong numbers that produced this skill

Seven instruments were caught returning confident wrong numbers in a single
session (2026-08-21). None of them errored. None warned. Every one returned a
plausibly-shaped integer. Cases 1–7 are those; 8 and 9 were added on 2026-08-31,
met while optimising this package rather than while using it.

These are worth reading once, not to memorise, but because **each is a different
way for the same shape to happen**, and recognising the shape is the skill.

⚠️ Note where the later ones came from. Cases 1–7 are an instrument lying about
an artifact. Cases 8–9 are an instrument lying about **itself** — a test suite
whose detection power was never checked, and a pair of measured improvements that
cancelled. The shape survives the change of subject.

## 1. `strings` on a .NET assembly — 16 of 115

`strings -a -el <companion>.dll | grep -c '^jawa/'` returned **16**. The
assembly carried **115** such names.

.NET keeps attribute strings, method bodies and custom-attribute blobs in
metadata structures that are not UTF-16 string literals. A byte scan reaches a
minority of them, and reports the minority as the answer.

**The near-miss:** the reading was about to justify rebuilding and redeploying
the assembly, on the theory that 99 tools had gone missing.

⇒ `strings` can prove a name is **present**. It can never prove one is absent.

## 2. `grep` a savegame for map contents — 2, where the answer was 233

Biomes, terrain and placed objects live in compressed grids of 2-byte identifier
hashes. A name appears once or twice in a lookup table no matter how many tiles
carry it.

⇒ Any count of grid-borne content from a text scan is meaningless. The number
returned is the size of the *legend*, not the map.

## 3. The filename collision — `0` where 612 were written

Fully documented in **`building.md` → The keying mistake**. Files keyed on a
simple type name; 532 entries under 517 names; 13 collisions; **824 records
destroyed**.

The part that matters most: a cross-check against three sources passed, because
one collision corrupted all three identically.

## 4. A count that was arithmetically guaranteed to be zero

A tool asked "how many tags were emptied by the cut" and always answered `0`.

The cutting mechanism **neuters** records rather than deleting them — it strips
their fields but leaves them in place. So a tag whose every carrier was cut is
**absent** from a dump-built index rather than **empty** in it. A counter over
that index literally cannot return anything else.

⇒ When a count can only ever be zero, that is a property of the *index*, not of
the world. Ask what the number would look like if the thing you are looking for
did exist — if the answer is "identical", the instrument is wrong.

## 5. A heuristic wider than its evidence — 53 dead paths, 39 of them alive

A texture audit assumed one naming convention and called 39 present assets dead.

⇒ No format change fixes this class. The tool needs to be able to say **"I
cannot judge this one"** — which is what `REFUSED` is for.

## 6. An xpath check that matched nothing and said so as `0 nodes`

A validator pointed at the wrong root reported zero matches for patches that
were live and working. A query that matches nothing and a query that is asking
the wrong question are indistinguishable from their result.

⇒ Validate the instrument against a case whose answer you already know, *before*
trusting it on the case you don't. If the known-good case also returns 0, the
instrument is broken, not the subject.

## 7. A reasonable default that hid the case you cared about

"No tags recorded" was treated as "deliberately has none". 291 records genuinely
had none; a record that *lost* its tags looked identical.

⇒ A heuristic that is right in the general case can be exactly wrong in the case
worth detecting. Where the two are indistinguishable, say so.

---

## The two meta-lessons

Both were learned *while building the fix*, which is the point.

### Agreement is not correctness when sources share a failure mode

The first cut of the instrument passed **16 of 16** of its own tests and
answered `MEASURED 0` for a type holding 612 records. It cross-checked a
manifest count, a per-file total and the parsed rows. All three agreed. All
three were wrong together.

The regression test that now guards this asserts that a *naive* parse still
produces the misleading value — so that anyone "simplifying" the careful parse
back to the obvious one fails immediately, rather than silently reintroducing
the bug with green tests.

### A guard that looks like a gap is usually a guard

Nineteen entries were reported as "a gap nobody had recorded — present but not
cross-checked". They were stale leftovers from removed inputs, and an older tool
had been skipping them deliberately for over a week with a comment explaining
why: *a dead identifier in the index makes a reference to a removed thing
validate clean.*

The new tool had ignored the older tool's rule and was loading **174 dead
records** as live. The bug was in the new tool. The "finding" had to be
retracted and corrected in place.

⇒ Before reporting a finding against something that already exists, read whether
it already handles the case and says why.

---

## Two from optimising this package — 2026-08-31

Both were met while making the build faster, which is the point: neither is about
reading a large file, and both produced numbers that looked right.

### 8. A green suite that had never seen a wrong implementation

42 cases passed. Every one had been written against correct code, so the suite's
own detection power was **untested** — "42/42" established that it ran.

`scripts/mutate_check.py` now breaks the code nine ways and requires a
named-in-advance case to catch each. It found no gaps on the first run, which is
the *only* reason the 42 mean anything.

⇒ The three properties that make it an instrument rather than a ritual:

- **a control run first** — a suite that was already red cannot read as detection
- **the mutation site must match exactly once** — a mutation that silently applied
  nowhere passes every case, which is the same shape as the shard splitter below
- **an undetected mutation is a GAP in the suite**, reported as a finding about the
  suite rather than as a success

### 9. Two optimisations that cancelled each other, each a win alone

| change | measured effect |
|---|---|
| windowed reader | peak memory 306 MB → **24 MB** |
| `PRAGMA cache_size = -262144` | build time **−4%** |
| both together | peak memory **326 MB** |

256 MB of page cache gave back almost exactly what the reader had saved. Neither
change was wrong; the pair was. Both had been measured — **separately**.

Of the four pragmas the source article recommends, measured one at a time on this
workload: `page_size` helped 7%, `locking_mode` did nothing, `temp_store` cost
46 MB for no speed, `cache_size` cost 233 MB for 4%. Only the first shipped.

⇒ **Measure the combination, on the axis you set out to improve.** Borrowed tuning
is tuned for someone else's workload — that article was inserting 100M synthetic
rows. And when a change exists to reduce X, the number that decides it is X with
every other change also applied.

### And one from the benchmark harness, which is the same shape as #6

The shard splitter searched for `},{` and took `hit + 1` as the record start. That
is the comma, not the record, so every candidate boundary failed validation, the
splitter returned **one** shard instead of eight — and the shard counts matched the
serial count *perfectly*, because there was only one shard. The harness printed a
4.76x speedup for parallelism that had not happened.

⇒ **Comparing the ANSWER cannot see that the MECHANISM never engaged.** Assert the
mechanism too: `boundaries()` now checks it produced the shard count it was asked
for. An xpath that matches nothing and a split that never split are the same bug
wearing different clothes.
