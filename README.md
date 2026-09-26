# CMFCore move-optimization benchmark

Measures the effect of the `Products.CMFCore` move optimization (branch
`move_optimization`) when moving a folder with thousands of children in a real
Plone 6 site, running in an isolated `plone/plone-backend` container.

**Optimization under test:** on `IObjectMovedEvent`, instead of a full
`unindexObject()` + `indexObject()` (recomputing *all* catalog indexes), the
catalog RID is preserved and only the registered *context-aware* indexes are
reindexed.

📊 **Results:** see [BENCHMARK.md](BENCHMARK.md) — moving a 10k-object folder is
~1.9× faster with ~8× fewer index writes.

## Requirements

- Docker
- Network access to clone the CMFCore repo

## Run

```bash
./run.sh                 # ensure container + Plone site + 10k-object dataset, then 4 measurements
N=20000 ./run.sh         # bigger dataset
PDF=1 N=100 ./run.sh     # random 40-60 page PDF File dataset, then rename measurements
REBUILD=1 ./run.sh       # recreate the container from scratch
BRANCH=move_optimization ./run.sh
IMAGE=plone/plone-backend:6.2 ./run.sh
./run.sh down            # remove the container (the dataset in ./_data is kept)
```

On first run it **clones** `https://github.com/zopefoundation/Products.CMFCore.git`
(branch `move_optimization`, override with `REPO=` / `BRANCH=`) into `./_src`,
installs it editable in the container (`pip install -e --no-deps`), creates a Volto
Plone site, and builds `/Plone/bigfolder` with `N` Documents plus an empty
`/Plone/dest`. Later runs reuse the persisted container/dataset and just
re-measure (fast).

With `PDF=1`, `run.sh` also ensures the Python `lorem` package is installed,
builds `/Plone/bigfilefolder` with `N` random 40-60 page PDF `File`
objects, and runs the rename scenario against that folder (`baseline` and
`optimized`).

## What it measures

By default, four runs — `{rename, cutpaste} × {baseline, optimized}` — each
timing the move plus the catalog-queue flush, then aborting the transaction so
the dataset stays pristine and repeatable. With `PDF=1`, the measurements are
`{rename} × {baseline, optimized}` against `/Plone/bigfilefolder`.

- **rename** — `manage_renameObject('bigfolder', 'bigfolder_moved')`
- **cutpaste** — cut `bigfolder`, paste into `/Plone/dest`
- **baseline** — unregisters the registered `IContextAwareIndexProvider`
  utilities, so `handleContentishEvent` runs the original `unindex` + `index`
  path
- **optimized** — utilities registered (default), so the `moveObject` path runs

### Reading the output

| column | baseline | optimized |
|---|---|---|
| `uncatalog_object` | ~N (every child unindexed) | 0 |
| `catalog_object` | ~N (full re-index) | ~N (but only context-aware idxs) |
| `idx_updates` | N × *all* indexes | N × registered context-aware indexes | 
| `searchable_words` | unique terms in `SearchableText` lexicon | same dataset vocabulary |
| `seconds` | higher | lower |
| `move_object` | 0 | ~N |

The summary prints `speedup` and `% saved` per scenario.

> **Note — real sites should benefit even more.** This dataset uses empty
> `Document` objects, so recomputing the `SearchableText` index is trivial. On a
> full reindex (the baseline), `SearchableText` extraction is one of the most
> expensive steps for content holding Word/PDF/Office/HTML payloads: it runs
> full-text extraction through `portal_transforms` (and external tooling). The
> optimization never touches `SearchableText` on a move, so the more such content
> a moved subtree contains, the larger the real-world saving versus the
> ~1.9× measured here on cheap-to-index objects.

## Why the toggle is a faithful baseline

The optimization is gated by the registered providers. With them unregistered,
`handleContentishEvent` executes the exact original code path. This keeps image,
CMFCore version, ZCML and dataset identical across the two runs — the only
variable is the feature itself.

## Optional: true git-branch comparison

For literal "original vs modified" fidelity, compare the branches directly. The
editable install reads the mounted clone live and `zconsole` reloads code + ZCML
on every invocation, so no reinstall is needed — just check out a branch in the
clone (`./_src/Products.CMFCore`) and re-run a measurement:

```bash
git -C ./_src/Products.CMFCore checkout master
docker exec -e BENCH_CMD=bench -e BENCH_SCENARIO=rename cmfbench \
  /app/bin/zconsole run etc/zope.conf /app/scripts-bench/benchmark_move.py

git -C ./_src/Products.CMFCore checkout move_optimization
docker exec -e BENCH_CMD=bench -e BENCH_SCENARIO=rename cmfbench \
  /app/bin/zconsole run etc/zope.conf /app/scripts-bench/benchmark_move.py
```

On `master` the providers don't exist, so the run is inherently baseline
(`--baseline` is a no-op there).

## Caveat surfaced by the benchmark

`CatalogTool.moveObject` calls `getQueue().process()` once **per descendant**.
For very large folders this repeated flushing may add overhead; the `seconds`
column versus `idx_updates` makes it visible and is useful input for tuning
(e.g. hoisting the flush out of the per-object path).

## Files

- `benchmark_move.py` — `zconsole` script (`setup` / `bench`), instrumentation, toggle, optional PDF dataset
- `run.sh` — clone + container orchestration + summary
- `_src/` — cloned CMFCore checkout (created on first run)
- `_data/` — persisted Plone `Data.fs` (created on first run)
