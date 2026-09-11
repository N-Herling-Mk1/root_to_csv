#!/usr/bin/env python3
"""
dropfilter.py - root2csv OPTIONAL FEATURE-FILTER LAYER

WHAT THIS IS
    A post-processing pass that produces a FILTERED CSV from flat.csv,
    removing whole branches named in a drop list.

    flat.csv   (layer 2's output — already exploded, never modified)
            |
            |  drop_list.txt   <- the physics decision, human-edited
            |  manifest.json   <- the lookup table, machine-written
            v
      filtered.csv  +  drop_report.txt

    WHY NOT THE PARQUET. canonical.parquet keeps lists as lists (SPEC
    decision 2): a jagged branch is ONE list-valued parquet column, not its
    N exploded CSV columns. Filtering there would drop 1 column per branch
    instead of `fanout`, and the surviving file would hold list cells rather
    than a flat table. Exploding it here would mean reimplementing layer 2's
    padding, fill and policy handling inside layer 3 — the duplication this
    split exists to avoid. So layer 3 reads the already-flat CSV.
    The parquet remains the untouched master; it is simply not the input.

WHY IT IS A SEPARATE FILE
    The drop set is expected to change. Re-running convert.py against the ROOT
    file for every edit is the wrong loop. This layer reads the parquet only,
    so a re-filter costs seconds and needs no ROOT file, no uproot, no awkward.
    convert.py is not touched by this module.

THE TWO INPUTS DO DIFFERENT JOBS
    drop_list.txt  decides WHAT is dropped   (branch names - a physics call)
    manifest.json  decides WHERE it lives    (branch -> its expanded columns)

    The manifest is never the policy. It is the authority on which branch names
    are real, which is what turns a typo into an error instead of a silent
    no-op that quietly ships a wrong-shaped training set.

COLUMN RESOLUTION - AND ITS ONE ASSUMPTION
    A branch B owns exactly the columns whose header is `B` or `B_<digits>`.
    The match is ANCHORED. This is what keeps `track_eta_NOSYS` and
    `trackID_eta` separate - a naive substring or prefix match merges the two
    families. Where the manifest states an expected width, the resolved count
    is cross-checked against it and any disagreement is reported loudly.

    >> VERIFY THIS AGAINST convert.py's suffix convention before trusting a
    >> production run. If convert.py ever changes how it names expanded
    >> columns, the WIDTH MISMATCH section of the report is what tells you.

USAGE
    # what would happen - resolve and count, write nothing
    python3 dropfilter.py ./s1_scan --preview

    # do it
    python3 dropfilter.py ./s1_scan

    # the repo's drop_list.txt is found automatically from any working
    # directory. --drop-file overrides it with a list of your own.

    # show wide branches as drop candidates - prints only, drops nothing
    python3 dropfilter.py ./s1_scan --drop-file drop_list.txt --suggest-width 64

EXIT CODES
    0  clean
    2  one or more drop-list entries matched no branch in the manifest
       (override with --allow-unmatched)
    3  bad inputs / missing files
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

VERSION = "dropfilter 0.1.0"
ROWGROUP_ROWS = 20000


# ---------------------------------------------------------------------------
# progress / status
# ---------------------------------------------------------------------------

class Progress:
    """Single-line progress bar. Falls back to periodic prints when not a tty."""

    def __init__(self, total, label, width=34, enabled=True):
        self.total = max(int(total), 1)
        self.label = label
        self.width = width
        self.enabled = enabled
        self.n = 0
        self.t0 = time.time()
        self.tty = sys.stdout.isatty()
        self._last = -1.0
        if self.enabled:
            self._draw()

    def step(self, k=1):
        self.n = min(self.n + k, self.total)
        if self.enabled:
            self._draw()

    def _draw(self):
        frac = self.n / self.total
        now = time.time()
        if not self.tty:
            if frac < 1.0 and (now - self._last) < 2.0:
                return
            self._last = now
            print(f"  {self.label}: {frac * 100:5.1f}%  ({self.n}/{self.total})",
                  flush=True)
            return
        filled = int(round(frac * self.width))
        bar = "#" * filled + "-" * (self.width - filled)
        el = now - self.t0
        sys.stdout.write(
            f"\r  {self.label:<22} [{bar}] {frac * 100:5.1f}%  {el:5.1f}s"
        )
        sys.stdout.flush()

    def done(self, note=""):
        if not self.enabled:
            return
        self.n = self.total
        self._draw()
        if self.tty:
            sys.stdout.write("\n")
        if note:
            print(f"  {note}")
        sys.stdout.flush()


def say(msg=""):
    print(msg, flush=True)


def rule(char="-", n=74):
    say(char * n)


def die(msg, code=3):
    say("")
    say(f"ERROR: {msg}")
    sys.exit(code)


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------

def discover(scan_dir):
    """Find the manifest / parquet / csv triple inside a scan directory."""
    if not os.path.isdir(scan_dir):
        die(f"not a directory: {scan_dir}")
    found = {"manifest": None, "parquet": None, "csv": None, "name": None}
    for fn in sorted(os.listdir(scan_dir)):
        path = os.path.join(scan_dir, fn)
        if fn.endswith("_manifest.json") or fn == "manifest.json":
            found["manifest"] = path
            found["name"] = fn[: -len("_manifest.json")] if fn.endswith("_manifest.json") else "out"
        elif fn.endswith("_canonical.parquet") or fn == "canonical.parquet":
            found["parquet"] = path
        elif fn.endswith("_flat.csv") or fn == "flat.csv":
            found["csv"] = path
    if not found["manifest"]:
        die(f"no *_manifest.json found in {scan_dir}")
    return found


def load_manifest(path):
    """
    Return {branch: {bin, policy, width_expected(or None), raw}}.

    Tolerant of schema drift: the real manifest carries flat len_min/len_med/
    len_p95/len_max, fill_frac, cpp_typename, and a "meta" block. Anything
    whose width cannot be determined confidently is recorded as None and simply
    skips the cross-check rather than guessing.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as exc:
        die(f"cannot read manifest {path}: {exc}")

    # branches may sit at top level or under a "branches" key, beside "meta"
    if isinstance(doc, dict) and isinstance(doc.get("branches"), dict):
        entries = doc["branches"]
    elif isinstance(doc, dict):
        entries = {k: v for k, v in doc.items()
                   if k != "meta" and isinstance(v, dict)}
    else:
        die("unrecognised manifest structure (expected a JSON object)")

    out = {}
    for branch, info in entries.items():
        if not isinstance(info, dict):
            continue
        bin_ = str(info.get("category", info.get("bin", info.get("kind", "?"))))
        policy = info.get("policy")
        width = _expected_width(bin_, policy, info)
        out[branch] = {"bin": bin_, "policy": policy,
                       "width_expected": width, "raw": info}
    if not out:
        die("manifest contained no branch entries")
    return out


def _resolve_policy_width(rec):
    """
    Width of a jagged branch's CSV expansion.

    MUST match convert.py::resolve_policy exactly -- it is the function that
    actually names the columns. Implemented locally rather than imported:
    convert.py -> common.py -> uproot, and layer 3 must stay runnable on a
    copied-down scan directory with no uproot installed. _assert_no_drift()
    below cross-checks the two whenever convert.py IS importable, so the
    duplication cannot rot silently.
    """
    pol = rec.get("policy", "pad_max")
    max_len = rec.get("max_len")
    if not isinstance(max_len, int):
        return None
    if pol == "pad_max":
        return max_len
    if pol == "drop":
        return 0
    if isinstance(pol, str) and pol.startswith("first:"):
        try:
            return max(0, min(int(pol.split(":", 1)[1]), max_len))
        except ValueError:
            pass
    return max_len          # convert.py warns and falls back to pad_max


def _assert_no_drift(records):
    """
    If convert.py can be imported, verify our copy of the policy rule still
    agrees with its original on every branch in this manifest. A disagreement
    means layer 2 changed how it sizes columns and layer 3 did not follow.
    Silent when convert.py is unavailable -- that is the supported laptop case.
    """
    try:
        from convert import resolve_policy
    except Exception:
        return None
    bad = []
    for name, rec in records.items():
        if rec.get("category") != "jagged" or "max_len" not in rec:
            continue
        action, n = resolve_policy(rec, name)
        theirs = 0 if action == "drop" else int(n)
        if theirs != _resolve_policy_width(rec):
            bad.append(name)
    return bad


def _expected_width(bin_, policy, info):
    """
    Expansion width. None means 'do not cross-check'.

    AUTHORITY ORDER MATTERS. The real CSV width comes from the branch's
    POLICY, not from `fanout`. `fanout` is only the base-policy width frozen
    at scan time: after a --from-scan run with an edited "first:N" policy the
    CSV is narrower than fanout, and trusting fanout would fire a spurious
    mismatch on every edited branch.
    """
    if str(bin_).lower() == "jagged":
        w = _resolve_policy_width(info)
        if w is not None:
            return w
    fo = info.get("fanout")
    if isinstance(fo, (int, float)) and not isinstance(fo, bool):
        return int(fo)
    b = bin_.lower()
    if b.startswith("empty"):
        return 0
    if b.startswith("scalar"):
        return 1
    if isinstance(policy, str):
        p = policy.strip().lower()
        if p == "drop":
            return 0
        if p == "collapse":
            return 1
        if p == "pad_max":
            lm = info.get("len_max")
            return int(lm) if isinstance(lm, (int, float)) else None
        m = re.fullmatch(r"first:(\d+)", p)
        if m:
            return int(m.group(1))
    if b.startswith("vec1"):
        return 1
    return None


def load_drop_list(path):
    """Return [(lineno, pattern)] preserving file order."""
    if not os.path.isfile(path):
        die(f"drop list not found: {path}")
    items = []
    with open(path, "r", encoding="utf-8") as fh:
        for i, raw in enumerate(fh, start=1):
            line = raw.split("#", 1)[0].strip()
            if line:
                items.append((i, line))
    if not items:
        die(f"drop list {path} contained no entries")
    return items


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

def resolve_patterns(patterns, manifest):
    """
    Map drop-list entries onto real branch names.

    Returns (matches, unmatched, duplicates) where
      matches    ordered [(lineno, pattern, [branch, ...])]
      unmatched  [(lineno, pattern)]   <- the audit that stops silent no-ops
      duplicates [(branch, [pattern, ...])]
    """
    branches = list(manifest.keys())
    matches, unmatched = [], []
    seen = {}
    for lineno, pat in patterns:
        if any(ch in pat for ch in "*?["):
            hit = sorted(fnmatch.filter(branches, pat))
        else:
            hit = [pat] if pat in manifest else []
        if hit:
            matches.append((lineno, pat, hit))
            for b in hit:
                seen.setdefault(b, []).append(pat)
        else:
            unmatched.append((lineno, pat))
    duplicates = [(b, p) for b, p in seen.items() if len(p) > 1]
    return matches, unmatched, duplicates


COL_CACHE = {}


def columns_for(branch, headers):
    """
    Columns owned by `branch`: exact header `branch`, or `branch_<digits>`.

    Anchored on purpose. `track_eta_NOSYS` must not absorb `trackID_eta_7`.
    """
    key = branch
    if key not in COL_CACHE:
        COL_CACHE[key] = re.compile(r"^" + re.escape(branch) + r"(?:_\d+)?$")
    rx = COL_CACHE[key]
    return [h for h in headers if rx.match(h)]


def build_plan(matches, manifest, headers):
    """Resolve matched branches to concrete column names + integrity checks."""
    plan = []            # [(branch, [cols], expected_or_None, status)]
    drop_cols = set()
    for _, _, hits in matches:
        for branch in hits:
            if any(p[0] == branch for p in plan):
                continue
            cols = columns_for(branch, headers)
            exp = manifest[branch]["width_expected"]
            if exp == 0 and not cols:
                status = "NO-OP"          # empty bin, never reached the CSV
            elif not cols:
                status = "ABSENT"         # manifest says it exists, data disagrees
            elif exp is None:
                status = "OK*"            # width unknown, no cross-check possible
            elif len(cols) == exp:
                status = "OK"
            else:
                status = "WIDTH MISMATCH"
            plan.append((branch, cols, exp, status))
            drop_cols.update(cols)
    plan.sort(key=lambda r: (-len(r[1]), r[0]))
    return plan, drop_cols


def width_candidates(manifest, headers, threshold, already):
    """Branches wider than `threshold` that are NOT already dropped. Advisory."""
    out = []
    for branch in manifest:
        if branch in already:
            continue
        n = len(columns_for(branch, headers))
        if n > threshold:
            out.append((branch, n))
    out.sort(key=lambda r: -r[1])
    return out


# ---------------------------------------------------------------------------
# data access
# ---------------------------------------------------------------------------

def csv_headers(path):
    import csv as _csv
    with open(path, "r", newline="", encoding="utf-8") as fh:
        row = next(_csv.reader(fh), None)
    if row is None:
        die(f"{path} is empty")
    return list(row), path


def write_filtered_from_csv(src, dst, keep_idx, quiet):
    import csv as _csv
    total = max(os.path.getsize(src), 1)
    bar = Progress(total, "writing filtered csv", enabled=not quiet)
    rows = 0
    with open(src, "r", newline="", encoding="utf-8") as fin, \
         open(dst, "w", newline="", encoding="utf-8") as fout:
        reader, writer = _csv.reader(fin), _csv.writer(fout)
        header = next(reader)
        writer.writerow([header[i] for i in keep_idx])
        seen_bytes = 0
        for row in reader:
            writer.writerow([row[i] for i in keep_idx])
            rows += 1
            if rows % 500 == 0:
                seen_bytes = fin.tell() if hasattr(fin, "tell") else seen_bytes
                bar.n = min(seen_bytes, total)
                bar._draw()
    bar.done()
    return rows


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def write_report(path, ctx):
    L = []
    w = L.append
    w("root2csv - FEATURE FILTER / DROP REPORT")
    w("=" * 74)
    w(f"generated      : {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    w(f"tool           : {VERSION}")
    w(f"manifest       : {ctx['manifest_path']}")
    w(f"drop list      : {ctx['drop_path']}")
    w(f"source data    : {ctx['source_path']}  ({ctx['source_kind']})")
    w(f"filtered csv   : {ctx['out_path'] or '(preview - nothing written)'}")
    w("")
    w("SOURCE : flat.csv (layer 2 output). Read-only; a new file is written.")
    w("MASTER : canonical.parquet is neither read nor modified by this tool.")
    w("         It keeps lists as lists, so it is not a flat table to filter.")
    w("")

    w("-" * 74)
    w("TOTALS")
    w("-" * 74)
    w(f"  branches in manifest      : {ctx['n_branches']}")
    w(f"  drop-list entries         : {ctx['n_patterns']}")
    w(f"  branches matched          : {ctx['n_matched']}")
    w(f"  branches remaining        : {ctx['n_branches'] - ctx['n_matched']}")
    w("")
    w(f"  columns before            : {ctx['cols_before']:,}")
    w(f"  columns removed           : {ctx['cols_removed']:,}")
    w(f"  columns after             : {ctx['cols_after']:,}")
    pct = 100.0 * ctx['cols_removed'] / max(ctx['cols_before'], 1)
    w(f"  reduction                 : {pct:.1f}%")
    if ctx.get("rows") is not None:
        w(f"  rows written              : {ctx['rows']:,}")
    w("")

    w("-" * 74)
    w("MATCHED  (branch / columns removed / manifest width / status)")
    w("-" * 74)
    for branch, cols, exp, status in ctx["plan"]:
        e = "-" if exp is None else str(exp)
        w(f"  {branch:<44} {len(cols):>6}   {e:>6}   {status}")
    w("")

    noop = [r for r in ctx["plan"] if r[3] == "NO-OP"]
    if noop:
        w("-" * 74)
        w("NO-OP  (listed, but empty-bin - never reached the CSV)")
        w("-" * 74)
        for branch, _, _, _ in noop:
            w(f"  {branch}")
        w("")

    bad = [r for r in ctx["plan"] if r[3] in ("WIDTH MISMATCH", "ABSENT")]
    if bad:
        w("-" * 74)
        w("!! INTEGRITY  - manifest and data disagree")
        w("-" * 74)
        w("   Manifest and data file are probably from different runs, or the")
        w("   column-suffix convention in convert.py has changed.")
        for branch, cols, exp, status in bad:
            w(f"  {branch:<44} resolved={len(cols)} expected={exp}  [{status}]")
        w("")

    w("-" * 74)
    w("UNMATCHED  (no such branch in the manifest - typo or wrong file)")
    w("-" * 74)
    if ctx["unmatched"]:
        for lineno, pat in ctx["unmatched"]:
            w(f"  line {lineno:>4}:  {pat}")
    else:
        w("  none - every drop-list entry resolved to a real branch.")
    w("")

    if ctx["duplicates"]:
        w("-" * 74)
        w("REDUNDANT  (branch selected by more than one entry)")
        w("-" * 74)
        for branch, pats in ctx["duplicates"]:
            w(f"  {branch:<44} <- {', '.join(pats)}")
        w("")

    if ctx.get("candidates"):
        w("-" * 74)
        w(f"ADVISORY  - kept branches wider than {ctx['cand_threshold']} columns")
        w("-" * 74)
        w("   Listed only. Nothing here was dropped.")
        for branch, n in ctx["candidates"]:
            w(f"  {branch:<44} {n:>6}")
        w("")

    w("=" * 74)
    w("end of report")
    text = "\n".join(L) + "\n"
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return text


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="dropfilter.py",
        description="Optional feature-filter layer: parquet master -> filtered CSV.",
    )
    ap.add_argument("scan_dir", help="scan/output directory (e.g. ./s1_scan)")
    ap.add_argument("--drop-file", default=None,
                    help="drop list, one branch per line "
                         "(default: drop_list.txt in the scan dir, then CWD; "
                         "if neither exists the layer is a clean no-op)")
    ap.add_argument("--preview", action="store_true",
                    help="resolve and count only; write nothing")
    ap.add_argument("--from-parquet", action="store_true",
                    help=argparse.SUPPRESS)   # retired: parquet keeps lists
    ap.add_argument("--out", default=None, help="output CSV path")
    ap.add_argument("--report", default=None, help="drop report path")
    ap.add_argument("--allow-unmatched", action="store_true",
                    help="downgrade unmatched drop-list entries to a warning")
    ap.add_argument("--suggest-width", type=int, default=None, metavar="N",
                    help="list kept branches wider than N columns (advisory only)")
    ap.add_argument("--quiet", action="store_true", help="suppress progress bars")
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args(argv)

    t0 = time.time()
    say("")
    rule("=")
    say(f"{VERSION}  -  optional feature-filter layer")
    rule("=")

    found = discover(args.scan_dir)

    # "(if present)" - an absent drop list is not an error. Step 2 is optional,
    # so the pipeline must still succeed with exit 0 and touch nothing.
    drop_path = args.drop_file
    if drop_path is None:
        # The repo ships drop_list.txt, so it should be found no matter where
        # the tool is invoked from. Search order is most-specific first:
        #   1. beside the data      - a list pinned to this one scan
        #   2. current directory    - a local override for this session
        #   3. beside this script   - the repo's shipped default
        here = os.path.dirname(os.path.abspath(__file__))
        for cand in (os.path.join(args.scan_dir, "drop_list.txt"),
                     "drop_list.txt",
                     os.path.join(here, "drop_list.txt")):
            if os.path.isfile(cand):
                drop_path = os.path.abspath(cand)
                break
    if drop_path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        say("  no drop list found - nothing to filter.")
        say(f"  looked for : {os.path.join(args.scan_dir, 'drop_list.txt')}")
        say("               ./drop_list.txt")
        say(f"               {os.path.join(here, 'drop_list.txt')}")
        say("  (the repo ships one - if none of these exist it was deleted)")
        say("")
        say("  step 2 skipped. Step 1 outputs are untouched.")
        rule("=")
        say("")
        return 0
    if not os.path.isfile(drop_path):
        # explicitly named but missing - that IS an error, the user meant it
        die(f"--drop-file given but not found: {drop_path}")

    manifest = load_manifest(found["manifest"])
    drift = _assert_no_drift({k: v["raw"] for k, v in manifest.items()})
    if drift:
        die("POLICY DRIFT: convert.py sizes these branches differently than\n"
            "       this tool expects: " + ", ".join(drift[:6]) +
            (f" (+{len(drift)-6} more)" if len(drift) > 6 else "") +
            "\n       convert.py::resolve_policy has changed. Update\n"
            "       dropfilter.py::_resolve_policy_width to match before filtering.")
    patterns = load_drop_list(drop_path)
    say(f"  manifest   : {found['manifest']}  ({len(manifest)} branches)")
    say(f"  drop list  : {drop_path}  ({len(patterns)} entries)")

    if args.from_parquet:
        die("--from-parquet is retired. canonical.parquet keeps lists as lists,\n"
            "       so a jagged branch is ONE column there, not its N exploded\n"
            "       CSV columns. Filtering it would remove 1 column per branch\n"
            "       and leave list cells behind. Layer 3 reads flat.csv.")
    if not found["csv"]:
        die("no *_flat.csv found in the scan directory.\n"
            "       Layer 3 filters layer 2's output, so run convert.py first:\n"
            "           python3 convert.py --from-scan " + args.scan_dir)
    headers, src = csv_headers(found["csv"])
    kind = "flat csv (layer 2 output - not modified)"
    say(f"  source     : {src}  ({len(headers):,} columns)")
    say("")

    say("  resolving drop list against manifest ...")
    matches, unmatched, duplicates = resolve_patterns(patterns, manifest)
    plan, drop_cols = build_plan(matches, manifest, headers)
    matched_branches = {b for b, _, _, _ in plan}
    keep_cols = [h for h in headers if h not in drop_cols]

    candidates = []
    if args.suggest_width is not None:
        candidates = width_candidates(manifest, headers, args.suggest_width,
                                      matched_branches)

    rows = None
    out_path = None
    if not args.preview:
        stem = found["name"] or "out"
        out_path = args.out or os.path.join(args.scan_dir, f"{stem}_filtered.csv")
        say("")
        idx = [i for i, h in enumerate(headers) if h not in drop_cols]
        rows = write_filtered_from_csv(src, out_path, idx, args.quiet)

    ctx = {
        "manifest_path": found["manifest"], "drop_path": drop_path,
        "source_path": src, "source_kind": kind, "out_path": out_path,
        "n_branches": len(manifest), "n_patterns": len(patterns),
        "n_matched": len(matched_branches),
        "cols_before": len(headers), "cols_removed": len(drop_cols),
        "cols_after": len(keep_cols), "rows": rows,
        "plan": plan, "unmatched": unmatched, "duplicates": duplicates,
        "candidates": candidates, "cand_threshold": args.suggest_width,
    }
    stem = found["name"] or "out"
    report_path = args.report or os.path.join(args.scan_dir, f"{stem}_drop_report.txt")
    if args.preview and args.report is None:
        report_path = None
    write_report(report_path, ctx)

    # ---- synopsis -------------------------------------------------------
    say("")
    rule("=")
    say("SYNOPSIS")
    rule("=")
    say(f"  branches   {len(manifest)} -> {len(manifest) - len(matched_branches)}"
        f"   ({len(matched_branches)} dropped)")
    pct = 100.0 * len(drop_cols) / max(len(headers), 1)
    say(f"  columns    {len(headers):,} -> {len(keep_cols):,}"
        f"   ({len(drop_cols):,} removed, {pct:.1f}%)")
    if rows is not None:
        mb = os.path.getsize(out_path) / (1024 * 1024)
        say(f"  wrote      {rows:,} x {len(keep_cols):,} -> {out_path}  ({mb:.1f} MB)")
    else:
        say("  wrote      nothing (--preview)")
    say(f"  source     {src} - UNCHANGED")
    say(f"  master     {found['parquet'] or '(none)'} - UNCHANGED (not read)")
    if report_path:
        say(f"  report     {report_path}")

    bad = [r for r in plan if r[3] in ("WIDTH MISMATCH", "ABSENT")]
    if bad:
        say("")
        say(f"  !! {len(bad)} branch(es) disagree with the manifest - see report")
    if duplicates:
        say(f"  .. {len(duplicates)} branch(es) selected by more than one entry")
    if candidates:
        say(f"  .. {len(candidates)} kept branch(es) wider than {args.suggest_width}"
            f" columns - advisory, see report")
    say(f"  elapsed    {time.time() - t0:.1f}s")
    rule("=")

    if unmatched:
        say("")
        say(f"UNMATCHED: {len(unmatched)} drop-list entr(ies) matched no branch:")
        for lineno, pat in unmatched[:12]:
            say(f"   line {lineno:>4}:  {pat}")
        if len(unmatched) > 12:
            say(f"   ... and {len(unmatched) - 12} more (full list in the report)")
        if not args.allow_unmatched:
            say("")
            say("Nothing above was dropped. Fix the names, or pass --allow-unmatched.")
            say("")
            return 2
        say("  (--allow-unmatched: continuing)")
    say("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
