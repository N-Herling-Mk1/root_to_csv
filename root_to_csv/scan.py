#!/usr/bin/env python3
# =============================================================================
# scan.py — Layer 1: scan a ROOT file, record every aspect of every struct
# =============================================================================
# Pass 1 only. Reads the ROOT file once and drops four artifacts into OUT:
#
#   <name>_canonical.parquet   everything readable, lists kept as lists
#                              (bulk storage — CSVs become disposable views)
#   <name>_manifest.json       machine-readable census; drives convert.py
#                              --from-scan; per-branch policies editable here
#   <name>_manifest.txt        terse ASCII audit (grep / Ctrl-F friendly)
#   (stdout summary)
#
# Usage:
#   python3 scan.py /data2/kjohns/run3_sample_fastframe_files/somefile.root
#   python3 scan.py SOMEDIR                # searches *.root, largest wins
#   python3 scan.py file.root --tree reco --name mySample --out ./mySample_scan
#
# Unreadable branches (custom C++ classes uproot can't decode) are recorded
# by name + reason only — that's all that can be done, and it's good enough.
# =============================================================================

import argparse
import os
import sys
import time
from datetime import datetime

import awkward as ak
import numpy as np
import pyarrow.parquet as pq

from common import (TOOL_VERSION, DEFAULT_POLICY, progress, progress_end,
                    info, parse_tags, find_root_file, resolve_tree,
                    classify_branch, sample_values, write_manifest_json,
                    manifest_json_path, synopsis_scan)


# ======================================================================
#   Pass 1 — scan with progress   (transplanted, extended stats)
# ======================================================================

def pass1_scan(tree, keep_samples=True):
    all_names = [n if isinstance(n, str) else n.decode() for n in tree.keys()]
    total = len(all_names)
    records, unreadable, cache = {}, [], {}

    for i, bname in enumerate(all_names, start=1):
        progress("pass 1", i, total, detail=bname)
        try:
            cpp = str(tree[bname].typename)
        except Exception:
            cpp = "?"
        try:
            arr = tree[bname].array(library="ak")
        except Exception as e:
            unreadable.append({"name": bname, "cpp_typename": cpp,
                               "reason": str(e)[:120]})
            continue

        cache[bname] = arr
        rec = classify_branch(arr)
        rec["cpp_typename"] = cpp
        rec["policy"] = DEFAULT_POLICY[rec["category"]]
        if keep_samples:
            rec["samples"] = sample_values(arr)
        records[bname] = rec

    progress_end()
    return records, unreadable, cache


# ======================================================================
#   Canonical parquet   (transplanted write_parquet)
# ======================================================================

def write_canonical_parquet(cache, records, tags, n, parquet_path):
    to_pack = {}
    for bname, rec in records.items():
        if rec["category"] in ("scalar", "vec1", "jagged", "jagged_deep"):
            to_pack[bname] = cache[bname]
    for k, v in tags.items():
        to_pack[k] = ak.Array(np.full(n, v, dtype=np.int8))
    try:
        pa_table = ak.to_arrow_table(ak.Array(to_pack))
        pq.write_table(pa_table, parquet_path, compression="snappy")
        return True
    except Exception as e:
        info(f"  [WARN] parquet write failed: {e}")
        return False


# ======================================================================
#   ASCII manifest   (transplanted writer, generalized)
# ======================================================================

def write_manifest_txt(path, name, source, tree_name, records, unreadable,
                       n_events, out_files, note, timings, tags, fill=-999.0):
    by_cat = lambda c: sorted(b for b, r in records.items() if r["category"] == c)
    f_scalar, f_vec1  = by_cat("scalar"), by_cat("vec1")
    f_jagged, f_deep  = by_cat("jagged"), by_cat("jagged_deep")
    f_empty           = by_cat("empty")

    total_cols = len(tags) + len(f_scalar) + len(f_vec1) + \
                 sum(records[b]["max_len"] for b in f_jagged)
    total_spacers = sum(records[b]["n_spacers"] for b in f_jagged)
    n_readable, n_unreadable = len(records), len(unreadable)

    with open(path, "w") as f:
        f.write("=" * 78 + "\n")
        f.write(f"  root2csv Scan Manifest  (v{TOOL_VERSION})\n")
        f.write(f"  Sample:    {name}\n")
        f.write(f"  Source:    {source}\n")
        f.write(f"  Tree:      {tree_name}\n")
        f.write(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"  Events:    {n_events}\n")
        for label, secs in timings:
            f.write(f"  {label:<10s} {secs:.1f} s\n")
        f.write("=" * 78 + "\n\n")

        f.write("-" * 78 + "\n")
        f.write("PASS 1 SUMMARY  (branch classification — the six bins)\n")
        f.write("-" * 78 + "\n")
        f.write(f"  Total branches encountered:             {n_readable + n_unreadable}\n")
        f.write(f"  Readable:                               {n_readable}\n")
        f.write(f"    scalar          (1D):                 {len(f_scalar)}\n")
        f.write(f"    vec1            (length-1 2D):        {len(f_vec1)}\n")
        f.write(f"    jagged          (explode + pad):      {len(f_jagged)}\n")
        f.write(f"    jagged-deep     (ndim > 2, pq only):  {len(f_deep)}\n")
        f.write(f"    empty           (max_len=0, skipped): {len(f_empty)}\n")
        f.write(f"  Unreadable (recorded by name):          {n_unreadable}\n\n")
        f.write(f"  Projected flat CSV column count:        {total_cols}\n")
        f.write(f"  Projected sentinel insertions:          {total_spacers} (fill='{fill}')\n")
        f.write(f"  User-added tag columns:                 {len(tags)}  -> {list(tags.keys())}\n\n")

        if note:
            f.write("-" * 78 + "\n")
            f.write("PROVENANCE / NOTE\n")
            f.write("-" * 78 + "\n")
            f.write(f"  {note}\n\n")

        f.write("-" * 78 + "\n")
        f.write("OUTPUT FILES\n")
        f.write("-" * 78 + "\n")
        for label, p in out_files:
            f.write(f"  {label:<10s} {p}\n")
        f.write("\n")

        f.write("-" * 78 + "\n")
        f.write("BRANCH CENSUS  (individual entries)\n")
        f.write("-" * 78 + "\n\n")
        for k in tags:
            f.write(f"  [added]   {k}\n")
        if tags:
            f.write("\n")
        for b in f_scalar:
            r = records[b]
            f.write(f"  [scalar]  {b:<52s} dtype={r['dtype']}\n")
        f.write("\n")
        for b in f_vec1:
            r = records[b]
            f.write(f"  [vec1]    {b:<52s} dtype={r['dtype']}\n")
        f.write("\n")
        for b in f_jagged:
            r = records[b]
            f.write(f"  [jagged]  {b:<52s} max={r['max_len']:<4d} "
                    f"med={r['len_med']:<5.1f} p95={r['len_p95']:<6.1f} "
                    f"spacers={r['n_spacers']:<9d} policy={r['policy']} "
                    f"-> {r['max_len']} cols\n")
        f.write("\n")

        if f_deep:
            f.write("-" * 78 + "\n")
            f.write("JAGGED-DEEP  (ndim > 2, in parquet only)\n")
            f.write("  Structure inventory only -- NOT flattened into CSV.\n")
            f.write("-" * 78 + "\n")
            for b in f_deep:
                r = records[b]
                axes = r.get("axis_max_lens", [])
                axes_str = " x ".join(str(v) for v in axes) if axes else "?"
                f.write(f"  {b}\n")
                f.write(f"    C++ type:         {r.get('cpp_typename', '?')}\n")
                f.write(f"    awkward type:     {r.get('ak_type', '?')[:70]}\n")
                f.write(f"    ndim:             {r.get('ndim', '?')}\n")
                f.write(f"    axis max-lengths: {axes_str}\n")
                f.write(f"    leaf dtype:       {r['dtype']}\n")
                hypo = r.get("hypo_cols", -1)
                hypo_s = str(hypo) if hypo > 0 else "unknown"
                f.write(f"    hypothetical columns if fully flattened: {hypo_s} (SKIPPED)\n\n")

        if f_empty:
            f.write("-" * 78 + "\n")
            f.write("EMPTY BRANCHES (max_len = 0, skipped from CSV)\n")
            f.write("-" * 78 + "\n")
            for b in f_empty:
                f.write(f"  {b}\n")
            f.write("\n")

        f.write("-" * 78 + "\n")
        f.write(f"UNABLE TO READ   ({len(unreadable)})  — names recorded; nothing more can be done\n")
        f.write("-" * 78 + "\n")
        if unreadable:
            for u in unreadable:
                f.write(f"  {u['name']:<40s}  type: {u.get('cpp_typename','?')[:30]:<30s}  reason: {u['reason']}\n")
        else:
            f.write("  (none)\n")
        f.write("\n")

        f.write("-" * 78 + "\n")
        f.write("CONDENSED BRANCH AUDIT  (binary: flattened vs unreadable)\n")
        f.write("-" * 78 + "\n")
        audit = []
        for b in f_scalar + f_vec1 + f_jagged:
            audit.append((b, "flattened"))
        for b in f_deep + f_empty:
            audit.append((b, "unreadable"))
        for u in unreadable:
            audit.append((u["name"], "unreadable"))
        audit.sort(key=lambda x: x[0])
        flat_count   = sum(1 for _, lab in audit if lab == "flattened")
        unread_count = len(audit) - flat_count
        f.write(f"\n  Total: {len(audit)}   flattened={flat_count}   unreadable={unread_count}\n\n")
        for nm, label in audit:
            tag = "[flattened] " if label == "flattened" else "[unreadable]"
            f.write(f"  {tag}  {nm}\n")
        f.write("\n")


# ======================================================================
#   Orchestration — importable by convert.py (quick mode)
# ======================================================================

def run_scan(input_path, tree_arg, name, out_dir, tags, note,
             write_pq=True, keep_samples=True):
    rootfile = find_root_file(input_path)
    if name is None:
        name = os.path.splitext(os.path.basename(rootfile))[0]
    if out_dir is None:
        out_dir = os.path.join(os.getcwd(), f"{name}_scan")
    os.makedirs(out_dir, exist_ok=True)

    tree, tree_name, all_trees = resolve_tree(rootfile, tree_arg)
    n = tree.num_entries

    info(f"\n{'='*70}")
    info(f"  root2csv SCAN  (Layer 1, v{TOOL_VERSION})")
    info(f"{'='*70}")
    info(f"  Name:     {name}")
    info(f"  Source:   {rootfile}")
    info(f"  Trees:    {all_trees}  ->  using '{tree_name}'")
    info(f"  Events:   {n}")
    info(f"  Branches: {len(tree.keys())}")
    info(f"  Out dir:  {out_dir}\n")

    info("Pass 1: scanning all events of all branches ...")
    t0 = time.time()
    records, unreadable, cache = pass1_scan(tree, keep_samples=keep_samples)
    pass1_s = time.time() - t0
    info(f"  Pass 1 done: {pass1_s:.1f}s  readable={len(records)}  unreadable={len(unreadable)}")

    parquet_path = os.path.join(out_dir, f"{name}_canonical.parquet")
    pq_s = 0.0
    if write_pq:
        info("\nWriting canonical parquet (bulk storage, lists stay lists) ...")
        t0 = time.time()
        ok = write_canonical_parquet(cache, records, tags, n, parquet_path)
        pq_s = time.time() - t0
        if ok:
            info(f"  Parquet: {parquet_path}  "
                 f"({os.path.getsize(parquet_path)/1e6:.1f} MB, {pq_s:.1f}s)")
        else:
            parquet_path = None
    else:
        parquet_path = None

    meta = {"name": name, "source": rootfile, "tree": tree_name,
            "all_trees": all_trees, "n_events": n, "note": note,
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pass1_seconds": round(pass1_s, 1),
            "canonical_parquet": parquet_path}
    json_path = manifest_json_path(out_dir, name)
    write_manifest_json(json_path, meta, records, unreadable, tags)
    info(f"  Manifest JSON: {json_path}")

    txt_path = os.path.join(out_dir, f"{name}_manifest.txt")
    out_files = [("Parquet:", parquet_path or "(skipped)"),
                 ("JSON:", json_path), ("TXT:", txt_path)]
    write_manifest_txt(txt_path, name, rootfile, tree_name, records, unreadable,
                       n, out_files, note,
                       [("Pass 1:", pass1_s), ("Parquet:", pq_s)], tags)
    info(f"  Manifest TXT:  {txt_path}")

    synopsis_scan(name, n, records, unreadable, tags)

    return {"records": records, "unreadable": unreadable, "cache": cache,
            "n": n, "name": name, "out_dir": out_dir, "rootfile": rootfile,
            "tree_name": tree_name, "parquet_path": parquet_path,
            "json_path": json_path, "txt_path": txt_path,
            "pass1_seconds": pass1_s}


def main():
    ap = argparse.ArgumentParser(
        description="Layer 1: scan a ROOT file, classify every branch into "
                    "the six bins, write canonical parquet + manifests.")
    ap.add_argument("input", help=".root file, or a directory to search "
                                  "(largest .root wins).")
    ap.add_argument("--tree", default=None,
                    help="TTree name. Default: auto-detect (most entries).")
    ap.add_argument("--name", default=None,
                    help="Output prefix. Default: ROOT filename stem.")
    ap.add_argument("--out", default=None,
                    help="Output directory. Default: ./<name>_scan")
    ap.add_argument("--tag", action="append",
                    help="KEY=VALUE integer column stamped on every row. Repeatable.")
    ap.add_argument("--note", default=None,
                    help="Freeform provenance text recorded in the manifests.")
    ap.add_argument("--no-parquet", action="store_true",
                    help="Skip canonical parquet (manifests only).")
    ap.add_argument("--no-samples", action="store_true",
                    help="Skip per-branch sample values in manifest.json.")
    args = ap.parse_args()

    res = run_scan(args.input, args.tree, args.name, args.out,
                   parse_tags(args.tag), args.note,
                   write_pq=not args.no_parquet,
                   keep_samples=not args.no_samples)

    info(f"\n{'='*70}")
    info(f"  SCAN DONE  {res['name']}   pass1={res['pass1_seconds']:.1f}s")
    info(f"  Next: edit policies in {os.path.basename(res['json_path'])} if desired, then")
    info(f"        python3 convert.py --from-scan {res['out_dir']}")
    info(f"{'='*70}\n")


if __name__ == "__main__":
    main()
