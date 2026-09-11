TEST 1 — first live vet (atlng02, 2026-07-22)
Sample: HSS_mH125_mS55_ct5320_537840_mc23e_fullsim.root (423 events)

Committed here: the metadata artifacts only.
  v1_manifest.json   the scan's full census + policies (93 KB)
  v1_manifest.txt    the human audit (34 KB)
  v1_build.txt       the build receipt (481 B)

NOT committed (intentionally): v1_flat.csv (85.6 MB) and
v1_canonical.parquet (8.9 MB) — real ATLAS MC event content stays off
the public repo, and 91 MB doesn't belong in clone history. They live
server-side:  ~/r2c_vet/v1_scan  on atlng02 (scp to fetch).
