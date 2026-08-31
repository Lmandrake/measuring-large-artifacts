# bench/ — evidence, not conclusions

Exploratory harness measuring where `measure build` spends its time and memory.
It exists so the performance claims in a design discussion carry a number and a
way to re-derive it, the same standard the rest of this repo holds for counts.

⚠️ **Nothing here has been run on Windows yet.** Every number below was measured
on macOS 26.6 / Python 3.12.14 / 16 cores / 128 GB, against a **synthetic 96.5 MB**
file shaped like `defs/ThingDef.json` (112,000 records, non-ASCII labels present).
The real file is 331 MB and the whole dump is 646 MB, so anything said about those
is **extrapolation, clearly labelled as such.**

```bash
python3 bench/gen.py big.json 96          # synthetic dump, ~96 MB
python3 bench/gen.py small_ascii.json 96 --ascii
python3 bench/read_bench.py current big.json     # also: slice chunked mmap
python3 bench/sql_bench.py big.json
python3 bench/par_bench.py big.json
```

## Reading the file — `read_bench.py`

| variant | what it does | time | peak RSS |
|---|---|---|---|
| `current` | today's `iter_defs`: whole file as one `str`, then `json.dumps` each record back out | 1.11 s | 306 MB |
| `slice` | same read, but the `json` column is the **original source span** `raw_decode` already delimited | 0.53 s | 306 MB |
| `chunked` | sliding `str` window (256 KiB), refilled on demand | 0.57 s | **24 MB** |
| `mmap` | mmap + pure-Python brace scanner + `json.loads(bytes)` | 3.47 s | 113 MB |

All four extract byte-identical fields (`fields-sha` matches), which is the only
reason the times are comparable.

Two things worth keeping:

- **`json.dumps` per record is half the read cost.** `raw_decode` returns the end
  offset, so the source text is already delimited and re-serialising it is pure
  waste.
- **The whole-file read costs ~3.2x the file size in RAM**, because at peak the raw
  bytes and the decoded `str` are both live and a single non-ASCII character
  widens the `str` to 2 bytes/char. Measured: 306 MB peak for the 96 MB file with
  non-ASCII labels, 209 MB for the same-size ASCII file. ⇒ *extrapolated* ~1.05 GB
  peak on the real 331 MB `ThingDef.json`.
- The sliding window's size is the memory dial: 17 MB @ 16 KiB, 24 MB @ 256 KiB,
  **219 MB @ 4 MiB**. That last jump is superlinear and unexplained, which is its
  own argument for keeping the window small.

## Building the database — `sql_bench.py`

96.5 MB source → 112,000 `defs` rows, 560,000 `def_flags`, 224,082 `def_tags`.

| variant | insert | index | total | db size | one-record read |
|---|---|---|---|---|---|
| `now` — today's schema, 8 indexes created **before** the load | 1.97 s | — | 1.97 s | 180.6 MB | 0.012 ms |
| `after` — same indexes, created **after** | 1.21 s | 0.54 s | 1.74 s | 175.8 MB | 0.015 ms |
| `after+prag` — plus `page_size`/`cache_size`/`temp_store`/`locking_mode` | 1.09 s | 0.53 s | **1.62 s** | 175.5 MB | 0.016 ms |
| `zlib` — `json` column compressed (level 1) | 2.56 s | 0.50 s | 3.06 s | **124.4 MB** | 0.020 ms |
| `nojson` — no blob, just `(src_file, src_off, src_len)` | 1.09 s | 0.45 s | 1.54 s | **77.2 MB** | 0.035 ms |

🔴 **The `nojson` variant took two attempts to get right, and both bugs were the
same one:** a source offset is in **bytes** and `len(str)` is in **characters**.
The first cut stored the character length, and the 200th record read back
`Unterminated string` — an offset index silently addresses the wrong bytes the
moment the dump stops being ASCII. It failed loudly here; in a design where the
blob is a fallback it would fail quietly.

## Sharding one giant file — `par_bench.py`

Seek to N equally spaced byte offsets, `bytes.find(b'},{')` forward to a record
boundary, validate the boundary by decoding the record that starts there, then
parse each range in its own process.

| procs | split | parse | total | speedup |
|---|---|---|---|---|
| 1 (serial) | — | 0.56 s | 0.56 s | 1.00x |
| 2 | 0.01 s | 0.28 s | 0.30 s | 1.88x |
| 4 | 0.01 s | 0.16 s | 0.17 s | 3.28x |
| 8 | 0.00 s | 0.11 s | 0.12 s | **4.76x** |
| 16 | 0.00 s | 0.14 s | 0.14 s | 4.09x |

Finding the boundaries is free; the parse is what parallelises, and it plateaus
around 8.

🔴 **The first cut of the splitter reported OK on a split that never happened.**
`bytes.find(b'},{')` returns the index of `}`, so `hit + 1` is the comma, not the
record — every candidate boundary failed validation, `boundaries()` returned
`[start, end]`, and the run parsed the file in **one** shard. The shard counts
then matched the serial count *perfectly*, and the harness printed a speedup
number for parallelism that did not exist.

⇒ A test that compares only the ANSWER cannot see that the MECHANISM was never
engaged. `boundaries()` now asserts it produced the shard count it was asked for.
This is the same shape as every incident in `references/incidents.md`, met while
building the instrument rather than while using it.

## What is NOT measured here

- Anything on Windows: native Python vs WSL, `/mnt/c` vs ext4, Defender.
- The real 646 MB dump end to end.
- Peak RSS on Windows — `resource.getrusage` does not exist there, and
  `ru_maxrss` is **bytes on macOS but kilobytes on Linux**, which is a 1024x
  wrong number waiting to be quoted.
