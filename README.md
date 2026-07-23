# root2csv

Two-layer ROOT→CSV tooling for ATLAS ntuples. Layer 1 scans a file and
classifies every branch (scalar / vec / jagged / jagged-deep / unreadable)
into a manifest; Layer 2 flattens to CSV — quick single-pass, or
manifest-driven full unfold. Pure uproot/awkward, no ROOT build needed,
headless-friendly.

Lineage: `flatten_events.py` (2026-04-21), the events.root two-pass
flattener. Design lock-in: `SPEC.txt`.

## Install (headless server, via clone)

```bash
git clone <REPO_URL>
cd root2csv
pip install --user -r requirements.txt     # or into a venv
```

No ATLAS environment, no ROOT build, no display needed.

## Fastest path: one file → one CSV

```bash
python3 convert.py /data2/kjohns/run3_sample_fastframe_files/<file>.root
```

That's it. Outputs land in `./<name>_scan/`:

| file | what it is |
|---|---|
| `<name>_flat.csv` | the flat CSV (jagged branches exploded to `_0…_N` columns, short events padded with the `-999` flag) |
| `<name>_canonical.parquet` | bulk storage — everything readable, lists kept as lists; regenerate CSVs from this without re-reading ROOT |
| `<name>_manifest.txt` | human-readable audit — what every branch is and what happened to it (Ctrl-F any branch name) |
| `<name>_manifest.json` | machine-readable census; edit per-branch policies here |

Useful options: `--tree reco` (default: auto-picks the tree with most
entries) · `--name mySample` · `--out DIR` · `--tag is_signal=1`
(stamp an integer column on every row, repeatable) ·
`--fill x` (legacy events.root sentinel) · `--fill nan` (NaN pads).

## The two layers

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
SPEC.txt      stage-A design lock-in (read before changing anything)
index.html    front-end home (TRON light) — the only page at root
pages/        about / tier1 / tier2 / tier3 / bins / glossary
assets/       css/tron_light.css · js/site.js · images/ (logo, mark,
              icon, tier screenshots)
```
