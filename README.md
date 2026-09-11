# root2csv

ROOT→CSV tooling for ATLAS ntuples. **Layer 1** scans a file and classifies
every branch (scalar / vec / jagged / jagged-deep / unreadable) into a
manifest; **Layer 2** flattens to CSV — quick single-pass, or manifest-driven
full unfold. **Layer 3** is optional and trims columns by branch name, working
from layer 2's CSV. Pure uproot/awkward, no ROOT build
needed, headless-friendly.

Layers 1 and 2 are the pipeline. Layer 3 is a post-processing step you can
ignore entirely — delete its list and everything else still runs.

Lineage: `flatten_events.py` (2026-04-21), the events.root two-pass
flattener. Design lock-in: `SPEC.txt`.

## The documentation site

[![Open the root2csv documentation site](assets/images/site_banner.png)](https://n-herling-mk1.github.io/root_to_csv/)

**Live:** <https://n-herling-mk1.github.io/root_to_csv/> — the full guide:
tiers, the manifest how-to, the ignored-branch list, the six bins, glossary,
and the vetting transcript. Click the banner.

No ATLAS environment, no ROOT build, no display needed — `pip install` and go.
Deps: uproot, awkward, pandas, numpy, pyarrow. (`dropfilter.py` alone needs
none of them; it is pure stdlib.)

## Fastest path: one ROOT file → one small CSV

Four commands, start to finish. Copy-paste the whole block.

```bash
# 1 · get the toolkit
git clone https://github.com/N-Herling-Mk1/root_to_csv.git
python3 -m pip install --user -r root_to_csv/requirements.txt

# 2 · ROOT -> full flat CSV  (scan runs internally; ~15 s on a 6 MB file)
python3 root_to_csv/convert.py /data2/kjohns/run3_sample_fastframe_files/ml/<file>.root --name s1

# 3 · see what the filter would remove — writes nothing
python3 root_to_csv/dropfilter.py ./s1_scan --preview

# 4 · apply it -> a much smaller CSV, verified on the way out
python3 root_to_csv/dropfilter.py ./s1_scan
```

Run it from wherever you like — outputs land in the directory you are standing
in, not in the repo, and `drop_list.txt` is found automatically.

**What you get**, measured on `HSS_mH125_mS55_ct5320_537840_mc23e_fullsim.root`
(423 events, 6.2 MB):

| after | file | shape | size |
|---|---|---|---|
| step 2 | `s1_flat.csv` | 423 × 24,925 | 85.6 MB |
| step 4 | `<stamp>_filtered.csv` | 423 × **85** | **239 KB** |

Same rows, 99.7% fewer columns. Step 4 re-reads what it wrote and checks the
column count, the row count against the source, that every row is the same
width, that no dropped column leaked through, and that `flat.csv`, the parquet
and the manifests are all untouched — any failure exits non-zero.

Don't want the filter? Stop after step 2; `flat.csv` is a complete CSV on its
own. Want different columns? Edit `root_to_csv/drop_list.txt` and re-run step 4
— it costs seconds and needs no re-scan, no re-convert, and no ROOT file.

### Where things land

Steps 1–2 write `./<name>_scan/`:

| file | what it is |
|---|---|
| `<name>_flat.csv` | the flat CSV (jagged branches exploded to `_0…_N` columns, short events padded with the `-999` flag) |
| `<name>_canonical.parquet` | bulk storage — everything readable, lists kept as lists; regenerate CSVs from this without re-reading ROOT |
| `<name>_manifest.txt` | human-readable audit — what every branch is and what happened to it (Ctrl-F any branch name) |
| `<name>_manifest.json` | machine-readable census; edit per-branch policies here |

Step 4 (layer 3), if you run it, adds two more:

| file | what it is |
|---|---|
| `<stamp>_filtered.csv` | the CSV with every branch named in `drop_list.txt` removed. Auto-named `MM_DD_YY_HHMMSS` from the clock, so a re-run never overwrites an earlier result; `--out PATH` to choose the name yourself |
| `<stamp>_drop_report.txt` | the audit — every branch matched and how many columns it removed, every listed branch that was empty-bin, every name that matched nothing, and the verification block. Shares the CSV's base name |

### Every option

All three programs, complete. `--help` on any of them prints the same set.

| flag | scan | conv | filt | what it does |
|---|:-:|:-:|:-:|---|
| `--name NAME` | Y | Y | | sample name; every step-1/2 output inherits it. Default: the ROOT file's stem |
| `--out DIR` / `--out PATH` | Y | Y | Y | scan/convert: output *directory*. filter: output *CSV path* |
| `--tree NAME` | Y | Y | | which TTree/RNTuple to read. Default: auto-picks the one with most entries |
| `--tag KEY=VALUE` | Y | Y | | stamp an integer column on every row; repeatable |
| `--note TEXT` | Y | Y | | freeform provenance text recorded in the manifest |
| `--no-parquet` | Y | Y | | skip the canonical parquet — then `--from-scan` has nothing to rebuild from |
| `--no-samples` | Y | | | omit the two example values per branch from the manifest |
| `--from-scan DIR` | | Y | | mode 2b: build from an existing scan dir, honouring edited policies |
| `--fill x` / `nan` / `NUM` | | Y | | pad value for short jagged rows. Default `-999` |
| `--drop-file PATH` | | | Y | use a different list. Default search: scan dir, cwd, then beside `dropfilter.py` |
| `--preview` | | | Y | resolve and count only; writes nothing |
| `--report PATH` | | | Y | write the drop report somewhere other than beside the CSV |
| `--allow-unmatched` | | | Y | downgrade an unmatched name from hard error to warning |
| `--suggest-width N` | | | Y | list kept branches wider than N columns. Advisory — drops nothing |
| `--verify-hash` | | | Y | md5 the untouched inputs as well as stat them |
| `--no-verify` | | | Y | skip the post-write verification pass |
| `--quiet` | | | Y | suppress progress bars; the synopsis still prints |
| `--version` | | | Y | print the tool version and exit |

**Exit codes** (`dropfilter.py`): `0` clean · `2` a drop-list name matched no
branch, nothing written · `3` bad inputs / missing files · `4` the written CSV
failed post-write verification. Worth checking in any script that chains steps.

## The layers

**Layer 1 — scan only** (census, no CSV):

```bash
python3 scan.py <file>.root
```

**Layer 2 — convert:**

```bash
# 2a QUICK — no pre-scan, default policies (the events.root behavior)
python3 convert.py <file>.root

# 2b FROM-SCAN — edit policies first, then build from the parquet
python3 scan.py <file>.root --name s1 --out ./s1_scan
#   ...edit "policy" per branch in ./s1_scan/s1_manifest.json...
python3 convert.py --from-scan ./s1_scan
```

Jagged policies: `pad_max` (default) · `first:N` (keep first N columns —
kills outlier-driven column explosions) · `drop` (exclude from CSV).
The manifest's `len_med / len_p95 / len_max` per branch shows you where
`first:N` is worth it *before* you build.

**Layer 3 — filter (optional):**

```bash
python3 dropfilter.py ./s1_scan --preview   # resolve + count, write nothing
python3 dropfilter.py ./s1_scan             # -> MM_DD_YY_HHMMSS_filtered.csv
python3 dropfilter.py ./s1_scan --out ./s1_scan/barrel.csv    # or name it
```

Removes whole branches named in `drop_list.txt`. Reads `flat.csv` and writes a
**new** file — `flat.csv`, the manifests and the parquet are all left intact,
so changing your mind costs one re-run with an edited list. No ROOT file
needed, no re-scan, no re-convert, and no uproot: layer 3 is pure stdlib and
runs on a copied-down scan directory.

> **Why not the parquet?** `canonical.parquet` keeps lists as lists, so a
> jagged branch is *one* list-valued column there — not its `N` exploded CSV
> columns. Filtering it would remove one column per branch and leave list
> cells behind. It stays the complete master copy; it just isn't a flat table
> to filter.

### The drop list

`drop_list.txt` **ships with the repo**, at the root beside `scan.py` — there
is nothing to create. Plain text, one **branch** name per line;
`#` comments and blank lines are skipped, inline comments too.

```
# --- Jets (10) ---
jet_pt_NOSYS
jet_eta          # inline comments work
caloCluster_*    # globs resolve against manifest branch names
```

Three rules worth knowing:

- **Branch names, not column names.** Write `trackID_pt`, never
  `trackID_pt_0`. `manifest.json` expands each branch to every column it
  produced — on the vet file that one line removes 487 columns.
- **A name that matches nothing is a hard error.** Nothing is written, the run
  exits non-zero, and the offending line numbers are printed. A typo cannot
  quietly hand you a wider CSV than you asked for. Override with
  `--allow-unmatched`.
- **Matching is anchored on the whole branch name.** `trackID_eta` and
  `track_eta_NOSYS` are different families and never touch each other — but a
  sloppy glob like `track*` eats both.

The list is found automatically from any working directory. Search order,
most specific first: `<scan_dir>/drop_list.txt`, then `./drop_list.txt`, then
the copy beside `dropfilter.py`. `--drop-file` overrides all three. Delete
every copy and layer 3 becomes a clean no-op at exit 0 — layers 1 and 2 are
unaffected.

Full list and rationale: <https://n-herling-mk1.github.io/root_to_csv/pages/ignored.html>

## The six bins

| bin | meaning | CSV fate |
|---|---|---|
| scalar | one value per event | straight through |
| vec1 | length-1 vector every event | collapsed to scalar |
| jagged | variable-length vector | exploded + padded |
| jagged-deep | ndim > 2 (list-of-lists) | parquet only, never CSV |
| empty | zero-length everywhere | skipped |
| unreadable | custom C++ class uproot can't decode | name + reason recorded — that's all that can be done |

## Viewing outputs on a headless server

Three paths, easiest first:

1. **Copy the file down** (from your local machine):
   ```bash
   scp user@server:/path/to/s1_scan/s1_manifest.txt .
   ```
2. **VS Code / code-server in the browser** — see below. Open the scan
   folder, click the files.
3. **Tunnel a tiny web server** (for the stage-C HTML report, later):
   ```bash
   # on the server — 127.0.0.1 bind ONLY, never expose to the network
   python3 -m http.server 8888 --bind 127.0.0.1
   # on your local machine
   ssh -L 8888:localhost:8888 user@server
   # browse to http://localhost:8888
   ```

## code-server (browser VS Code on the headless server)

Already installed? `command -v code-server && code-server --version`

If not (no root needed):

```bash
curl -fsSL https://code-server.dev/install.sh | sh -s -- --method=standalone --prefix=$HOME/.local
export PATH="$HOME/.local/bin:$PATH"
```

Run it (loopback only):

```bash
code-server --bind-addr 127.0.0.1:8080
```

**Password:** auto-generated at first run, stored in
`~/.config/code-server/config.yaml` — read it with:

```bash
cat ~/.config/code-server/config.yaml
```

Local machine, new terminal:

```bash
ssh -L 8080:localhost:8080 user@server
```

Browser → `http://localhost:8080` → paste the password.
Download any file: right-click it in the Explorer sidebar → **Download**.

## Port / tunnel hygiene — before you log out

Rule zero: everything binds `127.0.0.1` only — the ssh tunnel is the
only door. Even so, close up:

```bash
# ON THE SERVER — anything I left listening?
lsof -iTCP -sTCP:LISTEN -a -u $USER
# empty output = clean. Otherwise:
kill <PID>                         # e.g. leftover http.server / code-server
pkill -u $USER -f vscode-server    # optional: clear lingering VS Code remotes
```

Local side: a foreground `ssh -L` tunnel dies when you close its
terminal. Stray backgrounded ones:

```bash
ps aux | grep '[s]sh -L'   # then kill <PID>
```

## Repo layout

```
common.py     shared core: six-bin classifier, profiling, progress UI, IO
scan.py       Layer 1 — census → parquet + manifest.json + manifest.txt
convert.py    Layer 2 — quick (2a) and from-scan (2b) → flat CSV
dropfilter.py Layer 3 — optional; flat.csv + drop_list.txt → filtered CSV
drop_list.txt the branch names layer 3 removes — edit this, not the code
SPEC.txt      stage-A design lock-in (read before changing anything)
index.html    front-end home (TRON light) — the only page at root
pages/        about / tier1 / ignored / tier2 / tier3 / manifest / test1 /
              bins / glossary
assets/       css/tron_light.css · js/site.js · images/ (logo, mark,
              icon, tier screenshots)
```
