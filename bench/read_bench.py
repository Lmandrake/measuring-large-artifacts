"""Four ways to walk a giant single-line JSON dump. Time, peak RSS, equivalence.

usage: python3 read_bench.py <variant> <path>

⚠️ ru_maxrss is BYTES on macOS and KILOBYTES on Linux. Getting that wrong is a
1024x wrong number about memory, which is on-theme.
"""
import hashlib, json, mmap, os, re, resource, sys, time

VARIANT, PATH = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else (None, None)
_RSS_UNIT = 1 if sys.platform == "darwin" else 1024


def peak_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _RSS_UNIT / 1048576


# ---------------------------------------------------------------- variants

def v_current(path):
    """Exactly what dumpdb.iter_defs + build do today: whole file as str, then
    raw_decode, then json.dumps each record back out for the `json` column."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    dec = json.JSONDecoder()
    mm = re.search(r'"defs"\s*:\s*\[', text)
    idx, n = mm.end(), len(text)
    while True:
        while idx < n and text[idx] in " \t\r\n,":
            idx += 1
        if idx >= n or text[idx] == "]":
            return
        obj, idx = dec.raw_decode(text, idx)
        yield obj, json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def v_slice(path):
    """Same read, but the `json` column is the ORIGINAL source span — raw_decode
    already hands back the end offset, so re-serialising is pure waste."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    dec = json.JSONDecoder()
    mm = re.search(r'"defs"\s*:\s*\[', text)
    idx, n = mm.end(), len(text)
    while True:
        while idx < n and text[idx] in " \t\r\n,":
            idx += 1
        if idx >= n or text[idx] == "]":
            return
        start = idx
        obj, idx = dec.raw_decode(text, idx)
        yield obj, text[start:idx]


def v_chunked(path, chunk=1 << 22):
    """Never hold the whole file: a sliding str buffer, refilled on demand."""
    dec = json.JSONDecoder()
    fh = open(path, "r", encoding="utf-8-sig")
    buf = fh.read(chunk)
    mm = re.search(r'"defs"\s*:\s*\[', buf)
    idx = mm.end()
    while True:
        while True:
            while idx < len(buf) and buf[idx] in " \t\r\n,":
                idx += 1
            if idx < len(buf) and buf[idx] == "]":
                fh.close()
                return
            if idx >= len(buf) - 1:
                more = fh.read(chunk)
                if not more:
                    fh.close()
                    return
                buf = buf[idx:] + more
                idx = 0
                continue
            break
        try:
            start = idx
            obj, idx = dec.raw_decode(buf, idx)
        except ValueError:
            more = fh.read(chunk)
            if not more:
                fh.close()
                raise
            buf = buf[start:] + more
            idx = 0
            continue
        yield obj, buf[start:idx]


def _obj_end(data, i):
    """End offset of the JSON object starting at data[i] == '{'. Byte level.

    🔴 The whole risk of this variant lives here: a brace inside a string, and
    an escaped quote inside a string, must not be counted. That is what the
    equivalence check exists to catch.
    """
    depth = 0
    n = len(data)
    while i < n:
        c = data[i]
        if c == 0x22:                      # '"'
            i += 1
            while i < n:
                c = data[i]
                if c == 0x5C:              # backslash
                    i += 2
                    continue
                if c == 0x22:
                    break
                i += 1
        elif c == 0x7B:                    # '{'
            depth += 1
        elif c == 0x7D:                    # '}'
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unterminated object")


def v_mmap(path):
    """mmap + byte-level boundary scan + json.loads(bytes). Peak memory is one
    record; the OS pages the file and never charges us for it as anonymous RSS."""
    fh = open(path, "rb")
    data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    m = re.search(rb'"defs"\s*:\s*\[', data)
    i = m.end()
    n = len(data)
    while i < n:
        while i < n and data[i] in b" \t\r\n,":
            i += 1
        if i >= n or data[i] == 0x5D:      # ']'
            break
        end = _obj_end(data, i)
        blob = data[i:end]
        yield json.loads(blob), blob
        i = end
    data.close()
    fh.close()


VARIANTS = {"current": v_current, "slice": v_slice, "chunked": v_chunked,
            "mmap": v_mmap}

# ---------------------------------------------------------------- run

def run(variant, path, **kw):
    t0 = time.perf_counter()
    h = hashlib.sha256()
    n = tags = 0
    for obj, blob in VARIANTS[variant](path, **kw):
        n += 1
        # do what build does with each record, so it is like-for-like
        h.update(obj["defName"].encode())
        h.update(str(obj["shortHash"]).encode())
        h.update(obj.get("label", "").encode())
        for t in (obj.get("fields") or {}).get("weaponTags") or ():
            tags += 1
        _ = len(blob)
    dt = time.perf_counter() - t0
    print("%-8s n=%d tags=%d  %.2fs  peak=%.0f MB  fields-sha=%s"
          % (variant + (" %dK" % (kw["chunk"] >> 10) if "chunk" in kw else ""),
             n, tags, dt, peak_mb(), h.hexdigest()[:16]))


if VARIANT is not None:
    run(VARIANT, PATH)
