// Tests for dashboard/snapshot_shim.js (the hosted read-only mode).
//
//   node tests/test_snapshot_shim.js
//
// A plain Node script rather than a node:test file: it loads the shim into a
// vm sandbox with a fake window, document and fetch, prints one line per
// check, and exits 1 if any check fails.
//
// The shim answers every read the hosted dashboard makes, so a wrong query
// engine would show wrong numbers on a page that looks fine. These checks
// cover the no-op case, record lookups, filters, sorts and paging, the
// files-server endpoints, the 403 on every write and every Todoist call, CDN
// pass-through and the as-of pill. Rendering is not tested here, because the
// hosted page runs the same index.html as the local copy.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SHIM = path.join(__dirname, '..', 'dashboard', 'snapshot_shim.js');

let passed = 0, failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log('  ok   ' + name); }
  else { failed++; console.log('  FAIL ' + name + (detail ? '  ' + detail : '')); }
}
function eq(name, got, want) {
  check(name, JSON.stringify(got) === JSON.stringify(want),
        'got ' + JSON.stringify(got) + ' want ' + JSON.stringify(want));
}

const SNAP = {
  generated_at: '2026-09-10T04:00:00Z',
  collections: {
    jobs: [
      { id: 'j1', title: 'Alpha',   status: 'open',   created: '2026-01-03', direction: 'in'  },
      { id: 'j2', title: 'Bravo',   status: 'closed', created: '2026-02-09', direction: 'out' },
      { id: 'j3', title: 'Charlie', status: 'open',   created: '2026-03-11', direction: 'in'  },
    ],
    project_financials: [
      { id: 'f1', job: 'CARTER', project_key: 'OTHER', amount: 10, direction: 'in',  created: '2026-01-01' },
      { id: 'f2', job: 'OTHER', project_key: 'CARTER', amount: 20, direction: 'out', created: '2026-01-02' },
      { id: 'f3', job: 'NOPE',  project_key: 'NOPE',  amount: 30, direction: 'in',  created: '2026-01-03' },
    ],
  },
  endpoints: {
    '/api/xero': { ok: true, invoices: 3 },
    '/api/project-files': [{ path: 'Client/PROJECT.md', content: '# hi' }],
  },
};

function makeSandbox(cfg) {
  const els = [];
  const el = () => ({ style: {}, textContent: '', title: '', innerHTML: '', appendChild() {}, setAttribute() {} });
  const body = el();
  const calls = { real: [] };
  const sandbox = {
    console, URLSearchParams, Response, setTimeout, clearTimeout,
    Object, JSON, Math, Date, String, Number, isNaN, RegExp, Array, Promise, Error, parseInt,
    window: {
      SPLATT_CONFIG: cfg,
      fetch: (url, init) => {
        calls.real.push(String(url));
        if (String(url) === cfg.SNAPSHOT_URL) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(SNAP) });
        }
        return Promise.resolve({ ok: true, status: 200, passthrough: true, url: String(url) });
      },
    },
    document: { body, getElementById: () => el(), createElement: () => { const e = el(); els.push(e); return e; }, addEventListener: () => {} },
  };
  sandbox.window.window = sandbox.window;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SHIM, 'utf8'), sandbox, { filename: 'snapshot_shim.js' });
  return { sandbox, calls, els };
}

const CFG = { SNAPSHOT_URL: 'snapshot.json', PB_URL: 'https://snapshot.invalid/pb', FILES_URL: 'https://snapshot.invalid/files', TODOIST_TOKEN: '' };
const PB = CFG.PB_URL, FS = CFG.FILES_URL;

(async function run() {
  console.log('\nsnapshot_shim');

  {
    const noSnap = makeSandbox({ PB_URL: PB, FILES_URL: FS, TODOIST_TOKEN: 'x' });
    const before = noSnap.sandbox.window.fetch;
    check('no op when SNAPSHOT_URL is absent', typeof before === 'function' && String(before).indexOf('isPB') === -1);
  }

  const { sandbox, calls, els } = makeSandbox(CFG);
  const f = sandbox.window.fetch;
  const body = async (p) => JSON.parse(await (await p).text());

  { const d = await body(f(PB + '/api/collections/jobs/records?perPage=200'));
    eq('lists a whole collection', d.items.length, 3);
    eq('reports totalItems', d.totalItems, 3); }
  { const d = await body(f(PB + '/api/collections/jobs/records/j2'));
    eq('reads one record by id', d.title, 'Bravo'); }
  { const r = await f(PB + '/api/collections/jobs/records/nope');
    eq('404s an id not in the snapshot', r.status, 404); }
  { const d = await body(f(PB + '/api/collections/does_not_exist/records?perPage=50'));
    eq('an uncaptured collection is empty, not an error', d.items, []); }

  { const d = await body(f(PB + '/api/collections/jobs/records?perPage=200&sort=-created'));
    eq('sorts descending', d.items.map(x => x.id), ['j3', 'j2', 'j1']); }
  { const d = await body(f(PB + '/api/collections/jobs/records?perPage=200&sort=title'));
    eq('sorts ascending', d.items.map(x => x.id), ['j1', 'j2', 'j3']); }
  { const d = await body(f(PB + '/api/collections/project_financials/records?perPage=200&sort=direction,created'));
    eq('sorts on two keys', d.items.map(x => x.id), ['f1', 'f3', 'f2']); }

  { const filter = encodeURIComponent("job='CARTER' || project_key='CARTER'");
    const d = await body(f(PB + '/api/collections/project_financials/records?perPage=200&sort=direction,created&filter=' + filter));
    eq('or filter matches either field', d.items.map(x => x.id).sort(), ['f1', 'f2']); }
  { const filter = encodeURIComponent("status='open' && direction='in'");
    const d = await body(f(PB + '/api/collections/jobs/records?perPage=200&filter=' + filter));
    eq('and filter narrows', d.items.map(x => x.id), ['j1', 'j3']); }
  { const filter = encodeURIComponent("status!='open'");
    const d = await body(f(PB + '/api/collections/jobs/records?perPage=200&filter=' + filter));
    eq('not equal filter', d.items.map(x => x.id), ['j2']); }
  { const filter = encodeURIComponent("title~'rav'");
    const d = await body(f(PB + '/api/collections/jobs/records?perPage=200&filter=' + filter));
    eq('contains filter', d.items.map(x => x.id), ['j2']); }
  { const filter = encodeURIComponent('>>> not a filter at all <<<');
    const d = await body(f(PB + '/api/collections/jobs/records?perPage=200&filter=' + filter));
    eq('a filter it cannot parse shows more rows, never a blank page', d.items.length, 3); }

  { const d = await body(f(PB + '/api/collections/jobs/records?perPage=2&page=2&sort=title'));
    eq('page two returns the remainder', d.items.map(x => x.id), ['j3']);
    eq('page two reports total pages', d.totalPages, 2); }

  { const d = await body(f(FS + '/api/xero'));
    eq('serves a captured files endpoint', d.invoices, 3); }
  { const r = await f(FS + '/api/folder-contents?path=nothing%2Fhere');
    const d = JSON.parse(await r.text());
    eq('an uncaptured folder is empty, not a hang', d.entries, []); }
  { const r = await f(FS + '/api/file?path=missing.pdf');
    eq('an uncaptured file 404s in plain words', r.status, 404); }

  for (const [label, url, init] of [
    ['blocks a PocketBase POST',   PB + '/api/collections/jobs/records', { method: 'POST', body: '{}' }],
    ['blocks a PocketBase PATCH',  PB + '/api/collections/jobs/records/j1', { method: 'PATCH', body: '{}' }],
    ['blocks a PocketBase DELETE', PB + '/api/collections/jobs/records/j1', { method: 'DELETE' }],
    ['blocks a files server POST', FS + '/api/upload-file', { method: 'POST', body: '{}' }],
    ['blocks closing a Todoist task', 'https://api.todoist.com/api/v1/tasks/1/close', { method: 'POST' }],
  ]) { const r = await f(url, init); check(label, r.status === 403); }
  { const r = await f('https://api.todoist.com/api/v1/tasks/1');
    eq('blocks even a Todoist read, the token is not on this page', r.status, 403); }

  { const before = calls.real.length;
    const r = await f('https://unpkg.com/react@18/umd/react.development.js');
    check('passes CDN requests through untouched', r.passthrough === true && calls.real.length === before + 1); }

  { const pill = els.find(e => String(e.textContent).indexOf('Read only snapshot') === 0);
    check('shows the as-of pill', !!pill, pill ? '' : 'no pill created'); }

  console.log('\n' + passed + ' passed, ' + failed + ' failed\n');
  process.exit(failed ? 1 : 0);
})();
