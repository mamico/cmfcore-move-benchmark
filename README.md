# CMFCore move-optimization benchmark

Measures the effect of the `Products.CMFCore` move optimization (branch
`move_optimization`) when moving a folder with thousands of children in a real
Plone 6 site, running in an isolated `plone/plone-backend` container.

**Optimization under test:** on `IObjectMovedEvent`, instead of a full
`unindexObject()` + `indexObject()` (recomputing *all* catalog indexes), the
catalog RID is preserved and only the *context-aware* indexes (`path`, `getId`,
`id`, `allowedRolesAndUsers`) are reindexed.

## Requirements

- Docker
- This repo's sibling checkout at `../Products.CMFCore` (override with `CMFCORE=`)

## Run

```bash
./run.sh                 # ensure container + Plone site + 10k-object dataset, then 4 measurements
N=20000 ./run.sh         # bigger dataset
REBUILD=1 ./run.sh       # recreate the container from scratch
IMAGE=plone/plone-backend:6.1.4 ./run.sh
./run.sh down            # remove the container (the dataset in ./_data is kept)
```

The first run installs the mounted CMFCore editable (`pip install -e --no-deps`),
creates a Volto Plone site, and builds `/Plone/bigfolder` with `N` Documents plus
an empty `/Plone/dest`. Later runs reuse the persisted container/dataset and just
re-measure (fast).

## What it measures

Four runs — `{rename, cutpaste} × {baseline, optimized}` — each timing the move
plus the catalog-queue flush, then aborting the transaction so the dataset stays
pristine and repeatable.

- **rename** — `manage_renameObject('bigfolder', 'bigfolder_moved')`
- **cutpaste** — cut `bigfolder`, paste into `/Plone/dest`
- **baseline** — unregisters the two `IContextAwareIndexProvider` utilities, so
  `handleContentishEvent` runs the original `unindex` + `index` path
- **optimized** — utilities registered (default), so the `moveObject` path runs

### Reading the output

| column | baseline | optimized |
|---|---|---|
| `uncatalog_object` | ~N (every child unindexed) | 0 |
| `catalog_object` | ~N (full re-index) | ~N (but only context-aware idxs) |
| `idx_updates` | N × *all* indexes | N × 4 | 
| `seconds` | higher | lower |
| `move_object` | 0 | ~N |

The summary prints `speedup` and `% saved` per scenario.

## Why the toggle is a faithful baseline

The optimization is gated by the registered providers. With them unregistered,
`handleContentishEvent` executes the exact original code path. This keeps image,
CMFCore version, ZCML and dataset identical across the two runs — the only
variable is the feature itself.

## Optional: true git-branch comparison

For literal "original vs modified" fidelity, compare the branches directly. The
editable install reads the mounted source live and `zconsole` reloads code + ZCML
on every invocation, so no reinstall is needed — just check out a branch on the
host and re-run a measurement:

```bash
git -C ../Products.CMFCore checkout master
docker exec cmfbench /app/bin/zconsole run etc/zope.conf \
  /app/scripts-bench/benchmark_move.py bench --scenario rename

git -C ../Products.CMFCore checkout move_optimization
docker exec cmfbench /app/bin/zconsole run etc/zope.conf \
  /app/scripts-bench/benchmark_move.py bench --scenario rename
```

On `master` the providers don't exist, so the run is inherently baseline
(`--baseline` is a no-op there).

## Caveat surfaced by the benchmark

`CatalogTool.moveObject` calls `getQueue().process()` once **per descendant**.
For very large folders this repeated flushing may add overhead; the `seconds`
column versus `idx_updates` makes it visible and is useful input for tuning
(e.g. hoisting the flush out of the per-object path).

## Files

- `benchmark_move.py` — `zconsole` script (`setup` / `bench`), instrumentation, toggle
- `run.sh` — container orchestration + summary
- `_data/` — persisted Plone `Data.fs` (created on first run)
