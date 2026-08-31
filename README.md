# measuring-large-artifacts

A Claude skill for answering questions about files too large to read — dumps,
savegames, logs, database exports, packed binaries, giant CSV/JSON — **without
returning a confident wrong number**.

It exists because seven measuring instruments were caught lying in a single
session. None errored; every one returned a plausibly-shaped integer, and the
integers decided expensive things. `references/incidents.md` has the case files.

## What is here

```
SKILL.md                   the skill itself — start here
bin/measure                CLI shim
scripts/measure/           the implementation (stdlib only; sqlite3)
scripts/selftest_measure.py
scripts/mutate_check.py    breaks the code 9 ways; the suite must notice
bench/                     where build time and memory actually go, measured
references/
  api.md                   commands, exit codes, Python API
  building.md              how to DESIGN an artifact that cannot lose evidence
  incidents.md             the wrong numbers, as case studies
  rimworld.md              domain notes for the RimWorld modding stack
```

## Install

```bash
mkdir -p ~/.claude/skills
ln -s /mnt/d/Luke/dev/measuring-large-artifacts ~/.claude/skills/measuring-large-artifacts
```

Optionally put `bin/` on `PATH` so `measure …` works bare.

## Verify

```bash
python3 scripts/selftest_measure.py     # 48 cases
python3 scripts/mutate_check.py         # does the suite actually fail when it should?
```

Synthetic cases run anywhere with no real capture present; live cases run against
a real built artifact if one exists and **skip with a stated reason** if not — a
skip is never reported as a pass.

The second command is the one that makes the first mean anything. A suite that has
only ever been shown correct code has been *run*, not tested — so `mutate_check`
introduces nine plausible wrong versions of the code and requires the named case
to catch each. It runs a control first, and reports an undetected mutation as a
**gap in the suite** rather than a success.

## Design in one line

Every answer is `MEASURED` / `UNMEASURED` / `REFUSED`, one line, carrying what it
was measured against — so `0` can only ever mean measured zero.
