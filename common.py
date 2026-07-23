#!/usr/bin/env python3
# =============================================================================
# common.py — shared core for the root2csv toolkit
# =============================================================================
# Lineage: carved from flatten_events.py (N. Herling, 2026-04-21), the
# battle-tested events.root two-pass flattener. Classification logic,
# progress UI, and struct-safety helpers transplanted intact.
#
# Six branch bins (LOCKED):
#   scalar       ndim 1                      -> passes straight through
#   vec1         ndim 2, length 1 everywhere -> collapsed via [:, 0]
#   jagged       ndim 2, variable length     -> exploded + sentinel-padded
#   jagged_deep  ndim > 2                    -> parquet only, never CSV
#   empty        max_len 0 everywhere        -> skipped
#   unreadable   uproot cannot decode        -> recorded by name only
# =============================================================================

import json
import os
import sys
import glob
import numpy as np
import awkward as ak
import uproot

TOOL_VERSION  = "0.1.0"
FILL_SENTINEL = -999.0       # base-policy pad flag; override with --fill (x | nan | literal)

CATEGORIES = ("scalar", "vec1", "jagged", "jagged_deep", "empty", "unreadable")

DEFAULT_POLICY = {
    "scalar":      "direct",
    "vec1":        "collapse",
    "jagged":      "pad_max",       # editable: pad_max | first:N | drop
    "jagged_deep": "parquet_only",
    "empty":       "skip",
}


# ======================================================================
#   UI helpers — in-place stderr updates using \r   (transplanted)
# ======================================================================

def progress(label, i, n, detail=""):
    pct = 100.0 * i / n if n > 0 else 100.0
    bar_len = 24
    filled = int(bar_len * i / n) if n > 0 else bar_len
    bar = "#" * filled + "-" * (bar_len - filled)
    msg = f"\r  [{label}] [{bar}] {i:>5}/{n:<5} ({pct:5.1f}%)  {detail[:50]:<50s}"
    sys.stderr.write(msg)
    sys.stderr.flush()


def progress_end():
    sys.stderr.write("\n")
    sys.stderr.flush()


def info(msg):
    print(msg, flush=True)


# ======================================================================
#   CLI utilities   (transplanted)
# ======================================================================

def parse_tags(tag_args):
    """--tag KEY=VALUE (int) columns stamped on every output row."""
    tags = {}
    for t in tag_args or []:
        if "=" not in t:
            sys.exit(f"[ERROR] --tag must be KEY=VALUE (got: {t})")
        k, v = t.split("=", 1)
        try:
            tags[k.strip()] = int(v.strip())
        except ValueError:
            sys.exit(f"[ERROR] --tag VALUE must be an integer (got: {v})")
    return tags


def resolve_fill(fill_arg):
    """Default -999.0 (numeric flag, columns stay numeric). 'nan' -> np.nan,
    'x' -> legacy events.root sentinel, any number -> that number, else literal."""
    if fill_arg is None:
        return FILL_SENTINEL
    a = str(fill_arg).strip()
    if a.lower() == "nan":
        return np.nan
    if a.lower() == "x":
        return "x"
    try:
        return float(a)
    except ValueError:
        return a


# ======================================================================
#   Input discovery
# ======================================================================

def find_root_file(path):
    """
    Accept a .root file directly, or a directory to search.
    Directory search: *.root and */*.root, largest file wins
    (generalized from find_events_root — largest beats newest to dodge
    empty symlink targets).
    """
    if os.path.isfile(path):
        return path
    if os.path.isdir(path):
        matches = []
        for pat in ("*.root", "*/*.root"):
            matches += [p for p in glob.glob(os.path.join(path, pat))
                        if os.path.getsize(p) > 1000]
        if not matches:
            raise FileNotFoundError(f"No .root files found under {path}")
        matches.sort(key=lambda p: os.path.getsize(p), reverse=True)
        return matches[0]
    raise FileNotFoundError(f"Input does not exist: {path}")


def resolve_tree(rootfile, tree_arg=None):
    """
    Return (tree, tree_name). If --tree given, use it. Otherwise enumerate
    all TTrees and pick the one with the most entries (fastframe files
    carry reco/truth/etc rather than a single 'analysis' tree).
    """
    f = uproot.open(rootfile)
    classnames = f.classnames()
    ttree_names = sorted({k.split(";")[0] for k, cls in classnames.items()
                          if cls.startswith("TTree") or "RNTuple" in cls})
    if not ttree_names:
        sys.exit(f"[ERROR] No TTrees or RNTuples found in {rootfile}. "
                 f"Objects present: {list(classnames.items())[:10]}")

    if tree_arg:
        if tree_arg not in ttree_names:
            sys.exit(f"[ERROR] Tree '{tree_arg}' not in file. "
                     f"Available: {ttree_names}")
        return f[tree_arg], tree_arg, ttree_names

    best, best_n = None, -1
    for tn in ttree_names:
        n = f[tn].num_entries
        if n > best_n:
            best, best_n = tn, n
    return f[best], best, ttree_names


# ======================================================================
#   Struct-safety helpers   (transplanted)
# ======================================================================

def dtype_tag(arr):
    try:
        np_arr = ak.to_numpy(arr)
        return str(np_arr.dtype)
    except Exception:
        return "object"


def safe_scalar_column(arr):
    """
    Convert an awkward 1D array to a pandas-safe column.

    Record / struct branches convert to numpy structured arrays
    (dtype.kind == 'V') which crash pandas to_csv during its NaN check.
    Detect those and stringify instead.
    """
    try:
        np_arr = ak.to_numpy(arr)
        if np_arr.dtype.kind == 'V' or np_arr.dtype.names is not None:
            return [str(x) for x in ak.to_list(arr)]
        return np_arr
    except Exception:
        return [str(x) for x in ak.to_list(arr)]


def profile_axes(arr):
    """
    Per-axis max lengths for any ndim >= 2 array (generalized
    profile_jagged_deep). Returns (axis_max_lens, leaf_dtype).
    """
    axis_max_lens = []
    cur = arr
    try:
        while cur.ndim > 1:
            lengths = ak.num(cur, axis=1)
            if len(lengths) == 0:
                axis_max_lens.append(0)
                break
            axis_max_lens.append(int(ak.max(lengths)))
            cur = ak.flatten(cur, axis=1)
        try:
            leaf_dtype = str(ak.to_numpy(cur).dtype)
        except Exception:
            leaf_dtype = "object"
    except Exception as e:
        axis_max_lens = ["?"]
        leaf_dtype = f"probe-failed: {str(e)[:40]}"
    return axis_max_lens, leaf_dtype


def sample_values(arr, k=2, width=60):
    """First k events, repr-truncated — human orientation only."""
    out = []
    try:
        for v in ak.to_list(arr[:k]):
            s = repr(v)
            out.append(s[:width] + ("..." if len(s) > width else ""))
    except Exception:
        out.append("(unsampleable)")
    return out


# ======================================================================
#   Classification — the six bins   (transplanted logic, extended stats)
# ======================================================================

def classify_branch(arr, deep_profile=True):
    """
    Classify one branch array into a record dict.
    Extended beyond flatten_events with length stats, fill fraction,
    awkward type string, and projected fan-out — the 'record every
    aspect of every struct' layer.
    """
    rec = {"ak_type": str(arr.type)}

    if arr.ndim == 1:
        rec.update({"category": "scalar", "dtype": dtype_tag(arr),
                    "ndim": 1, "max_len": 1, "n_spacers": 0, "fanout": 1})
        return rec

    if arr.ndim == 2:
        lengths = ak.num(arr, axis=1)
        if len(lengths) == 0:
            rec.update({"category": "empty", "dtype": "none", "ndim": 2,
                        "max_len": 0, "n_spacers": 0, "fanout": 0})
            return rec
        np_len  = np.asarray(ak.to_numpy(lengths))
        max_len = int(np_len.max())
        if max_len == 0:
            rec.update({"category": "empty", "dtype": "none", "ndim": 2,
                        "max_len": 0, "n_spacers": 0, "fanout": 0})
            return rec
        len_stats = {"len_min": int(np_len.min()),
                     "len_med": float(np.median(np_len)),
                     "len_p95": float(np.percentile(np_len, 95)),
                     "len_max": max_len,
                     "fill_frac": float((np_len > 0).mean())}
        if int(np_len.min()) == 1 and max_len == 1:
            rec.update({"category": "vec1", "dtype": dtype_tag(arr[:, 0]),
                        "ndim": 2, "max_len": 1, "n_spacers": 0, "fanout": 1,
                        **len_stats})
        else:
            n_spacers = int((max_len - np_len).sum())
            rec.update({"category": "jagged", "dtype": "object",
                        "ndim": 2, "max_len": max_len,
                        "n_spacers": n_spacers, "fanout": max_len,
                        **len_stats})
        return rec

    # ndim > 2
    if deep_profile:
        axis_max_lens, leaf_dtype = profile_axes(arr)
    else:
        axis_max_lens, leaf_dtype = ["?"], "unprofiled"
    hypo = 1
    for v in axis_max_lens:
        if isinstance(v, int):
            hypo *= max(v, 1)
        else:
            hypo = -1
            break
    rec.update({"category": "jagged_deep", "dtype": leaf_dtype,
                "ndim": int(arr.ndim), "axis_max_lens": axis_max_lens,
                "hypo_cols": hypo, "max_len": -1, "n_spacers": 0,
                "fanout": hypo})
    return rec


# ======================================================================
#   Manifest JSON IO
# ======================================================================

def manifest_json_path(out_dir, name):
    return os.path.join(out_dir, f"{name}_manifest.json")


def write_manifest_json(path, meta, records, unreadable, tags):
    doc = {"meta": {**meta, "tool_version": TOOL_VERSION},
           "tags": tags,
           "branches": records,
           "unreadable": unreadable}
    with open(path, "w") as f:
        json.dump(doc, f, indent=1, default=str)
    return path


def read_manifest_json(path):
    with open(path) as f:
        return json.load(f)


def fill_repr(f):
    """Terse display form of the pad flag."""
    import math
    if isinstance(f, float) and math.isnan(f):
        return "NaN"
    if isinstance(f, float) and f.is_integer():
        return str(int(f))
    return str(f)


def synopsis_scan(name, n, records, unreadable, tags):
    """Terse end-of-scan summary — headline: CSV columns under the base policy."""
    cnt = {k: 0 for k in ("scalar", "vec1", "jagged", "jagged_deep", "empty")}
    cols = len(tags)
    for r in records.values():
        cnt[r["category"]] += 1
        if r["category"] in ("scalar", "vec1"):
            cols += 1
        elif r["category"] == "jagged":
            cols += r["max_len"]
    bar = "  " + "-" * 60
    info(bar)
    info(f"  SYNOPSIS  {name}   events={n:,}")
    info(f"    branches {len(records)+len(unreadable)} = readable {len(records)} + unreadable {len(unreadable)}")
    info(f"    bins: scalar {cnt['scalar']} | vec1 {cnt['vec1']} | jagged {cnt['jagged']}"
         f" | jagged-deep {cnt['jagged_deep']} | empty {cnt['empty']}")
    info(f"    CSV columns as-is (base policy): {cols:,}")
    info(bar)


def synopsis_convert(name, n_rows, n_cols, csv_path, fill, n_dropped):
    """Terse end-of-convert summary."""
    size = os.path.getsize(csv_path) / 1e6
    bar = "  " + "-" * 60
    info(bar)
    info(f"  SYNOPSIS  {name}   {n_rows:,} rows x {n_cols:,} cols -> {os.path.basename(csv_path)}")
    info(f"    {size:.1f} MB | fill {fill_repr(fill)} | dropped by policy: {n_dropped}")
    info(bar)
