#!/usr/bin/env python3
# =============================================================================
# convert.py — Layer 2: ROOT -> flat CSV
# =============================================================================
# Two modes:
#
#   2a QUICK (no pre-scan) — the events.root flatten_events behavior:
#      scan + flatten in one shot, all-default policies. Hard structs
#      (jagged-deep, unreadable) are dropped from the CSV (jagged-deep
#      still lands in the parquet).
#
#        python3 convert.py somefile.root
#        python3 convert.py somefile.root --tree reco --name mySample \
#                --tag is_signal=1 --fill nan
#
#   2b FROM-SCAN (manifest-driven) — consume a scan.py output directory,
#      honoring any per-branch policy edits in <name>_manifest.json.
#      Never re-touches the ROOT file (reads canonical parquet).
#
#        python3 convert.py --from-scan ./mySample_scan
#
# Jagged policies (edit "policy" per branch in manifest.json):
#   pad_max    explode to max_len columns, pad short events   (default)
#   first:N    keep only the first N columns
#   drop       exclude branch from CSV entirely
#
# --fill x    pad sentinel (default 'x', the events.root convention)
# --fill nan  pad with NaN instead — keeps jagged columns numeric dtype
# =============================================================================

import argparse
import os
import sys
import time
from datetime import datetime

import awkward as ak
import numpy as np
import pandas as pd

from common import (TOOL_VERSION, progress, progress_end, info, parse_tags,
                    resolve_fill, safe_scalar_column, read_manifest_json,
                    synopsis_convert, fill_repr)


# ======================================================================
#   Policy resolution
# ======================================================================

def resolve_policy(rec, bname):
    """Return ('pad', n_cols) | ('drop', 0) for a jagged branch."""
    pol = rec.get("policy", "pad_max")
    max_len = rec["max_len"]
    if pol == "pad_max":
        return "pad", max_len
    if pol == "drop":
        return "drop", 0
    if pol.startswith("first:"):
        try:
            n = int(pol.split(":", 1)[1])
            return "pad", max(0, min(n, max_len))
        except ValueError:
            pass
    info(f"  [WARN] unknown policy '{pol}' on {bname} — falling back to pad_max")
    return "pad", max_len


# ======================================================================
#   Pass 2 — flatten with progress   (transplanted, policy-aware)
# ======================================================================

def pass2_flatten(get_arr, records, tags, n, fill):
    to_process = [b for b in sorted(records)
                  if records[b]["category"] in ("scalar", "vec1", "jagged")]
    total = len(to_process)
    flat, dropped = {}, []

    for k, v in tags.items():
        flat[k] = np.full(n, v, dtype=np.int8)

    for i, bname in enumerate(to_process, start=1):
        rec, cat = records[bname], records[bname]["category"]
        detail = f"{cat:<7s} {bname}"
        if cat == "jagged":
            detail += f" x{rec['max_len']}"
        progress("pass 2", i, total, detail=detail)

        if cat == "scalar":
            flat[bname] = safe_scalar_column(get_arr(bname))

        elif cat == "vec1":
            flat[bname] = safe_scalar_column(get_arr(bname)[:, 0])

        elif cat == "jagged":
            action, n_cols = resolve_policy(rec, bname)
            if action == "drop" or n_cols == 0:
                dropped.append(bname)
                continue
            arr_list = ak.to_list(get_arr(bname))
            cols = [[] for _ in range(n_cols)]
            for ev in arr_list:
                ev_len = len(ev)
                for j in range(n_cols):
                    cols[j].append(ev[j] if j < ev_len else fill)
            for j in range(n_cols):
                flat[f"{bname}_{j}"] = cols[j]

    progress_end()
    return flat, dropped


# ======================================================================
#   Build report (from-scan mode)
# ======================================================================

def write_build_report(path, name, scan_dir, csv_path, df, dropped,
                       fill, seconds, tags):
    with open(path, "w") as f:
        f.write("=" * 78 + "\n")
        f.write(f"  root2csv Build Report  (v{TOOL_VERSION}, from-scan)\n")
        f.write(f"  Sample:    {name}\n")
        f.write(f"  Scan dir:  {scan_dir}\n")
        f.write(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 78 + "\n\n")
        f.write(f"  CSV:        {csv_path}\n")
        f.write(f"  Shape:      {len(df.columns)} cols x {len(df)} rows\n")
        f.write(f"  Size:       {os.path.getsize(csv_path)/1e6:.1f} MB\n")
        f.write(f"  Fill:       {fill_repr(fill)}\n")
        f.write(f"  Tags:       {list(tags.keys())}\n")
        f.write(f"  Build time: {seconds:.1f} s\n\n")
        f.write(f"  Policy-dropped jagged branches ({len(dropped)}):\n")
        for b in dropped:
            f.write(f"    [dropped]  {b}\n")
        if not dropped:
            f.write("    (none)\n")


# ======================================================================
#   Modes
# ======================================================================

def mode_quick(args):
    """2a — no pre-scan: scan + flatten in one shot, default policies."""
    from scan import run_scan
    fill = resolve_fill(args.fill)
    tags = parse_tags(args.tag)

    res = run_scan(args.input, args.tree, args.name, args.out, tags,
                   args.note, write_pq=not args.no_parquet)
    records, cache, n = res["records"], res["cache"], res["n"]

    info("\nPass 2: flattening + writing CSV ...")
    t0 = time.time()
    flat, dropped = pass2_flatten(lambda b: cache[b], records, tags, n, fill)
    df = pd.DataFrame(flat)
    csv_path = os.path.join(res["out_dir"], f"{res['name']}_flat.csv")
    try:
        df.to_csv(csv_path, index=False)
    except Exception as e:
        info(f"  [ERROR] CSV write failed: {e}")
        if res["parquet_path"]:
            info("  Canonical parquet was saved; recover via ak.from_parquet / pandas.")
        raise
    secs = time.time() - t0
    info(f"  CSV: {csv_path}")
    info(f"       {len(df.columns)} cols  {len(df)} rows  "
         f"{os.path.getsize(csv_path)/1e6:.1f} MB  ({secs:.1f}s)")
    if dropped:
        info(f"  Policy-dropped jagged branches: {len(dropped)}")

    synopsis_convert(res["name"], len(df), len(df.columns), csv_path, fill, len(dropped))

    total = res["pass1_seconds"] + secs
    info(f"\n{'='*70}")
    info(f"  DONE  {res['name']}  total={total:.1f}s  ({total/60:.1f} min)")
    info(f"  Manifest: {res['txt_path']}")
    info(f"{'='*70}\n")


def mode_from_scan(args):
    """2b — manifest-driven: honor policy edits, never re-touch ROOT."""
    scan_dir = args.from_scan
    jsons = [p for p in os.listdir(scan_dir) if p.endswith("_manifest.json")]
    if len(jsons) != 1:
        sys.exit(f"[ERROR] Expected exactly one *_manifest.json in {scan_dir}, "
                 f"found {len(jsons)}: {jsons}")
    man = read_manifest_json(os.path.join(scan_dir, jsons[0]))
    meta, records = man["meta"], man["branches"]
    name = args.name or meta["name"]
    n    = meta["n_events"]
    fill = resolve_fill(args.fill)
    tags = {**man.get("tags", {}), **parse_tags(args.tag)}

    pq_path = meta.get("canonical_parquet")
    if not pq_path or not os.path.isfile(pq_path):
        cand = os.path.join(scan_dir, f"{meta['name']}_canonical.parquet")
        if os.path.isfile(cand):
            pq_path = cand
        else:
            sys.exit(f"[ERROR] Canonical parquet not found ({pq_path}). "
                     f"Re-run scan.py without --no-parquet, or use quick mode "
                     f"on the original ROOT file.")

    info(f"\n{'='*70}")
    info(f"  root2csv CONVERT  (Layer 2, from-scan, v{TOOL_VERSION})")
    info(f"{'='*70}")
    info(f"  Name:     {name}")
    info(f"  Scan dir: {scan_dir}")
    info(f"  Parquet:  {pq_path}")
    info(f"  Events:   {n}")
    info(f"  Fill:     {fill_repr(fill)}\n")

    info("Loading canonical parquet ...")
    t0 = time.time()
    table = ak.from_parquet(pq_path)
    info(f"  loaded in {time.time()-t0:.1f}s  fields={len(table.fields)}")

    out_dir = args.out or scan_dir
    os.makedirs(out_dir, exist_ok=True)

    info("\nPass 2: flattening + writing CSV (policies from manifest.json) ...")
    t0 = time.time()
    flat, dropped = pass2_flatten(lambda b: table[b], records, tags, n, fill)
    df = pd.DataFrame(flat)
    csv_path = os.path.join(out_dir, f"{name}_flat.csv")
    df.to_csv(csv_path, index=False)
    secs = time.time() - t0
    info(f"  CSV: {csv_path}")
    info(f"       {len(df.columns)} cols  {len(df)} rows  "
         f"{os.path.getsize(csv_path)/1e6:.1f} MB  ({secs:.1f}s)")

    synopsis_convert(name, len(df), len(df.columns), csv_path, fill, len(dropped))

    report_path = os.path.join(out_dir, f"{name}_build.txt")
    write_build_report(report_path, name, scan_dir, csv_path, df, dropped,
                       fill, secs, tags)
    info(f"  Build report: {report_path}")

    info(f"\n{'='*70}")
    info(f"  DONE  {name}  build={secs:.1f}s   dropped={len(dropped)}")
    info(f"{'='*70}\n")


# ======================================================================
#   Main
# ======================================================================

def main():
    ap = argparse.ArgumentParser(
        description="Layer 2: ROOT -> flat CSV. Quick mode (positional input) "
                    "or --from-scan SCANDIR (manifest-driven).")
    ap.add_argument("input", nargs="?", default=None,
                    help="QUICK mode: .root file or directory (largest wins).")
    ap.add_argument("--from-scan", default=None, metavar="SCANDIR",
                    help="FROM-SCAN mode: scan.py output directory.")
    ap.add_argument("--tree", default=None, help="(quick) TTree name; default auto.")
    ap.add_argument("--name", default=None, help="Output prefix.")
    ap.add_argument("--out",  default=None, help="Output directory.")
    ap.add_argument("--tag", action="append",
                    help="KEY=VALUE integer column stamped on every row. Repeatable.")
    ap.add_argument("--fill", default=None,
                    help="Jagged pad flag. Default -999 (numeric). Also: 'x' "
                         "(legacy events.root sentinel), 'nan', or any number.")
    ap.add_argument("--note", default=None, help="(quick) provenance text.")
    ap.add_argument("--no-parquet", action="store_true",
                    help="(quick) skip canonical parquet.")
    args = ap.parse_args()

    if bool(args.input) == bool(args.from_scan):
        ap.error("Give exactly one of: INPUT (quick mode) or --from-scan SCANDIR.")

    if args.from_scan:
        mode_from_scan(args)
    else:
        mode_quick(args)


if __name__ == "__main__":
    main()
