---
name: measuring-large-artifacts
description: Answer questions about files too large to read — def dumps, savegames, logs, database exports, packed binaries, giant CSV/JSON — without returning a confident wrong number. Use this whenever you are about to grep, strings, wc, awk or sed a file you cannot open whole; whenever a count, census or "how many X are there" decides something expensive; when building or changing a dump/export format; when a tool reports 0 or an empty result you are not certain is real; when two sources disagree about a number; and when deciding whether a cached or derived artifact is still current. Also use it before trusting any number that came out of a large file, even one you produced yourself — especially then.
---

# Measuring large artifacts

An artifact you cannot read whole is not a file any more, it is a **measuring
problem**. This skill is about getting a number out of one that you can actually
bet on, and about knowing — out loud, in the answer — when you cannot.

## The chain that produces a wrong number

Every incident behind this skill has the same shape. It is worth holding in your
head as a chain, because the fix is not at the end:

```
the artifact is too large to READ
        ↓
so you reach for a SCANNING tool (grep, strings, wc)
        ↓
the scanner does not understand the artifact's ENCODING
        ↓
it returns a NUMBER, plausibly shaped, with no error
        ↓
that number decides something expensive
```

Notice where the failure is *not*. `grep` is not broken. It answered the question
it was asked — "how many lines contain this byte sequence" — perfectly. The
mismatch is that you asked a different question and read its answer as if it were
yours.

And notice the middle link. **Size is what makes people reach for the wrong
tool.** Any fix that does not make the correct path *cheaper* than the scan will
be routed around, exactly the way an annoying lock gets propped open. If you take
one thing from this skill, take that: an instrument nobody reaches for is not an
instrument.

## The three answers

The core move is to stop returning bare numbers. Every measurement is one of
three things, and collapsing them is the bug:

| answer | means | what `0` would have hidden |
|---|---|---|
| `MEASURED 612` | the number is real, and it says what it was measured against | — |
| `UNMEASURED` | the artifact does not carry the evidence | "not captured" reading as "none exist" |
| `REFUSED` | this instrument cannot judge this question | "cannot tell" reading as "none exist" |

**`0` must be able to mean only one thing: measured zero.** This is the single
highest-value property in the whole skill, and everything else is downstream of
it. Ignorance gets its own word; inability gets its own word.

Two consequences that catch people:

- **`if count:` is the bug.** `Measured(0)` is falsy and `Unmeasured` has no
  value at all. Test the *kind* of answer, never the truthiness of the number.
- **A refusal that does not name the right instrument is just an obstacle**, and
  obstacles get bypassed. Every `UNMEASURED` and `REFUSED` carries a remedy for
  the same reason a good error message carries a fix.

## Using it

`measure` is `bin/measure` in this skill. It is **not on `PATH` by default** —
either add `bin/` to `PATH`, or call the CLI directly:

```bash
python3 ~/.claude/skills/measuring-large-artifacts/scripts/measure/cli.py count <Type>
```

```bash
measure count <Type>          # MEASURED 24904  /  UNMEASURED + why
measure coverage              # what the artifact did NOT capture
measure get <name>            # does this exist, and as what
measure find <literal>        # does this exact string occur, and where
measure explain <path>        # what IS this file, and what may read it
measure csv <path> --where col=value
measure build                 # (re)build the queryable form
```

Exit status carries the tri-state so scripts can branch: `0` measured, `2`
unmeasured, `3` refused — and **`64` for a malformed command**, deliberately
outside the measurement codes so a typo can never be read as a finding. One line
of output per question by design; `--rows N` adds detail **without changing the
verdict or the exit code**.

`measure explain <path>` is the cheapest useful thing here and the best habit:
before scanning anything big, ask what it is. It answers from the same registry
the guard rail uses, so the advice and the enforcement can never drift apart.

Full command reference and the Python API: **`references/api.md`**.

## Building one

If you are designing the dump, export or cache — rather than just reading one —
read **`references/building.md`** before you choose a layout. It covers the four
properties a large artifact needs (unique keys, per-slice coverage, provenance,
pruning), why SQLite beats JSONL beats JSON for most of them, and the specific
mistakes that destroyed data in the incidents below.

The short version, because it is the part people skip:

- **Key on something that cannot collide.** A simple name is not unique. If two
  things can share a key, one of them will be silently overwritten.
- **Record what you did NOT capture**, per slice, as a first-class field. An
  artifact that cannot say "I don't have that" forces every reader to guess.
- **Stamp what the artifact is an answer ABOUT** — the input set, a fingerprint,
  a capture time. A number with no provenance cannot be re-derived or trusted
  later.
- **Prune, or mark, what accumulates.** A directory that is written but never
  cleaned mixes today's capture with last week's.

## Watching one

Two failure modes are about *time*, not encoding, and they are the ones that bite
after everything looks finished.

**Staleness — fingerprint, not timestamp.** An mtime tells you when bytes were
written, which is a different question from whether the artifact still describes
the world you are asking about. Compare the *content identity* — a capture stamp,
a hash of the input set. A re-run that produced identical bytes is not stale; a
touched file is not fresh. And when the source is simply gone, that is **not**
stale: an archived artifact is the only record left, and refusing to read it
helps nobody.

**Accumulation.** Many capture tools write a file per slice and never delete the
file for a slice that stopped existing. A directory that looks freshly written
can hold entries from weeks ago. Check whether each part was written by *this*
capture, and treat leftovers as orphans rather than data — see the incident below
for what happens when you don't.

## Appropriate uses — and the honest limits

A derived artifact answers exactly one question well: *what was true when it was
captured, about the inputs it captured.* Push it past that and it becomes a
confident liar again.

| the question | the instrument |
|---|---|
| what exists / how many, as of a capture | the artifact |
| what is true RIGHT NOW in a running system | the live system, never the artifact |
| what a config or source file SAYS | read the source; a dump shows the result, not the intent |
| does this exact literal string occur | a scan is fine — `measure find` adds coverage |
| how many, of something structured | never a scan |

⚠️ **`measure` does not answer every artifact.** It has commands for the def
dump and for CSVs; for a savegame, a log or an assembly it classifies the file
and hands you to the right reader, which is a different tool with its own
arguments. `measure explain <path>` tells you which. When the named tool has no
`measure` equivalent, the honest answer to "how many" is often `REFUSED` plus
the narrower question that *can* be answered — "did X occur" instead of "how
many X".

**A literal-string search is legitimate and this skill does not forbid it.** The
distinction that matters is *literal vs semantic*: "does the byte sequence
`Foo` occur in this file" is a question a scanner answers correctly. "How many
Foos are there" is not, whenever Foos are stored as indices, compressed, encoded,
or spread across records. When you genuinely want the literal form, say so and
proceed — there is an escape hatch precisely so the guard rail stays credible.

Two things a scan still cannot tell you, and `measure find` exists for both. A hit
in a 331 MB single-line file **cannot be read back to a record** — you learn that
something matched, not what. And a **miss is not an absence**: the text may be
stored escaped (`caf\u00e9` for `café`), or compressed, or in a slice the capture
never wrote. ⚠️ `find` is *not* faster than `grep` — measured 0.1 s against 0.11 s
per 96 MB. What it adds is that hits carry their record, and that a zero is
coverage-gated, so it can only say "no" about slices it actually read.

## The lesson that outranks the tooling

Twice in one day, on the work that produced this skill, the same mistake:

> **Agreement between sources is not correctness when the sources share a failure
> mode.** A count was cross-checked against three independent-looking places — a
> manifest, a per-file total, and the parsed records. All three said `0`. All
> three were wrong *together*, because one collision had corrupted all three
> identically. The check passed and the answer was garbage.

Its twin, from the same afternoon:

> **A guard that looks like a gap is usually a guard.** Nineteen entries were
> reported as an unrecorded hole in an artifact. They were stale leftovers, and
> another tool had been deliberately skipping them for over a week, with a
> comment explaining exactly why. The "bug" was in the new tool, which had
> ignored the older tool's rule.

So: before reporting a finding against something that already exists, check
whether it already handles the case and says why. And when you validate an
instrument, **validate it against a case whose answer you already know
independently** — not against another reading of the same artifact.

### 🔴 Calibrating on the right answer is not enough — measured 2026-08-26

Five instruments were built in one session, **every one calibrated against an answer
already known**, and **every one was wrong the first time** in a way that produced a
clean, plausible number. Not one was caught by reading its output.

🔑 **The failures were never in the core logic. They were all in the cases the author
did not think to include:**

| instrument | calibrated on | broke on |
|---|---|---|
| a 14-check criteria grader | the shipped, correct implementation — scored 14/14 | a *regression* that kept every structure it checked and destroyed the tuned constants it did not |
| a reply classifier | a hit, and an abstention | a **negative verdict** ("VERDICT: no, it is absent") — a right answer, counted as a find |
| the same classifier | complete replies | a **truncated** reply, scored as "looked and found nothing" |
| a ceiling sweep | sizes with test cases | a size with **no cases buildable**, printed `0/0` — ignorance rendered as a measurement |
| an answer-shape prompt | prose shapes | a shape written `<1> \| <2>`, **echoed back literally** instead of filled in |

⇒ **Calibrate on the known POSITIVE, the known NEGATIVE, the EMPTY case and the
TRUNCATED case.** An instrument shown only the answer it was built to find has been
*run*, not tested — and the case you forgot is exactly where it will answer confidently
and wrongly. ⚠️ Note the first row especially: passing a full checklist against a
known-good input still did not detect a real loss, because **a checklist can only see
the dimensions it names**. Where a delegate or a generator WROTE something, the diff is
the instrument and the checklist is not.

### 🔴 A green suite is a hypothesis until you break the code — measured 2026-08-31

The rule above says calibrate on the negative. The next question is who calibrates
the *calibrator*. A 42-case suite guarding this package passed, and nothing in it
had ever been shown a wrong implementation — so "42/42" meant *the suite ran*.

⇒ **Mutate the code and require the named case to notice.** Nine plausible wrong
versions — the reader silently dropping a record, a literal search rebuilt on
`LIKE`, an orphan's records loaded, the manifest parsed with plain `json` — each
had to be caught by a case named in advance. `scripts/mutate_check.py`. Three
properties make it an instrument rather than a ritual: a **control run** first, so
a suite that was already red cannot read as detection; the mutation site must match
**exactly once**, because a mutation that applied nowhere passes everything; and an
undetected mutation is reported as a **gap in the suite**, not a success.

### 🔴 Two optimisations, each a win, cancelling each other — measured 2026-08-31

A windowed reader cut peak memory from 306 MB to 24 MB. A `cache_size` pragma from
a "fast SQLite inserts" article cut build time 4%. Both measured, both real, both
landed — and peak memory came out at **326 MB**, because 256 MB of page cache gave
back almost exactly what the reader had saved. Neither change was wrong. The
*pair* was, and each looked fine in isolation.

⇒ **Measure the combination, on the axis you were trying to improve.** Borrowed
tuning is tuned for someone else's workload: of the four pragmas that article
recommends, one helped, one did nothing, and two cost memory for no speed. And
when a change is meant to reduce X, the acceptance number is X after everything
else is also applied.

Worked case studies of all of these, with the real numbers:
**`references/incidents.md`**.

## Before you trust a number

A short checklist, worth running when the number is about to decide something
expensive:

1. **What did the number come from** — the artifact, or a scan of it?
2. **Could it be `0` because nothing was captured?** Ask for coverage.
3. **What is it an answer about?** Which capture, which inputs, which moment.
4. **Is the artifact current** by fingerprint, not by mtime?
5. **If two sources agree — are they actually independent?**
6. **Has this instrument been checked against a known answer?** If not, it is a
   hypothesis, not a measurement.
7. **Has it been checked against a known NEGATIVE, an EMPTY input and a TRUNCATED
   one?** The known-good case is the one it was built to pass; the others are where
   it answers confidently and wrongly.
8. **Did anything WRITE?** Then read the diff. A criteria checklist reports 100%
   while constants, weights and prose it never names are quietly destroyed.
9. **Has the CHECK been shown to fail?** A test suite is an instrument too, and
   one that has only ever seen correct code has been run, not tested. Break the
   code on purpose and require the named case to notice — see below.

## Project specifics

This skill is generic. Where it is used against a particular stack, the artifact
registry and the domain notes live in **`references/`**:

- `references/rimworld.md` — the RimWorld def dump, `.rws` savegames, `Player.log`,
  world CSVs and .NET assemblies: what each one's encoding does to a scanner, and
  which tool answers instead.

Adding a new artifact class to the registry is a small, well-defined edit —
`references/building.md` has the recipe, and doing it there means both the
instrument and any guard rail learn about it at once.
