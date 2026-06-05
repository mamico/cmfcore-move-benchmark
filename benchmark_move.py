"""Benchmark the Products.CMFCore move optimization.

Run inside the plone/plone-backend container via:

    zconsole run etc/zope.conf /app/scripts-bench/benchmark_move.py <cmd> [opts]

Commands:
    setup <N>                       build /Plone/bigfolder with N Documents (+ /Plone/dest)
    bench --scenario rename|cutpaste [--baseline]
                                    time the move, print a RESULT line, then abort

The script is branch-agnostic: it works on both the original (master) and the
modified (move_optimization) CMFCore.  ``--baseline`` unregisters the
IContextAwareIndexProvider utilities so that the modified code falls back to the
original ``unindex`` + ``index`` path (a no-op on master, which is baseline anyway).

``zconsole run`` injects the Zope application root as the global ``app``.
"""

import argparse
import sys
import time

import transaction
from AccessControl.SecurityManagement import newSecurityManager
from zope.component import getGlobalSiteManager
from zope.component.hooks import setSite


SITE_ID = 'Plone'
FOLDER_ID = 'bigfolder'
DEST_ID = 'dest'
PROVIDER_NAMES = ('cmf.location', 'cmf.security')


def _login_admin(app):
    admin = app.acl_users.getUserById('admin')
    if admin is None:
        raise SystemExit('No Zope "admin" user found (inituser missing?).')
    newSecurityManager(None, admin.__of__(app.acl_users))


def _get_portal(app):
    portal = getattr(app, SITE_ID, None)
    if portal is None:
        raise SystemExit(
            'Plone site %r not found. Create it first (run.sh does this).'
            % SITE_ID)
    setSite(portal)
    return portal


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------
def cmd_setup(app, n):
    _login_admin(app)
    portal = _get_portal(app)

    if FOLDER_ID not in portal.objectIds():
        portal.invokeFactory('Folder', FOLDER_ID, title='Big Folder')
    if DEST_ID not in portal.objectIds():
        portal.invokeFactory('Folder', DEST_ID, title='Destination')
    transaction.commit()

    bigfolder = portal[FOLDER_ID]
    start = len(bigfolder.objectIds())
    if start >= n:
        print('setup: %r already has %d items (>= %d), skipping.'
              % (FOLDER_ID, start, n))
        return

    print('setup: creating Documents %d..%d in /%s/%s ...'
          % (start, n - 1, SITE_ID, FOLDER_ID))
    for i in range(start, n):
        bigfolder.invokeFactory('Document', 'doc-%06d' % i, title='Doc %d' % i)
        if (i + 1) % 500 == 0:
            transaction.commit()
            print('  ... %d/%d' % (i + 1, n))
    transaction.commit()

    catalog = portal.portal_catalog
    print('setup: done. /%s/%s has %d items; catalog length=%d'
          % (SITE_ID, FOLDER_ID, len(bigfolder.objectIds()), len(catalog)))


# --------------------------------------------------------------------------
# bench
# --------------------------------------------------------------------------
def _disable_optimization():
    """Unregister the context-aware index providers (reproduce baseline)."""
    try:
        from Products.CMFCore.interfaces import IContextAwareIndexProvider
    except ImportError:
        return False  # master: optimization does not exist -> already baseline
    gsm = getGlobalSiteManager()
    removed = 0
    for name in PROVIDER_NAMES:
        util = gsm.queryUtility(IContextAwareIndexProvider, name=name)
        if util is not None:
            gsm.unregisterUtility(util, IContextAwareIndexProvider, name=name)
            removed += 1
    return removed > 0


def _install_instrumentation():
    """Wrap the low-level catalog write methods to count work. Returns (counters, restore)."""
    from Products.ZCatalog.Catalog import Catalog

    counters = {
        'catalog_object': 0,
        'uncatalog_object': 0,
        'idx_updates': 0,
        'move_object': 0,
    }

    orig_catalog = Catalog.catalogObject
    orig_uncatalog = Catalog.uncatalogObject

    def counting_catalog(self, object, uid, threshold=None, idxs=None,
                         update_metadata=1):
        counters['catalog_object'] += 1
        counters['idx_updates'] += len(idxs) if idxs else len(self.indexes)
        return orig_catalog(self, object, uid, threshold, idxs,
                            update_metadata)

    def counting_uncatalog(self, uid):
        counters['uncatalog_object'] += 1
        return orig_uncatalog(self, uid)

    Catalog.catalogObject = counting_catalog
    Catalog.uncatalogObject = counting_uncatalog

    restorers = []

    def restore():
        Catalog.catalogObject = orig_catalog
        Catalog.uncatalogObject = orig_uncatalog
        for r in restorers:
            r()

    # Count CatalogTool.moveObject if present (modified branch only).
    try:
        from Products.CMFCore.CatalogTool import CatalogTool
        orig_move = getattr(CatalogTool, 'moveObject', None)
        if orig_move is not None:
            def counting_move(self, object, old_path, idxs):
                counters['move_object'] += 1
                return orig_move(self, object, old_path, idxs)
            CatalogTool.moveObject = counting_move
            restorers.append(
                lambda: setattr(CatalogTool, 'moveObject', orig_move))
    except ImportError:
        pass

    return counters, restore


def _do_move(portal, scenario):
    if scenario == 'rename':
        portal.manage_renameObject(FOLDER_ID, FOLDER_ID + '_moved')
    elif scenario == 'cutpaste':
        cp = portal.manage_cutObjects([FOLDER_ID])
        portal[DEST_ID].manage_pasteObjects(cp)
    else:
        raise SystemExit('Unknown scenario %r' % scenario)


def cmd_bench(app, scenario, baseline):
    from Products.CMFCore.indexing import getQueue

    _login_admin(app)
    portal = _get_portal(app)

    bigfolder = portal[FOLDER_ID]
    n = len(bigfolder.objectIds())

    mode = 'baseline'
    if not baseline:
        mode = 'optimized'
    else:
        _disable_optimization()

    counters, restore = _install_instrumentation()
    try:
        t0 = time.perf_counter()
        _do_move(portal, scenario)
        getQueue().process()        # flush queued index ops into the timed region
        elapsed = time.perf_counter() - t0
    finally:
        restore()
        transaction.abort()         # keep the dataset pristine for the next run

    print(
        'RESULT scenario=%s mode=%s N=%d seconds=%.3f '
        'catalog_object=%d uncatalog_object=%d idx_updates=%d move_object=%d'
        % (scenario, mode, n, elapsed,
           counters['catalog_object'], counters['uncatalog_object'],
           counters['idx_updates'], counters['move_object']))


# --------------------------------------------------------------------------
def main(app, argv):
    parser = argparse.ArgumentParser(prog='benchmark_move.py')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_setup = sub.add_parser('setup', help='build the dataset')
    p_setup.add_argument('n', type=int, help='number of Documents to create')

    p_bench = sub.add_parser('bench', help='time a move and report')
    p_bench.add_argument('--scenario', choices=('rename', 'cutpaste'),
                         required=True)
    p_bench.add_argument('--baseline', action='store_true',
                         help='disable the optimization (original behavior)')

    args = parser.parse_args(argv)
    if args.cmd == 'setup':
        cmd_setup(app, args.n)
    elif args.cmd == 'bench':
        cmd_bench(app, args.scenario, args.baseline)


# ``app`` is injected by ``zconsole run``; argv after the script path.
main(app, sys.argv[1:])  # noqa: F821
