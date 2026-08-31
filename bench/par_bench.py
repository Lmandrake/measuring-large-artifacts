"""Can one giant defs file be split across processes, and what does it buy?

Splitting strategy: seek to N equally spaced byte offsets, then `bytes.find(b'},{')`
forward — a C-speed scan — to land on a record boundary. Each worker then walks
its own byte range with the ordinary decoder.

🔴 THE RISK: `},{` can occur INSIDE A STRING (a description containing `},{`) and
inside a nested object. A boundary landed on wrongly does not raise — it silently
starts mid-record, and the worker's count is short. So every boundary is VALIDATED
by decoding the record that starts there, and the shard counts must sum to the
single-process count. That equality is the whole test.
"""
import json, mmap, multiprocessing as mp, os, re, sys, time

PATH = sys.argv[1] if len(sys.argv) > 1 else "big.json"
NPROC = int(sys.argv[2]) if len(sys.argv) > 2 else 8


def boundaries(path, n):
    fh = open(path, "rb")
    data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    start = re.search(rb'"defs"\s*:\s*\[', data).end()
    end = data.rfind(b"]", len(data) - 200)
    out = [start]
    span = (end - start) // n
    dec = json.JSONDecoder()
    for k in range(1, n):
        probe = start + span * k
        while True:
            hit = data.find(b"},{", probe)
            if hit < 0 or hit >= end:
                break
            cand = hit + 2      # hit points at '}'; the record starts after '},'
            # validate: a real boundary decodes to a whole record with a defName
            try:
                obj, _ = dec.raw_decode(data[cand:cand + 20000].decode("utf-8",
                                                                      "strict"))
                if isinstance(obj, dict) and "defName" in obj:
                    out.append(cand)
                    break
            except Exception:
                pass
            probe = hit + 3
    out.append(end)
    # 🔴 ASSERT THE MECHANISM ENGAGED. The first cut used hit+1 (the comma), so
    # every candidate failed validation, `out` came back as [start, end] — ONE
    # shard — and the shard counts matched the serial count PERFECTLY. A test
    # that only compares counts reports OK on a split that never happened.
    if len(out) - 1 != n:
        raise SystemExit("split produced %d shards, not %d" % (len(out) - 1, n))
    data.close()
    fh.close()
    return out


def shard(args):
    path, lo, hi = args
    fh = open(path, "rb")
    data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    text = data[lo:hi].decode("utf-8")
    data.close()
    fh.close()
    dec = json.JSONDecoder()
    idx, n, cnt, tags = 0, len(text), 0, 0
    while True:
        while idx < n and text[idx] in " \t\r\n,":
            idx += 1
        if idx >= n or text[idx] == "]":
            return cnt, tags
        obj, idx = dec.raw_decode(text, idx)
        cnt += 1
        tags += len((obj.get("fields") or {}).get("weaponTags") or ())


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.argv = ["x"]
    import read_bench as rb

    t = time.perf_counter()
    n1 = t1 = 0
    for obj, blob in rb.v_chunked(PATH, chunk=1 << 18):
        n1 += 1
        t1 += len((obj.get("fields") or {}).get("weaponTags") or ())
    serial = time.perf_counter() - t
    print("serial      n=%d tags=%d  %.2fs" % (n1, t1, serial))

    for np_ in (2, 4, 8, 16):
        t = time.perf_counter()
        b = boundaries(PATH, np_)
        tb = time.perf_counter() - t
        jobs = [(PATH, b[i], b[i + 1]) for i in range(len(b) - 1)]
        t = time.perf_counter()
        with mp.Pool(np_) as pool:
            res = pool.map(shard, jobs)
        par = time.perf_counter() - t
        n2 = sum(r[0] for r in res)
        t2 = sum(r[1] for r in res)
        ok = "OK " if (n2, t2) == (n1, t1) else "MISMATCH n=%d tags=%d" % (n2, t2)
        print("procs=%-3d   %s  split=%.2fs parse=%.2fs total=%.2fs  speedup=%.2fx"
              % (np_, ok, tb, par, tb + par, serial / (tb + par)))
