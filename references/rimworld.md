# RimWorld artifacts

The five large artifacts in a RimWorld modding stack, what each one's encoding
does to a scanner, and what answers instead. The machine-readable form of this
table is `scripts/measure/artifacts.py`; this file is the prose behind it.

## The def dump — `DefDump/`

**646 MB**, 536 files. `defs/ThingDef.json` alone is **331 MB** — roughly 82
million tokens. Nobody reads it, which is exactly why everyone scans it.

Each file is `{"defType":X,"defs":[…],"count":N}` on a **single line**, so a
line count is always 1 and a `grep` hit is a fragment of a record rather than a
record. Reading it means streaming the array element by element
(`json.JSONDecoder.raw_decode`), never `json.load`.

⚠️ **`manifest.json` is the exception and is deliberately scannable** — 144 KB
of pretty-printed JSON. Grepping it is legitimate. Its trap is the opposite one:
`json.load` silently drops its duplicate `defCounts` keys. Use
`measure.dumpdb.read_manifest()`.

Two things the dump can never tell you:
- **which file or xpath a def came from.** `Def` has no `fileName` field once
  loaded; the finest provenance available is which *mod* won.
- **anything about right now.** It is a photograph of one load.

What it uniquely can: post-patch values, the mod that actually won an override,
true `shortHash`es, and the engine's own computed classification (`IsWeapon` is
C# logic, not an XML field) — none of which an offline scan of `Defs/` can reach.

⇒ `measure count <DefType>` · `measure coverage` · `measure get <defName>`

## Savegames — `*.rws`

Plain XML wrapping **base64 / raw-DEFLATE grids of 2-byte def shortHashes**.
Biomes, terrain and things are *indices into a compressed grid*, so a defName
appears once or twice in a lookup table regardless of how many tiles carry it.

Ludeon's format — no redesign is possible or planned, so the answer here is a
correct reader plus a guard rail, permanently.

✅ A literal scan is legitimate and idiomatic: `grep '<def>NAME</def>' save.rws`
answers "does this occur". ⛔ Any *count* of grid-borne content does not.

Two error strings that mean different things and are constantly confused:
- `Could not resolve cross-reference` — the def loader, about the live mod set.
- `Could not load reference to` — Scribe: the **save** holds a dead name, and no
  mod change fixes it.

⇒ `rimbench/savemap.py`, or the live bridge.

## Assemblies — `*.dll`

.NET metadata keeps attribute strings, method bodies and custom-attribute blobs
outside the UTF-16 literal pool. `strings -a -el` on the companion DLL found
**16 of 115** tool names.

`strings` proves presence, never absence. For our own assemblies, read the `.cs`
— it is on disk. For third-party ones, use a metadata reader (there is a real
PE/CLI walker at `src/RimMandrake/Utils/ilprobe/`) or the running system's own
introspection.

## `Player.log`

Multi-line stack traces and long runs of identical lines. **`grep -c` counts
LINES** — one exception spanning 30 lines is not 30 errors, and a repeated
message is not a repeated failure.

✅ Grepping for a literal error string to see **whether** it occurred is the
correct use. ⛔ Counting occurrences to judge severity is not.

⇒ `src/RimMandrake/Utils/harvest_log.py`

## World CSVs — `world/*.csv`

~20k rows with a header. A `grep -c` counts the header row and any value that
merely *contains* the token in an unrelated column.

Small enough for a script to read whole, never for an agent to read whole.

⇒ `measure csv <path> --where col=value` — excludes the header and matches on a
named column, and reports the total row count as evidence.

## Not registered, and deliberately

`ModsConfig.xml` and mod `About.xml` files are small, line-structured and meant
to be read. Scanning them is correct. Adding them to the registry would be an
unearned refusal, which is how a guard rail loses its credibility.
