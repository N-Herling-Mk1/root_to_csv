#!/usr/bin/env bash
# =============================================================================
# batch.sh — run the whole pipeline over many ROOT files, one after another
# =============================================================================
#
#   For every .root file given (or found in a given directory):
#     convert.py   <file>.root  ->  <out>/<name>_scan/<name>_flat.csv  (+ parquet, manifests)
#     dropfilter.py              ->  <out>/<name>_scan/<name>_filtered.csv (+ drop report)
#   where <name> is the ROOT file's stem.
#
#   USAGE
#     bash root_to_csv/batch.sh /data2/kjohns/run3_sample_fastframe_files/ml-NEW/
#     bash root_to_csv/batch.sh --out ./csv_new  /path/to/dir/
#     bash root_to_csv/batch.sh /path/a.root /path/b.root
#     bash root_to_csv/batch.sh /path/to/dir/HSS_*.root          # shell glob
#
#   OPTIONS
#     --out DIR         where the <name>_scan/ folders go   (default: current dir)
#     --force           redo files that already have a finished output
#     --no-filter       stop after convert.py (flat.csv only)
#     --fill VALUE      passed to convert.py (default there: -999)
#     --drop-file PATH  passed to dropfilter.py (default: repo drop_list.txt)
#     -h | --help       this text
#
#   BEHAVIOUR
#     - A directory is searched one level deep for *.root (not recursive).
#     - One file failing does not stop the batch; failures are listed at the end
#       and the script exits 1.
#     - Re-running skips any file whose <name>_filtered.csv (or _flat.csv with
#       --no-filter) already exists, so an interrupted batch resumes where it
#       stopped. --force redoes everything.
#     - Full per-file output goes to <out>/batch_logs/<name>.log; the terminal
#       shows one status line per file plus a running total.
#     - A summary table is written to <out>/batch_logs/summary_<date>.txt.
#
#   LONG BATCHES ON A SERVER — keep it alive after you log out:
#     nohup bash root_to_csv/batch.sh /path/to/dir/ > batch.out 2>&1 &
#     tail -f batch.out                  # watch it; Ctrl-C stops watching only
#   (or run it inside tmux / screen)
# =============================================================================

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

OUT="."
FORCE=0
NOFILTER=0
FILL=""
DROPFILE=""
INPUTS=()

usage() { awk '/^# =====/{n++; if(n==3) exit; next} n>=1{sub(/^# ?/,""); print}' "${BASH_SOURCE[0]}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)        OUT="${2:?--out needs a directory}"; shift 2 ;;
    --force)      FORCE=1; shift ;;
    --no-filter)  NOFILTER=1; shift ;;
    --fill)       FILL="${2:?--fill needs a value}"; shift 2 ;;
    --drop-file)  DROPFILE="${2:?--drop-file needs a path}"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    -*)           echo "unknown option: $1  (try --help)" >&2; exit 3 ;;
    *)            INPUTS+=("$1"); shift ;;
  esac
done

if [[ ${#INPUTS[@]} -eq 0 ]]; then
  echo "give a directory of .root files, or the files themselves  (try --help)" >&2
  exit 3
fi

# ---- collect the file list ---------------------------------------------------
shopt -s nullglob
FILES=()
for p in "${INPUTS[@]}"; do
  if [[ -d "$p" ]]; then
    for f in "${p%/}"/*.root; do FILES+=("$f"); done
  elif [[ -f "$p" && "$p" == *.root ]]; then
    FILES+=("$p")
  else
    echo "  [skip] not a .root file or directory: $p" >&2
  fi
done
shopt -u nullglob

N=${#FILES[@]}
if [[ $N -eq 0 ]]; then
  echo "no .root files found in: ${INPUTS[*]}" >&2
  exit 3
fi

# duplicate stems would write into the same <name>_scan/ — refuse up front
DUPES="$(for f in "${FILES[@]}"; do basename "$f" .root; done | sort | uniq -d)"
if [[ -n "$DUPES" ]]; then
  echo "these file names appear more than once (outputs would collide):" >&2
  echo "$DUPES" | sed 's/^/   /' >&2
  echo "run the directories separately with different --out folders." >&2
  exit 3
fi

mkdir -p "$OUT/batch_logs" || exit 3
OUT="$(cd "$OUT" && pwd)"
LOGDIR="$OUT/batch_logs"
SUMMARY="$LOGDIR/summary_$(date +%Y%m%d_%H%M%S).txt"

# ---- helpers -----------------------------------------------------------------
TTY=0; [[ -t 1 ]] && TTY=1
CHILD=""
trap 'echo; echo "interrupted - stopping."; [[ -n "$CHILD" ]] && kill "$CHILD" 2>/dev/null; exit 130' INT TERM

fmt_t() { local s=$1; printf "%dm%02ds" $((s/60)) $((s%60)); }

# run a command into the log; on a terminal show a live elapsed counter
run_step() {   # run_step "<label>" <log> cmd...
  local label="$1" log="$2"; shift 2
  local t0=$SECONDS
  "$@" >>"$log" 2>&1 &
  CHILD=$!
  if [[ $TTY -eq 1 ]]; then
    local spin='|/-\' i=0
    while kill -0 "$CHILD" 2>/dev/null; do
      printf "\r      %-8s %s %s " "$label" "${spin:i++%4:1}" "$(fmt_t $((SECONDS-t0)))"
      sleep 1
    done
  fi
  wait "$CHILD"; local rc=$?
  CHILD=""
  STEP_T=$((SECONDS-t0))
  [[ $TTY -eq 1 ]] && printf "\r\033[K"
  return $rc
}

shape() {   # rows x cols, MB of a csv
  local f="$1"
  local cols rows mb
  cols=$(head -1 "$f" | tr ',' '\n' | wc -l)
  rows=$(( $(wc -l < "$f") - 1 ))
  mb=$(awk -v b="$(stat -c %s "$f")" 'BEGIN{printf "%.1f", b/1048576}')
  printf "%s x %s  %s MB" "$rows" "$cols" "$mb"
}

bar() {   # overall progress bar
  local done=$1 total=$2 w=30
  local fill=$(( done * w / total ))
  printf "[%s%s] %d/%d" "$(printf '%*s' $fill '' | tr ' ' '#')" \
         "$(printf '%*s' $((w-fill)) '' | tr ' ' '-')" "$done" "$total"
}

# ---- run ---------------------------------------------------------------------
echo "======================================================================"
echo "  root2csv BATCH   $N file(s)"
echo "  output   : $OUT/<name>_scan/"
echo "  logs     : $LOGDIR/"
[[ $NOFILTER -eq 1 ]] && echo "  filter   : OFF (--no-filter)"
[[ -n "$FILL" ]]      && echo "  fill     : $FILL"
[[ -n "$DROPFILE" ]]  && echo "  drop file: $DROPFILE"
echo "======================================================================"

CONV_ARGS=(); [[ -n "$FILL" ]] && CONV_ARGS+=(--fill "$FILL")
FILT_ARGS=(); [[ -n "$DROPFILE" ]] && FILT_ARGS+=(--drop-file "$DROPFILE")

declare -a R_NAME R_STATUS R_SHAPE
NOK=0; NSKIP=0; NFAIL=0
T_ALL=$SECONDS
I=0

for f in "${FILES[@]}"; do
  I=$((I+1))
  name="$(basename "$f" .root)"
  sdir="$OUT/${name}_scan"
  flat="$sdir/${name}_flat.csv"
  filt="$sdir/${name}_filtered.csv"
  final="$filt"; [[ $NOFILTER -eq 1 ]] && final="$flat"
  log="$LOGDIR/${name}.log"

  printf "[%*d/%d] %s\n" ${#N} "$I" "$N" "$name"

  if [[ $FORCE -eq 0 && -f "$final" ]]; then
    echo "      skip     already done -> $(basename "$final")"
    R_NAME+=("$name"); R_STATUS+=("SKIP"); R_SHAPE+=("$(shape "$final")")
    NSKIP=$((NSKIP+1))
    continue
  fi

  : > "$log"
  echo "### $(date '+%F %T')  $f" >>"$log"

  if run_step convert "$log" "$PY" "$HERE/convert.py" "$f" --name "$name" --out "$sdir" "${CONV_ARGS[@]}"; then
    echo "      convert  ok   $(fmt_t $STEP_T)   $(shape "$flat")"
  else
    echo "      convert  FAIL $(fmt_t $STEP_T)   see $log"
    R_NAME+=("$name"); R_STATUS+=("FAIL convert"); R_SHAPE+=("-")
    NFAIL=$((NFAIL+1))
    continue
  fi

  if [[ $NOFILTER -eq 0 ]]; then
    if run_step filter "$log" "$PY" "$HERE/dropfilter.py" "$sdir" "${FILT_ARGS[@]}"; then
      warn=""
      grep -q "^WARNING - " "$log" && warn="   (some drop-list names not in this file - see log)"
      echo "      filter   ok   $(fmt_t $STEP_T)   $(shape "$filt")$warn"
    else
      echo "      filter   FAIL $(fmt_t $STEP_T)   see $log"
      R_NAME+=("$name"); R_STATUS+=("FAIL filter"); R_SHAPE+=("$(shape "$flat") (flat)")
      NFAIL=$((NFAIL+1))
      continue
    fi
  fi

  R_NAME+=("$name"); R_STATUS+=("OK"); R_SHAPE+=("$(shape "$final")")
  NOK=$((NOK+1))

  el=$((SECONDS-T_ALL)); left=$(( el * (N-I) / I ))
  echo "      $(bar "$I" "$N")   elapsed $(fmt_t $el)   ~$(fmt_t $left) left"
done

# ---- summary -----------------------------------------------------------------
{
  echo "root2csv batch summary   $(date '+%F %T')"
  echo "output dir : $OUT"
  echo "files      : $N   ok $NOK   skipped $NSKIP   failed $NFAIL"
  echo "elapsed    : $(fmt_t $((SECONDS-T_ALL)))"
  echo
  printf "%-6s  %-14s  %s\n" "#" "status" "name  ->  rows x cols  size"
  for k in "${!R_NAME[@]}"; do
    printf "%-6s  %-14s  %s  ->  %s\n" "$((k+1))" "${R_STATUS[$k]}" "${R_NAME[$k]}" "${R_SHAPE[$k]}"
  done
} > "$SUMMARY"

echo "======================================================================"
cat "$SUMMARY"
echo "======================================================================"
echo "  summary saved: $SUMMARY"

[[ $NFAIL -eq 0 ]] || exit 1
exit 0
