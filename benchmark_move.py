"""Benchmark the Products.CMFCore move optimization.

Run inside the plone/plone-backend container via ``zconsole run``, which injects
the Zope application root as the global ``app`` but does NOT forward argv — so
parameters are passed as environment variables:

    BENCH_CMD=setup BENCH_N=10000 \
        zconsole run etc/zope.conf benchmark_move.py
    BENCH_CMD=bench BENCH_SCENARIO=rename|cutpaste [BENCH_BASELINE=1] \
        zconsole run etc/zope.conf benchmark_move.py
    PDF=1 BENCH_CMD=setup BENCH_N=100 \
        zconsole run etc/zope.conf benchmark_move.py
    PDF=1 BENCH_CMD=bench BENCH_SCENARIO=rename [BENCH_BASELINE=1] \
        zconsole run etc/zope.conf benchmark_move.py

  setup    build /Plone/bigfolder with BENCH_N Documents (+ /Plone/dest)
           with PDF=1, build /Plone/bigfilefolder with BENCH_N random 40-60 page PDFs
  bench    time the move, print a RESULT line, then abort

The script is branch-agnostic: it works on both the original (master) and the
modified (move_optimization) CMFCore.  ``BENCH_BASELINE=1`` unregisters the
IContextAwareIndexProvider utilities so that the modified code falls back to the
original ``unindex`` + ``index`` path (a no-op on master, which is baseline anyway).
"""

import os
import random
import textwrap
import time

import transaction
from AccessControl.SecurityManagement import newSecurityManager
from zope.component import getGlobalSiteManager
from zope.component.hooks import setSite


SITE_ID = 'Plone'
FOLDER_ID = 'bigfolder'
PDF_FOLDER_ID = 'bigfilefolder'
DEST_ID = 'dest'
PDF_MIMETYPE = 'application/pdf'
PDF_LINE_WIDTH = 86
PDF_LINES_PER_PAGE = 46
PDF_MIN_PAGES = 40
PDF_MAX_PAGES = 60
PDF_COMMIT_EVERY = 100


def _env_flag(name):
    return os.environ.get(name, '').lower() in ('1', 'true', 'yes', 'on')


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
def _ensure_folder_addable(portal):
    # In Plone 6 the 'Folder' type has global_allow=False; enable it so we can
    # build a folderish container with many children at the site root.
    fti = portal.portal_types.getTypeInfo('Folder')
    if fti is not None and not fti.global_allow:
        fti.global_allow = True


def _ensure_benchmark_folders(portal, folder_id, title):
    if folder_id not in portal.objectIds():
        portal.invokeFactory('Folder', folder_id, title=title)
    if DEST_ID not in portal.objectIds():
        portal.invokeFactory('Folder', DEST_ID, title='Destination')
    transaction.commit()


def _load_lorem():
    try:
        import lorem
    except ImportError:
        raise SystemExit(
            'PDF=1 requires the Python "lorem" package. When using run.sh, '
            'it is installed automatically; otherwise run '
            '"/app/bin/pip install lorem" in the Plone container.')
    return lorem


def _pdf_escape(text):
    return text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def _random_pdf_pages(lorem):
    pages = []
    for _page in range(random.randint(PDF_MIN_PAGES, PDF_MAX_PAGES)):
        lines = []
        while len(lines) < PDF_LINES_PER_PAGE:
            paragraph = lorem.paragraph().strip()
            paragraph_lines = textwrap.wrap(
                paragraph,
                width=PDF_LINE_WIDTH,
            ) or ['']
            for line in paragraph_lines:
                if len(lines) >= PDF_LINES_PER_PAGE:
                    break
                lines.append(line)
            if len(lines) < PDF_LINES_PER_PAGE:
                lines.append('')
        pages.append(lines)
    return pages


def _page_stream(page_lines):
    ops = ['BT', '/F1 11 Tf', '72 720 Td', '14 TL']
    for line in page_lines:
        if line:
            ops.append('(%s) Tj' % _pdf_escape(line))
        ops.append('T*')
    ops.append('ET')
    return '\n'.join(ops).encode('latin-1', 'replace')


def _build_pdf(pages):
    objects = {
        1: b'<< /Type /Catalog /Pages 2 0 R >>',
        3: b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    }
    page_numbers = []
    next_object = 4
    for page_lines in pages:
        page_number = next_object
        content_number = next_object + 1
        next_object += 2
        page_numbers.append(page_number)

        stream = _page_stream(page_lines)
        objects[content_number] = (
            b'<< /Length %d >>\nstream\n' % len(stream)
        ) + stream + b'\nendstream'
        objects[page_number] = (
            '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
            '/Resources << /Font << /F1 3 0 R >> >> '
            '/Contents %d 0 R >>' % content_number
        ).encode('ascii')

    kids = ' '.join('%d 0 R' % number for number in page_numbers)
    objects[2] = (
        '<< /Type /Pages /Kids [%s] /Count %d >>'
        % (kids, len(page_numbers))
    ).encode('ascii')

    chunks = [b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n']
    offsets = [0]
    for number in range(1, next_object):
        offsets.append(sum(len(chunk) for chunk in chunks))
        chunks.append(('%d 0 obj\n' % number).encode('ascii'))
        chunks.append(objects[number])
        chunks.append(b'\nendobj\n')

    startxref = sum(len(chunk) for chunk in chunks)
    xref = [
        'xref\n0 %d\n' % len(offsets),
        '0000000000 65535 f \n',
    ]
    xref.extend('%010d 00000 n \n' % offset for offset in offsets[1:])
    xref.append(
        'trailer\n<< /Size {} /Root 1 0 R >>\n'
        'startxref\n{}\n%%EOF\n'.format(len(offsets), startxref)
    )
    chunks.append(''.join(xref).encode('ascii'))
    return b''.join(chunks)


def _populate_documents(folder, folder_id, n):
    start = len(folder.objectIds())
    if start >= n:
        print('setup: %r already has %d items (>= %d), skipping.'
              % (folder_id, start, n))
        return

    print('setup: creating Documents %d..%d in /%s/%s ...'
          % (start, n - 1, SITE_ID, folder_id))
    for i in range(start, n):
        folder.invokeFactory('Document', 'doc-%06d' % i, title='Doc %d' % i)
        if (i + 1) % 500 == 0:
            transaction.commit()
            print('  ... %d/%d' % (i + 1, n))
    transaction.commit()


def _populate_pdfs(folder, folder_id, n):
    from plone.namedfile.file import NamedBlobFile

    lorem = _load_lorem()
    start = len(folder.objectIds())
    if start >= n:
        print('setup: %r already has %d items (>= %d), skipping.'
              % (folder_id, start, n))
        return

    print('setup: creating random 40-60 page PDFs %d..%d in /%s/%s ...'
          % (start, n - 1, SITE_ID, folder_id))
    existing = set(folder.objectIds())
    for i in range(start, n):
        item_id = 'pdf-%06d' % i
        if item_id not in existing:
            folder.invokeFactory('File', item_id, title='PDF %d' % i)
            existing.add(item_id)
        filename = '%s.pdf' % item_id
        pdf = folder[item_id]
        pdf.file = NamedBlobFile(
            _build_pdf(_random_pdf_pages(lorem)),
            contentType=PDF_MIMETYPE,
            filename=filename,
        )
        pdf.reindexObject()
        if (i + 1) % PDF_COMMIT_EVERY == 0:
            transaction.commit()
            print('  ... %d/%d' % (i + 1, n))
    transaction.commit()


def cmd_setup(app, n, pdf):
    _login_admin(app)
    portal = _get_portal(app)
    _ensure_folder_addable(portal)

    folder_id = PDF_FOLDER_ID if pdf else FOLDER_ID
    folder_title = 'Big File Folder' if pdf else 'Big Folder'
    _ensure_benchmark_folders(portal, folder_id, folder_title)

    folder = portal[folder_id]
    if pdf:
        _populate_pdfs(folder, folder_id, n)
    else:
        _populate_documents(folder, folder_id, n)

    catalog = portal.portal_catalog
    print('setup: done. /%s/%s has %d items; catalog length=%d'
          % (SITE_ID, folder_id, len(folder.objectIds()), len(catalog)))


# --------------------------------------------------------------------------
# bench
# --------------------------------------------------------------------------
def _searchable_text_word_count(portal):
    """Return the number of unique terms in the SearchableText lexicon."""
    catalog = portal.portal_catalog
    index = catalog._catalog.indexes.get('SearchableText')
    if index is None:
        return 0

    lexicon = None
    lexicon_id = getattr(index, 'lexicon_id', None)
    if lexicon_id:
        lexicon = getattr(catalog, lexicon_id, None)

    if lexicon is None:
        if hasattr(index, '__of__'):
            index = index.__of__(catalog)
        get_lexicon = getattr(index, 'getLexicon', None)
        if get_lexicon is not None:
            lexicon = get_lexicon()

    if lexicon is None:
        return 0

    length = getattr(lexicon, 'length', None)
    if length is not None:
        return length()
    words = getattr(lexicon, 'words', None)
    if words is not None:
        return len(words())
    return 0


def _disable_optimization():
    """Unregister the context-aware index providers (reproduce baseline)."""
    try:
        from Products.CMFCore.interfaces import IContextAwareIndexProvider
    except ImportError:
        return False  # master: optimization does not exist -> already baseline
    gsm = getGlobalSiteManager()
    providers = list(gsm.getUtilitiesFor(IContextAwareIndexProvider))
    for name, util in providers:
        gsm.unregisterUtility(util, IContextAwareIndexProvider, name=name)
    return bool(providers)


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


def _do_move(portal, scenario, folder_id):
    if scenario == 'rename':
        portal.manage_renameObject(folder_id, folder_id + '_moved')
    elif scenario == 'cutpaste':
        cp = portal.manage_cutObjects([folder_id])
        portal[DEST_ID].manage_pasteObjects(cp)
    else:
        raise SystemExit('Unknown scenario %r' % scenario)


def cmd_bench(app, scenario, baseline, pdf):
    from Products.CMFCore.indexing import getQueue

    _login_admin(app)
    portal = _get_portal(app)

    folder_id = PDF_FOLDER_ID if pdf else FOLDER_ID
    if folder_id not in portal.objectIds():
        raise SystemExit(
            'Dataset folder /%s/%s not found. Run BENCH_CMD=setup first.'
            % (SITE_ID, folder_id))
    folder = portal[folder_id]
    n = len(folder.objectIds())
    searchable_words = _searchable_text_word_count(portal)

    mode = 'baseline'
    if not baseline:
        mode = 'optimized'
    else:
        _disable_optimization()

    counters, restore = _install_instrumentation()
    try:
        t0 = time.perf_counter()
        _do_move(portal, scenario, folder_id)
        getQueue().process()        # flush queued index ops into the timed region
        elapsed = time.perf_counter() - t0
    finally:
        restore()
        transaction.abort()         # keep the dataset pristine for the next run

    print(
        'RESULT scenario=%s mode=%s N=%d seconds=%.3f '
        'catalog_object=%d uncatalog_object=%d idx_updates=%d move_object=%d '
        'searchable_words=%d'
        % (scenario, mode, n, elapsed,
           counters['catalog_object'], counters['uncatalog_object'],
           counters['idx_updates'], counters['move_object'], searchable_words))


# --------------------------------------------------------------------------
def main(app):
    cmd = os.environ.get('BENCH_CMD', '')
    pdf = _env_flag('PDF')
    if cmd == 'setup':
        cmd_setup(app, int(os.environ.get('BENCH_N', '10000')), pdf)
    elif cmd == 'bench':
        scenario = os.environ.get('BENCH_SCENARIO', 'rename')
        baseline = _env_flag('BENCH_BASELINE')
        cmd_bench(app, scenario, baseline, pdf)
    else:
        raise SystemExit('Set BENCH_CMD=setup|bench (see module docstring).')


# ``app`` is injected by ``zconsole run``.
main(app)  # noqa: F821
