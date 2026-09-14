# root2csv — dropfilter.py · field failure and fix

**Reported:** 2026-09-12, Prof. Johns, atlng02
**Fixed in:** v43 · `dropfilter 0.1.0`
**Severity:** crash on write. No data corruption — the inputs are opened read-only.
**Status:** fixed and verified on the failing code path. Not yet verified on the reporter's file.

---

## 1 · What happened

`dropfilter.py` crashed partway through writing the filtered CSV.

```
  writing filtered csv   [----------------------------------]   0.0%    0.0s
Traceback (most recent call last):
  File ".../dropfilter.py", line 906, in <module>
    sys.exit(main())
  File ".../dropfilter.py", line 803, in main
    rows = write_filtered_from_csv(src, out_path, idx, args.quiet)
  File ".../dropfilter.py", line 449, in write_filtered_from_csv
    seen_bytes = fin.tell() if hasattr(fin, "tell") else seen_bytes
OSError: telling position disabled by next() call
```

The run reached this point normally: 227 branches read from the manifest, 117
drop-list entries resolved, 49,641 source columns identified. The failure is in
the write loop, not in the resolution.

---

## 2 · The cause

The progress bar was sized in **bytes** and asked the source handle for its
position:

```python
total = max(os.path.getsize(src), 1)          # bytes
...
reader = csv.reader(fin)
for row in reader:
    ...
    if rows % 500 == 0:
        seen_bytes = fin.tell()               # <-- raises here
```

Python disables `tell()` on a file object once `next()` has been called on it,
which is exactly what `csv.reader` does to iterate. The call raises
`OSError: telling position disabled by next() call`.

**This is a progress-reporting line, not a data line.** No filtering logic was
wrong. The crash simply kills the process mid-write.

---

## 3 · Why every test we ran passed

The faulty statement sits behind `if rows % 500 == 0`.

| | rows | reaches row 500? | executes `tell()`? |
|---|---|---|---|
| Vet file `HSS_mH125_mS55` | 423 | no | **never** |
| Reporter's file | ≥ 500 | yes | yes — crash |

Every local and server test to that point used the 423-row vet file. The line
was never executed once. The code path had **zero** coverage while appearing
fully tested.

The same `tell()`-after-`next()` fault had been found and fixed in
`verify_written()` one version earlier (v38). The rest of the file was not
audited for the same pattern at that time. That is the process failure behind
this defect: an instance was fixed instead of the class.

---

## 4 · The fix

Progress is now counted in **rows**, never byte offsets. A new `count_rows()`
does a fast newline count over the file before the write begins.

```python
def count_rows(path, quiet=False):
    """Data rows in a CSV, by counting newlines in raw chunks."""
    n = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            n += chunk.count(b"\n")
    return max(n - 1, 0)          # minus the header
```

```python
total = max(count_rows(src), 1)               # rows
...
    if rows % 500 == 0:
        bar.step(500)                         # no handle interrogation
```

A newline inside a quoted field would over-count the total. That is cosmetic —
it can only make the bar finish early, and cannot affect the output.

Audited: no `.tell()` call remains anywhere in `dropfilter.py`, `scan.py`,
`convert.py` or `common.py`.

Recorded as **SPEC decision 17** so it cannot be reintroduced, including the
coverage lesson: *a test set whose row count is below the progress interval
cannot exercise the progress path — vet files must exceed it.*

---

## 5 · What was tested

### Pre-fix reproduction

The original logic was run against a 501-row file and produced the reported
error at the expected point:

```
  pre-fix code dies at row 500: OSError: telling position disabled by next() call
```

Same exception, same message, same trigger as the field report.

### Post-fix, synthetic — boundary sweep

Fixtures built from the real `v1_manifest.json`, so column shape is genuine
(24,925 columns); only row count varies.

| rows | exit | columns | rows verified | FAILs |
|---|---|---|---|---|
| 499 | 0 | 85 | 499 | 0 |
| 500 | 0 | 85 | 500 | 0 |
| 501 | 0 | 85 | 501 | 0 |
| 1500 | 0 | 85 | 1,500 | 0 |

### Post-fix, atlng02 — real data

**Run A — regression, 423 rows.** `HSS_mH125_mS55_ct5320_537840_mc23e_fullsim.root`
via quick convert. Confirms nothing that worked was broken. *Does not touch the
repaired code path.*

```
  columns    24,925 -> 85   (24,840 removed, 99.7%)
  wrote      423 x 85 -> 09_14_26_125627_filtered.csv  (0.2 MB)
  VERIFICATION — 7 OK, 0 FAIL
```

**Run B — the actual test, 846 rows.** `v2_flat.csv` concatenated onto itself to
cross the 500-row threshold twice. This executes the statement that failed.

```
  columns    24,925 -> 85   (24,840 removed, 99.7%)
  wrote      846 x 85 -> 09_14_26_125713_filtered.csv  (0.5 MB)
  writing filtered csv   [##################################] 100.0%    2.7s
  VERIFICATION
    OK    columns written    85  (expected 85)
    OK    rows written       846  (source had 846)
    OK    rectangular        every row the same width
    OK    dropped columns    none present in the output
    OK    source flat.csv    UNCHANGED (size+mtime+md5)
    OK    manifest.json      UNCHANGED (size+mtime+md5)
```

The progress bar drove to 100% over 2.7s, so the row-counted replacement works
rather than merely not crashing.

The doubled file is **physics nonsense by construction** — every event appears
twice. It exercises the write path and row bookkeeping only, and says nothing
about the data.

### Incidental coverage gained

`canonical.parquet (not present)` rendered as `--`, not `FAIL` — the input
fingerprinting handles a missing optional input correctly. Not previously
exercised.

---

## 6 · What remains UNTESTED

| # | Case | Why it matters | Status |
|---|---|---|---|
| 1 | **The reporter's actual file** | 49,641 columns — roughly 2× our width. The bug was row-dependent, so width *should* be irrelevant, but we have not run his shape. | file path unknown |
| 2 | **Row counts far above 846** | We cross the 500 threshold twice. A file with 10⁴–10⁶ rows exercises the bar over thousands of steps and the write loop far longer. | never run |
| 3 | **A sample where `el_*` / `mu_*` are populated** | In the 423-event vet file those 25 branches are **empty-bin** and never reach the CSV. In a sample with reconstructed leptons they become jagged, expand, and **survive the filter** — they are not on the drop list. The output would silently be wider than 85 columns. | never run |
| 4 | **Unskimmed files (~1.6 GB)** | ~250× the skimmed size. Tests streaming behaviour and `count_rows` at scale. | never run |
| 5 | **A sample whose jagged set differs** | `category` is computed per file. A branch jagged here may be vec1 elsewhere and vice versa. The 117-name list is *this file's* structure frozen in place. | never run |
| 6 | **Edited `first:N` policies end to end** | The policy-over-`fanout` width logic is unit-tested and drift-guarded, but no full `--from-scan` run with edited policies has been filtered. | never run |
| 7 | **A genuine ragged / truncated output** | The FAIL path is proven on a hand-built bad file, not on one produced by a real partial write. | synthetic only |

**Case 3 is the one most likely to surprise someone.** It is not a bug — the
filter is doing exactly what it is told — but a drop list validated on one
sample does not automatically give the same column set on another.

---

## 7 · Action

**For the reporter:** re-clone and re-run. Nothing else changes; the scan
directory from the failed attempt is intact, because the crash happened during
the write and the inputs are opened read-only.

```bash
cd /data2/kjohns/run3_csv_from_root_files_new
rm -rf root_to_csv
git clone https://github.com/N-Herling-Mk1/root_to_csv.git
rm -f s1_scan/*_filtered.csv s1_scan/*_drop_report.txt    # discard the partial write
python3 root_to_csv/dropfilter.py ./s1_scan --verify-hash
```

**Delete any partial `*_filtered.csv` from the failed run.** It is a truncated
file that never reached the verification pass. It is not usable and should not
be mistaken for output.

Expect: `24,925`→ no — his source is 49,641 columns, so expect
`49,641 → N`, then a VERIFICATION block of all-OK lines and the filename banner.
**`N` is worth reporting.** If it is 85, the drop list generalises to his sample.
If it is larger, case 3 above has occurred and the extra columns are branches
that are empty in our vet file but populated in his.

**Information requested, for future debugging:**

1. The **full path of the ROOT file** — the six `HSS_*` samples, the two
   `data24` files, and the `unskimmed/` set all have different shapes, and we
   currently have a vetting record for exactly one of them.
2. The **event count** (`Events:` in the scan header, or `head -20
   s1_manifest.txt`).
3. The final **column count after filtering** — see above.

---

## 8 · Honest summary

The specific fault is fixed and proven on the exact code path that broke, both
in synthetic boundary tests and on real data at 846 rows.

It does **not** follow that the reporter's run will now succeed. We fixed the
crash that is visible in his traceback; the process died there, so nothing
downstream of it has ever executed on his file. Whether his run completes is
answered only by his re-run.

The broader lesson is about coverage, not this line. A 423-row vet file cannot
test anything gated at 500 rows, and no amount of repeating that test would
have found this. Test fixtures need to straddle the thresholds the code
actually branches on.
