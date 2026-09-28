#!/usr/bin/env python3
"""
fix_timestamp_jump_v2.py  --  TEMPORARY workaround, not a root-cause fix.

Handles FORWARD and BACKWARD log_time discontinuities (clock steps).

How it works
    1. Splits the recording into "clusters" of time separated by gaps larger
       than the threshold (in the merged, time-sorted stream).
    2. Works out the TRUE order of the clusters from the order in which they
       appear in the file (file order = recording order).
    3. For each boundary between consecutive clusters, estimates the shift that
       makes the timeline continuous (last sample + nominal dt == first sample
       of the next cluster), using every channel that spans the boundary.
    4. Writes a NEW mcap with the shifts applied. The original is never modified.

What it changes
    * Message log_time (and publish_time, when on the same clock as log_time)
    * Attachment log_time / create_time (same rule)
    It does NOT touch message payloads (header.stamp etc.).

Usage
    python3 fix_timestamp_jump_v2.py input.mcap                  # analyse only
    python3 fix_timestamp_jump_v2.py input.mcap -o fixed.mcap    # write repaired copy
    python3 fix_timestamp_jump_v2.py input.mcap -o fixed.mcap --anchor last

    --anchor first : keep the FIRST cluster's clock, shift later data (default)
    --anchor last  : keep the LAST cluster's clock, shift earlier data

Assumptions (check the report before trusting the output)
    * The clock step is seen by ALL channels at once.
    * No frames were lost across the step (boundary interval ~= normal interval).
"""
import argparse
import bisect
import statistics
import sys
from collections import defaultdict

from mcap.reader import make_reader
from mcap.writer import CompressionType, Writer

NS = 1_000_000_000


def fmt_ns(ns):
    s = ns / NS
    h, rem = divmod(abs(s), 3600)
    m, sec = divmod(rem, 60)
    sign = "-" if s < 0 else ""
    return f"{sign}{int(h)}h{int(m):02d}m{sec:06.3f}s ({ns} ns)"


# --------------------------------------------------------------------------- #
# Pass 1: scan
# --------------------------------------------------------------------------- #
def scan(path):
    per_ch = defaultdict(list)  # channel_id -> [log_time ...] in file order
    info = {}
    seq = []                    # all log_times in file order
    with open(path, "rb") as f:
        reader = make_reader(f)
        for schema, channel, msg in reader.iter_messages(log_time_order=False):
            per_ch[channel.id].append(msg.log_time)
            seq.append(msg.log_time)
            if channel.id not in info:
                info[channel.id] = (
                    channel.topic,
                    schema.name if schema else "<no schema>",
                    channel.message_encoding,
                )
            if len(seq) % 200_000 == 0:
                print(f"  scanned {len(seq)} messages...", file=sys.stderr)
    return per_ch, info, seq


def find_clusters(seq, thr_ns):
    """Return (cuts, gaps). cuts = sorted midpoints; cluster(t) = bisect_right(cuts, t)."""
    all_t = sorted(seq)
    cuts, gaps = [], []
    for a, b in zip(all_t, all_t[1:]):
        if b - a > thr_ns:
            cuts.append((a + b) // 2)
            gaps.append((a, b))
    return cuts, gaps


def cluster_order(seq, cuts):
    """Cluster ids in recording (file) order, by first appearance."""
    order, seen = [], set()
    total = len(cuts) + 1
    for t in seq:
        c = bisect.bisect_right(cuts, t)
        if c not in seen:
            seen.add(c)
            order.append(c)
            if len(seen) == total:
                break
    return order


def split_by_cluster(per_ch, cuts):
    out = {}
    for cid, ts in per_ch.items():
        d = defaultdict(list)
        for t in ts:
            d[bisect.bisect_right(cuts, t)].append(t)
        out[cid] = d
    return out


def estimate_boundary(split, info, prev_c, next_c):
    """Offset to ADD to next_c so it continues right after prev_c, per channel."""
    rows = []
    for cid, d in split.items():
        p, q = d.get(prev_c), d.get(next_c)
        if not p or not q or len(p) < 6:
            continue
        tail = p[-201:]
        dts = [y - x for x, y in zip(tail, tail[1:]) if y > x]
        if len(dts) < 5:
            continue
        dt = statistics.median(dts)
        last, first = max(p), min(q)
        rows.append({
            "cid": cid,
            "topic": info[cid][0],
            "dt": dt,
            "raw": first - last,                   # raw boundary interval
            "add": int(round(last + dt - first)),  # shift for next_c
        })
    return rows


# --------------------------------------------------------------------------- #
# Pass 2: write
# --------------------------------------------------------------------------- #
def detect_compression(reader):
    summary = reader.get_summary()
    if summary and summary.chunk_indexes:
        return {
            "zstd": CompressionType.ZSTD,
            "lz4": CompressionType.LZ4,
            "": CompressionType.NONE,
        }.get(summary.chunk_indexes[0].compression, CompressionType.ZSTD)
    return CompressionType.ZSTD


def write_fixed(src, dst, cuts, shifts, thr_ns, marker, anchor):
    """shifts: dict cluster_id -> ns to ADD."""
    schema_map, channel_map = {}, {}
    n = pub_untouched = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        reader = make_reader(fin)
        header = reader.get_header()
        writer = Writer(fout, compression=detect_compression(reader))
        writer.start(profile=header.profile, library=header.library)

        # file order = recording order, which stays correct after shifting
        for schema, channel, msg in reader.iter_messages(log_time_order=False):
            if schema is None:
                sid = 0
            else:
                if schema.id not in schema_map:
                    schema_map[schema.id] = writer.register_schema(
                        name=schema.name, encoding=schema.encoding, data=schema.data)
                sid = schema_map[schema.id]
            if channel.id not in channel_map:
                channel_map[channel.id] = writer.register_channel(
                    topic=channel.topic,
                    message_encoding=channel.message_encoding,
                    schema_id=sid,
                    metadata=dict(channel.metadata or {}))

            add = shifts[bisect.bisect_right(cuts, msg.log_time)]
            pub = msg.publish_time
            if pub and abs(pub - msg.log_time) <= thr_ns:
                pub += add
            elif pub:
                pub_untouched += 1
            writer.add_message(channel_map[channel.id], msg.log_time + add,
                               msg.data, pub, msg.sequence)
            n += 1
            if n % 200_000 == 0:
                print(f"  wrote {n} messages...", file=sys.stderr)

        for att in reader.iter_attachments():
            add = shifts[bisect.bisect_right(cuts, att.log_time)]
            ct = att.create_time
            if ct and abs(ct - att.log_time) <= thr_ns:
                ct += add
            writer.add_attachment(create_time=ct, log_time=att.log_time + add,
                                  name=att.name, media_type=att.media_type,
                                  data=att.data)
        for md in reader.iter_metadata():
            writer.add_metadata(md.name, dict(md.metadata))

        if marker:
            writer.add_metadata("timestamp_jump_fix", {
                "tool": "fix_timestamp_jump_v2.py (temporary workaround)",
                "source_file": src,
                "anchor": anchor,
                "boundaries_repaired": str(len(cuts)),
                "shifts_ns_by_cluster": ",".join(f"{c}:{shifts[c]}" for c in sorted(shifts)),
                "payload_timestamps_modified": "false",
            })
        writer.finish()
    return n, pub_untouched


def verify(dst, thr_ns, expected_count):
    per_ch, _, seq = scan(dst)
    cuts, _ = find_clusters(seq, thr_ns)
    backward = False
    run_max = None
    for t in seq:
        if run_max is not None and t < run_max - thr_ns:
            backward = True
            break
        if run_max is None or t > run_max:
            run_max = t
    n = len(seq)
    ok = (n == expected_count) and not cuts and not backward
    print("\n== Verification of output ==")
    print(f"  messages: {n} (input had {expected_count})  -> {'OK' if n == expected_count else 'MISMATCH'}")
    print(f"  remaining gaps > threshold: {len(cuts)}   backward jumps: {'YES' if backward else 'none'}")
    if seq:
        print(f"  duration: {fmt_ns(max(seq) - min(seq))}")
    print("  RESULT:", "PASS" if ok else "CHECK OUTPUT")
    return ok


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Temporary MCAP timestamp-jump repair tool (v2)")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", help="write repaired copy here (omit = analyse only)")
    ap.add_argument("--gap-threshold-s", type=float, default=10.0,
                    help="merged-stream gap treated as a jump (default 10 s)")
    ap.add_argument("--anchor", choices=["first", "last"], default="first",
                    help="first: keep first cluster's clock (default). last: keep last cluster's clock")
    ap.add_argument("--offset-ns", type=int,
                    help="force this shift (ns, ADDED to the later cluster) for a single boundary")
    ap.add_argument("--no-marker", action="store_true",
                    help="do not add a 'timestamp_jump_fix' metadata record to the output")
    args = ap.parse_args()
    thr_ns = int(args.gap_threshold_s * NS)

    print(f"Scanning {args.input} ...")
    per_ch, info, seq = scan(args.input)
    n = len(seq)
    if n == 0:
        sys.exit("No messages found.")

    print(f"\n== Channels ({n} messages) ==")
    for cid, (topic, sname, enc) in sorted(info.items()):
        print(f"  [{cid}] {topic:40s} {len(per_ch[cid]):8d} msgs  schema={sname}  enc={enc}")

    cuts, gaps = find_clusters(seq, thr_ns)
    print(f"\nRaw range: {min(seq)} -> {max(seq)}  ({fmt_ns(max(seq) - min(seq))} on raw clock)")
    if not cuts:
        print(f"No gap larger than {args.gap_threshold_s}s found. Nothing to fix.")
        return

    order = cluster_order(seq, cuts)
    split = split_by_cluster(per_ch, cuts)
    counts = defaultdict(int)
    lo, hi = {}, {}
    for t in seq:
        c = bisect.bisect_right(cuts, t)
        counts[c] += 1
        lo[c] = min(lo.get(c, t), t)
        hi[c] = max(hi.get(c, t), t)

    print(f"\n== Clusters in recording order ({len(order)}) ==")
    for pos, c in enumerate(order, 1):
        print(f"  #{pos}: {counts[c]:8d} msgs  raw {lo[c]} -> {hi[c]}  ({fmt_ns(hi[c] - lo[c])})")

    if args.offset_ns is not None and len(order) != 2:
        sys.exit(f"--offset-ns only valid with exactly 2 clusters, found {len(order)}.")

    adds = []  # shift to add to order[i+1], relative to order[i]
    for i in range(len(order) - 1):
        pc, nc = order[i], order[i + 1]
        direction = "BACKWARD" if lo[nc] < hi[pc] else "FORWARD"
        print(f"\n== Boundary {i + 1}: cluster #{i + 1} -> #{i + 2} ({direction} jump) ==")
        rows = estimate_boundary(split, info, pc, nc)
        if not rows and args.offset_ns is None:
            sys.exit("Could not estimate offset (not enough samples on the pre-jump side). Use --offset-ns.")
        ref = min(rows, key=lambda r: r["dt"]) if rows else None
        add = args.offset_ns if args.offset_ns is not None else ref["add"]
        for r in sorted(rows, key=lambda r: r["dt"]):
            resid = r["raw"] + add
            tag = "  <- reference" if ref and r is ref else ""
            flag = "" if 0.3 * r["dt"] <= resid <= 3 * r["dt"] else "   !! check"
            print(f"  {r['topic']:40s} nominal dt={r['dt']/1e6:9.3f} ms | "
                  f"boundary after fix={resid/1e6:9.3f} ms{flag}{tag}")
        if rows:
            spread = (max(r["add"] for r in rows) - min(r["add"] for r in rows)) / 1e6
            print(f"  offset estimate spread across channels: {spread:.3f} ms")
        print(f"  => shift applied to later cluster: {fmt_ns(add)}")
        adds.append(add)

    # cumulative shift per cluster (anchor first)
    shifts = {order[0]: 0}
    for i, add in enumerate(adds):
        shifts[order[i + 1]] = shifts[order[i]] + add
    if args.anchor == "last":
        final = shifts[order[-1]]
        shifts = {c: s - final for c, s in shifts.items()}

    if not args.output:
        print("\nAnalysis only (no -o given). Re-run with -o <file> to write a repaired copy.")
        return

    print(f"\nWriting {args.output} (anchor={args.anchor}) ...")
    written, pub_left = write_fixed(args.input, args.output, cuts, shifts,
                                    thr_ns, not args.no_marker, args.anchor)
    if pub_left:
        print(f"  note: {pub_left} messages had a publish_time on a different clock; left unchanged")
    verify(args.output, thr_ns, n)
    print("\nReminder: temporary workaround. Payload timestamps are untouched; "
          "flag this file as repaired in delivery/QC notes.")


if __name__ == "__main__":
    main()