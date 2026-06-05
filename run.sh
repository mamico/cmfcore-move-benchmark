#!/usr/bin/env bash
#
# Orchestrate the CMFCore move-optimization benchmark in a plone/plone-backend
# container.  Idempotent: first run sets everything up; later runs just re-bench.
#
#   ./run.sh            # ensure container + site + dataset, then run 4 measurements
#   REBUILD=1 ./run.sh  # recreate the container from scratch
#   N=20000 ./run.sh    # different dataset size
#   ./run.sh down       # remove the container
#
set -euo pipefail

IMAGE="${IMAGE:-plone/plone-backend:6.2}"
CONTAINER="${CONTAINER:-cmfbench}"
N="${N:-10000}"
REPO="${REPO:-https://github.com/zopefoundation/Products.CMFCore.git}"
BRANCH="${BRANCH:-move_optimization}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRCDIR="$HERE/_src/Products.CMFCore"
DATADIR="$HERE/_data"
CONF="etc/zope.conf"
ZRUN="/app/bin/zconsole run $CONF"

# zope.conf uses $(...) substitutions that the image entrypoint normally exports
# at runtime; set them at `docker run` so every `docker exec` inherits them.
ZOPE_ENV=(
  -e SECURITY_POLICY_IMPLEMENTATION=C
  -e VERBOSE_SECURITY=off
  -e DEBUG_MODE=off
  -e DEFAULT_ZPUBLISHER_ENCODING=utf-8
  -e ZODB_CACHE_SIZE=50000
  -e ZOPE_FORM_MEMORY_LIMIT=4MB
  -e ZOPE_FORM_DISK_LIMIT=1GB
  -e ZOPE_FORM_MEMFILE_LIMIT=4MB
  -e CLIENT_HOME=/data/client
)

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

if [[ "${1:-}" == "down" ]]; then
  docker rm -f "$CONTAINER" 2>/dev/null || true
  log "Removed container $CONTAINER (data kept in $DATADIR)"
  exit 0
fi

if [[ "${REBUILD:-}" == "1" ]]; then
  docker rm -f "$CONTAINER" 2>/dev/null || true
fi

# --- container -------------------------------------------------------------
if ! docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  if [[ ! -d "$SRCDIR/.git" ]]; then
    log "Cloning $REPO ($BRANCH)"
    rm -rf "$SRCDIR"; mkdir -p "$(dirname "$SRCDIR")"
    git clone --branch "$BRANCH" "$REPO" "$SRCDIR"
  else
    log "Updating clone to $BRANCH"
    git -C "$SRCDIR" fetch --quiet origin
    git -C "$SRCDIR" checkout --quiet "$BRANCH"
    git -C "$SRCDIR" pull --quiet --ff-only || true
  fi

  log "Starting idle container $CONTAINER from $IMAGE"
  mkdir -p "$DATADIR"; chmod 777 "$DATADIR"
  docker run -d --name "$CONTAINER" \
    "${ZOPE_ENV[@]}" \
    -v "$SRCDIR":/app/src/Products.CMFCore \
    -v "$HERE":/app/scripts-bench \
    -v "$DATADIR":/data \
    "$IMAGE" sleep infinity >/dev/null

  docker exec "$CONTAINER" mkdir -p /data/client

  log "Installing editable CMFCore (--no-deps)"
  docker exec "$CONTAINER" /app/bin/pip install -e /app/src/Products.CMFCore --no-deps -q
  echo -n "CMFCore in use: "
  docker exec "$CONTAINER" /app/bin/python -c "import Products.CMFCore as m; print(m.__file__)"

  log "Creating Plone site /Plone"
  docker exec -e SITE_ID=Plone -e TYPE=volto -e DELETE_EXISTING=false \
    "$CONTAINER" $ZRUN /app/scripts/create_site.py
elif ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  log "Restarting existing container $CONTAINER"
  docker start "$CONTAINER" >/dev/null
fi

# --- dataset ---------------------------------------------------------------
log "Ensuring dataset of $N objects (idempotent)"
docker exec "$CONTAINER" $ZRUN /app/scripts-bench/benchmark_move.py setup "$N"

# --- measurements ----------------------------------------------------------
log "Running measurements"
RESULTS=()
for scenario in rename cutpaste; do
  for mode in baseline optimized; do
    flag=""; [[ "$mode" == "baseline" ]] && flag="--baseline"
    line="$(docker exec "$CONTAINER" $ZRUN \
      /app/scripts-bench/benchmark_move.py bench --scenario "$scenario" $flag \
      2>/dev/null | grep '^RESULT' || true)"
    echo "$line"
    RESULTS+=("$line")
  done
done

# --- summary ---------------------------------------------------------------
log "Summary"
printf '%-9s %-10s %8s %9s  %12s %14s %11s %11s\n' \
  scenario mode N seconds catalog_obj uncatalog_obj idx_updates move_object
printf -- '---------------------------------------------------------------------------------------------\n'
declare -A SECS
for r in "${RESULTS[@]}"; do
  [[ -z "$r" ]] && continue
  eval "${r#RESULT }"   # sets scenario= mode= N= seconds= catalog_object= ...
  printf '%-9s %-10s %8s %9s  %12s %14s %11s %11s\n' \
    "$scenario" "$mode" "$N" "$seconds" \
    "$catalog_object" "$uncatalog_object" "$idx_updates" "$move_object"
  SECS["$scenario/$mode"]="$seconds"
done

echo
for scenario in rename cutpaste; do
  b="${SECS[$scenario/baseline]:-}"; o="${SECS[$scenario/optimized]:-}"
  if [[ -n "$b" && -n "$o" ]]; then
    awk -v s="$scenario" -v b="$b" -v o="$o" 'BEGIN{
      printf "%-9s baseline=%.3fs optimized=%.3fs  speedup=%.2fx  saved=%.1f%%\n",
             s, b, o, b/o, (b-o)/b*100 }'
  fi
done
