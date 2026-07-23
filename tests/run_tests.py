#!/usr/bin/env python3
# =============================================================================
# tests/run_tests.py — end-to-end exercise of scan.py + convert.py
# Synthesizes a ROOT file with scalar / vec1 / jagged / empty branches and
# two trees (auto-detect check), plus direct unit checks for jagged_deep.
# =============================================================================
import json
import os
import subprocess
import sys

import awkward as ak
import numpy as np
import pandas as pd
import uproot

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
WORK = os.path.join(HERE, "work")
os.makedirs(WORK, exist_ok=True)

PASS, FAIL = 0, 0
def check(label, cond):
    global PASS, FAIL
    status = "ok " if cond else "FAIL"
    print(f"  [{status}] {label}")
    if cond: PASS += 1
    else:    FAIL += 1

# ----------------------------------------------------------------------
# 1. Synthesize test ROOT file
# ----------------------------------------------------------------------
rng = np.random.default_rng(42)
N = 40
rootpath = os.path.join(WORK, "test_sample.root")

jet_counts   = rng.integers(0, 6, N)          # jagged, incl. some zeros
track_counts = rng.integers(1, 4, N)          # jagged, never empty
jet_pt   = ak.unflatten(rng.exponential(50, jet_counts.sum()).astype(np.float32), jet_counts)
track_d0 = ak.unflatten(rng.normal(0, 1, track_counts.sum()).astype(np.float64), track_counts)
msvtx_eta = ak.unflatten(rng.uniform(-2.5, 2.5, N).astype(np.float32), np.ones(N, int))  # vec1
empty_b   = ak.unflatten(np.array([], dtype=np.float32), np.zeros(N, int))               # empty

with uproot.recreate(rootpath) as f:                     # genuine TTrees
    f.mktree("reco", {"eventNumber": np.int64, "met": np.float32,
                      "jet_pt": "var * float32", "track_d0": "var * float64",
                      "msvtx_eta": "var * float32", "empty_b": "var * float32"})
    f["reco"].extend({"eventNumber": np.arange(N, dtype=np.int64),
                      "met": rng.exponential(30, N).astype(np.float32),
                      "jet_pt": jet_pt, "track_d0": track_d0,
                      "msvtx_eta": msvtx_eta, "empty_b": empty_b})
    f.mktree("truth", {"pdgId": np.int32})
    f["truth"].extend({"pdgId": np.full(5, 25, dtype=np.int32)})

rnt_path = os.path.join(WORK, "test_rntuple.root")
with uproot.recreate(rnt_path) as f:                     # RNTuple format
    f["reco"] = {"met": rng.exponential(30, N).astype(np.float32),
                 "jet_pt": jet_pt}

print(f"\nSynth TTree file:   {rootpath}  ({os.path.getsize(rootpath)} bytes)")
print(f"Synth RNTuple file: {rnt_path}  ({os.path.getsize(rnt_path)} bytes)")

# ----------------------------------------------------------------------
# 2. Unit: classify_branch on all shapes incl. jagged_deep (ndim 3)
# ----------------------------------------------------------------------
print("\n[unit] classify_branch")
from common import classify_branch
check("scalar bin", classify_branch(ak.Array(np.arange(5)))["category"] == "scalar")
check("vec1 bin",   classify_branch(ak.Array([[1.], [2.], [3.]]))["category"] == "vec1")
jrec = classify_branch(ak.Array([[1, 2], [], [3, 4, 5]]))
check("jagged bin", jrec["category"] == "jagged" and jrec["max_len"] == 3)
check("jagged spacer count", jrec["n_spacers"] == (3-2) + (3-0) + (3-3))
deep = classify_branch(ak.Array([[[1., 2.], [3.]], [], [[4.]]]))
check("jagged_deep bin", deep["category"] == "jagged_deep" and deep["ndim"] == 3)
check("deep axis lens", deep["axis_max_lens"] == [2, 2])
check("empty bin", classify_branch(ak.Array([[], [], []]))["category"] == "empty")

# ----------------------------------------------------------------------
# 3. scan.py CLI — auto tree detect, artifacts exist
# ----------------------------------------------------------------------
print("\n[e2e] scan.py")
scan_out = os.path.join(WORK, "t1_scan")
r = subprocess.run([sys.executable, os.path.join(REPO, "scan.py"), rootpath,
                    "--name", "t1", "--out", scan_out,
                    "--tag", "is_test=1", "--note", "synthetic test sample"],
                   capture_output=True, text=True)
print(r.stdout[-400:])
check("scan exit 0", r.returncode == 0)
check("picked 'reco' (largest tree)", "using 'reco'" in r.stdout)
for suffix in ("_canonical.parquet", "_manifest.json", "_manifest.txt"):
    check(f"artifact t1{suffix}", os.path.isfile(os.path.join(scan_out, "t1"+suffix)))

man = json.load(open(os.path.join(scan_out, "t1_manifest.json")))
cats = {b: rec["category"] for b, rec in man["branches"].items()}
check("eventNumber scalar", cats.get("eventNumber") == "scalar")
check("msvtx_eta vec1",     cats.get("msvtx_eta") == "vec1")
check("jet_pt jagged",      cats.get("jet_pt") == "jagged")
check("empty_b empty",      cats.get("empty_b") == "empty")
check("stats present", "len_p95" in man["branches"]["jet_pt"])
check("policy default pad_max", man["branches"]["jet_pt"]["policy"] == "pad_max")

# ----------------------------------------------------------------------
# 4. convert.py QUICK mode (2a)
# ----------------------------------------------------------------------
print("\n[e2e] convert.py quick (2a)")
q_out = os.path.join(WORK, "t2_quick")
r = subprocess.run([sys.executable, os.path.join(REPO, "convert.py"), rootpath,
                    "--name", "t2", "--out", q_out, "--tag", "is_test=1"],
                   capture_output=True, text=True)
print(r.stdout[-400:])
check("quick exit 0", r.returncode == 0)
csv_q = os.path.join(q_out, "t2_flat.csv")
check("quick CSV exists", os.path.isfile(csv_q))
df = pd.read_csv(csv_q, dtype=str)
jmax = int(max(jet_counts))
check("rows == N", len(df) == N)
check(f"jet_pt exploded to {jmax} cols",
      all(f"jet_pt_{j}" in df.columns for j in range(jmax)))
check("sentinel 'x' present in pads", (df[f"jet_pt_{jmax-1}"] == "x").any())
check("vec1 collapsed (no _0 suffix)",
      "msvtx_eta" in df.columns and "msvtx_eta_0" not in df.columns)
check("tag column stamped", (df["is_test"] == "1").all())
check("empty_b excluded", not any(c.startswith("empty_b") for c in df.columns))
# spot-check a padded value round-trips
ev3 = ak.to_list(jet_pt[3])
got = df.loc[3, "jet_pt_0"]
check("value round-trip ev3 jet_pt_0",
      (len(ev3) == 0 and got == "x") or
      (len(ev3) > 0 and abs(float(got) - ev3[0]) < 1e-4))

# ----------------------------------------------------------------------
# 5. Policy edit + convert.py FROM-SCAN mode (2b)
# ----------------------------------------------------------------------
print("\n[e2e] convert.py from-scan (2b) with policy edits")
mpath = os.path.join(scan_out, "t1_manifest.json")
man = json.load(open(mpath))
man["branches"]["jet_pt"]["policy"]   = "first:2"
man["branches"]["track_d0"]["policy"] = "drop"
json.dump(man, open(mpath, "w"), indent=1)

r = subprocess.run([sys.executable, os.path.join(REPO, "convert.py"),
                    "--from-scan", scan_out, "--fill", "nan"],
                   capture_output=True, text=True)
print(r.stdout[-400:])
check("from-scan exit 0", r.returncode == 0)
csv_f = os.path.join(scan_out, "t1_flat.csv")
check("from-scan CSV exists", os.path.isfile(csv_f))
df2 = pd.read_csv(csv_f)
check("first:2 honored", "jet_pt_0" in df2.columns and "jet_pt_1" in df2.columns
      and "jet_pt_2" not in df2.columns)
check("drop honored", not any(c.startswith("track_d0") for c in df2.columns))
check("nan fill -> numeric dtype", pd.api.types.is_float_dtype(df2["jet_pt_1"]))
check("tags carried from scan manifest", "is_test" in df2.columns)
check("build report written",
      os.path.isfile(os.path.join(scan_out, "t1_build.txt")))

# ----------------------------------------------------------------------
# 6. RNTuple format scan (future-proofing)
# ----------------------------------------------------------------------
print("\n[e2e] scan.py on RNTuple file")
rnt_out = os.path.join(WORK, "t3_rnt")
r = subprocess.run([sys.executable, os.path.join(REPO, "scan.py"), rnt_path,
                    "--name", "t3", "--out", rnt_out, "--no-parquet"],
                   capture_output=True, text=True)
check("rntuple scan exit 0", r.returncode == 0)
if r.returncode == 0:
    man3 = json.load(open(os.path.join(rnt_out, "t3_manifest.json")))
    check("rntuple jet_pt jagged", man3["branches"]["jet_pt"]["category"] == "jagged")
else:
    print(r.stdout[-300:], r.stderr[-300:])
    check("rntuple jet_pt jagged", False)

# ----------------------------------------------------------------------
print(f"\n{'='*50}\n  RESULTS: {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
