# CMFCore move optimization — benchmark report

Measures the effect of the `Products.CMFCore` move optimization (branch
`move_optimization`) when moving a folder with many children in a real Plone 6
site. On `IObjectMovedEvent` the optimization preserves the catalog RID and
reindexes only the *context-aware* indexes (`path`, `getId`, `id`,
`allowedRolesAndUsers`) instead of doing a full `unindexObject()` +
`indexObject()` that recomputes **all** indexes.

## Summary

Moving a folder of **10,000** objects:

| scenario | baseline | optimized | speedup | time saved |
|---|--:|--:|--:|--:|
| rename (`manage_renameObject`) | 72.22 s | 38.39 s | **1.88×** | **46.8 %** |
| cut + paste | 74.37 s | 40.21 s | **1.85×** | **45.9 %** |

The optimization eliminates the unindex pass entirely and reindexes ~4 indexes
per object instead of ~32 — an **~8× reduction in index updates**, roughly
halving wall-clock time.

## Environment

| | |
|---|---|
| Image | `plone/plone-backend:6.2` |
| Plone | 6.2.0 (Volto distribution) |
| Python | 3.13 |
| Products.CMFCore | 3.10.dev0 — branch `move_optimization` (editable, cloned from GitHub) |
| Storage | FileStorage (Data.fs), `ZODB_CACHE_SIZE=50000` |
| Dataset | `/Plone/bigfolder` with N `Document` objects; cut/paste target `/Plone/dest` |
| Host | 30 GiB RAM |

## Methodology

- **Single binary, single variable.** Baseline and optimized run on the *same*
  image, branch and dataset. The only difference is whether the two
  `IContextAwareIndexProvider` utilities (`cmf.location`, `cmf.security`) are
  registered. With them unregistered, `handleContentishEvent` executes the exact
  original `unindex` + `index` path. This isolates the feature itself.
- **What is timed.** The move operation plus the catalog-queue flush
  (`getQueue().process()`), i.e. the actual indexing work — not just enqueuing.
- **Repeatable.** Each measurement runs in a fresh `zconsole` process, performs
  one move, then `transaction.abort()` — the dataset stays pristine, so every
  run starts from identical state.
- **Instrumentation.** `Products.ZCatalog.Catalog.Catalog.catalogObject` /
  `uncatalogObject` are wrapped to count calls and total index-attribute updates;
  `CatalogTool.moveObject` is counted when present.
- A folder move dispatches the move events to **every descendant**
  (`OFS.subscribers` → `dispatchToSublocations`), so `handleContentishEvent`
  runs once per child — this is what makes a big-folder move expensive.

## Results — 10,000 objects

```
scenario  mode         N        seconds  catalog_object  uncatalog_object  idx_updates  move_object
rename    baseline     10000     72.219           10003             10001       320065            0
rename    optimized    10000     38.394           10003                 0        40037        10001
cutpaste  baseline     10000     74.371           10003             10001       320096            0
cutpaste  optimized    10000     40.210           10003                 0        40068        10001
```

### Interpretation

- **Index updates:** baseline ≈ `320065 / 10003 ≈ 32` index updates per object
  (the catalog has ~32 indexes); optimized ≈ `40037 / 10003 ≈ 4` — exactly the
  four context-aware indexes. → **~8× fewer index writes.**
- **Unindex pass removed:** baseline issues ~10,000 `uncatalogObject` calls (each
  also removing the object from *every* index); optimized issues **zero**. The
  real reduction in catalog work is therefore larger than the 8× visible in
  `idx_updates`, because the baseline's unindex side is not counted there.
- **Wall-clock vs index work:** time drops ~1.9× while index updates drop ~8×.
  The gap is fixed per-object overhead that the optimization does not remove:
  loading each descendant, dispatching the events, the rename/paste itself, and —
  notably — `CatalogTool.moveObject` calling `getQueue().process()` **once per
  descendant** (10,001 flushes). This repeated flushing is the prime candidate
  for further tuning (hoist the flush out of the per-object path).

## Results — 100,000 objects

_Pending — see "100k run" below._

## Reproduce

```bash
./run.sh                 # 10k by default
N=100000 ./run.sh        # 100k
```

See [README.md](README.md) for details and the optional real-branch comparison.
