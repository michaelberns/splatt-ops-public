// Checks that every chip on the Workload filter bar promises the number of
// project cards you get by clicking it, against a real PocketBase rather
// than a test fixture.
//
//   node tools/check_workload_counts.js
//   SPLATT_PB_URL=http://127.0.0.1:8090 node tools/check_workload_counts.js
//
// How it works
//   1. Reads jobs, assignments, clients, project_financials, contacts and
//      suppliers from PocketBase (SPLATT_PB_URL, default 127.0.0.1:8090).
//   2. Works out the quality flags per project with quality_engine.js.
//   3. Runs dashboard/filter_engine.js once at default settings, then once
//      per chip and once per pair of chips from different rows, and compares
//      each promised count with the cards actually produced.
//
// tests/test_filter_engine.js proves the same invariant on a small fictional
// dataset; this checks it on whatever data is really there. Exits 0 when
// every chip agrees, 1 on any mismatch (so bin/splatt-validate can run it as
// a standing check), and 2 when PocketBase cannot be read.
//
// Read only. It fetches and counts. It never writes anything.

'use strict';

const path = require('path');
const F = require(path.join(__dirname, '..', 'dashboard', 'filter_engine.js'));
const Q = require(path.join(__dirname, '..', 'dashboard', 'quality_engine.js'));

const PB = (process.env.SPLATT_PB_URL || 'http://127.0.0.1:8090').replace(/\/+$/, '');

async function collection(name) {
  const rows = [];
  let page = 1;
  for (;;) {
    const url = `${PB}/api/collections/${name}/records?perPage=500&page=${page}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${name}: ${res.status} ${res.statusText} from ${PB}`);
    const body = await res.json();
    rows.push(...(body.items || []));
    if (!body.totalPages || page >= body.totalPages) return rows;
    page += 1;
  }
}

function pad(text, width) {
  const s = String(text);
  return s.length >= width ? s : s + ' '.repeat(width - s.length);
}

async function main() {
  let jobs, assignments, clients, financials, contacts, suppliers;
  try {
    [jobs, assignments, clients, financials, contacts, suppliers] = await Promise.all([
      collection('jobs'), collection('assignments'), collection('clients'),
      collection('project_financials'), collection('contacts'), collection('suppliers'),
    ]);
  } catch (err) {
    console.error('Could not read PocketBase at ' + PB);
    console.error('  ' + err.message);
    console.error('');
    console.error('Start the stack, or set SPLATT_PB_URL to the address it is on.');
    console.error('See docs/INSTALL.md.');
    return 2;
  }

  const qctx = { jobs, clients, contacts, suppliers, financials, now: Date.now() };
  const byJob = {};
  for (const j of jobs) {
    const issues = Q.jobIssuesDeep(j, qctx);
    if (issues.length) byJob[j.id] = issues;
  }
  const recordCodes = Q.RULES
    .filter(r => r.entity === 'job' || r.entity === 'finline')
    .map(r => r.code);

  const dataset = { jobs, assignments, clients, financials, quality: { byJob } };
  const base = {};
  const start = F.runWorkload(dataset, base, { recordCodes });

  console.log('PocketBase %s', PB);
  console.log('%d projects, %d assignments, %d open',
    jobs.length, assignments.length,
    assignments.filter(t => t.status !== 'completed').length);
  console.log('');
  console.log('Default view: %d project cards, %d host projects, %d tasks',
    start.projects.length, start.hostProjects.length, start.tasks.length);
  console.log('Summary bar: %d open projects, %d open tasks, %d overdue projects, %d overdue tasks',
    start.summary.openProjects, start.summary.openTasks,
    start.summary.overdueProjects, start.summary.overdueTasks);
  console.log('');

  const chips = [];
  for (const key of F.STATUS_KEYS) {
    chips.push({ row: 'status', name: key, patch: { status: key }, promised: start.counts.status[key] });
  }
  for (const key of F.DATE_WINDOWS) {
    chips.push({ row: 'when', name: key, patch: { when: key }, promised: start.counts.when[key] });
  }
  for (const code of recordCodes) {
    chips.push({ row: 'records', name: code, patch: { records: [code] }, promised: start.counts.records[code] });
  }
  for (const key of F.SCOPE_KEYS) {
    chips.push({ row: 'scope', name: key, patch: { [key]: true }, promised: start.counts.scope[key] });
  }

  let bad = 0;
  console.log('%s %s %s %s', pad('row', 9), pad('chip', 26), pad('says', 6), 'rows');
  console.log('-'.repeat(56));
  for (const chip of chips) {
    const actual = F.countCards(F.runWorkload(dataset, chip.patch, { recordCodes }));
    const ok = chip.promised === actual;
    if (!ok) bad += 1;
    console.log('%s %s %s %s  %s',
      pad(chip.row, 9), pad(chip.name, 26), pad(chip.promised, 6), pad(actual, 5),
      ok ? 'ok' : 'MISMATCH');
  }

  // Pairs of chips from different rows, because combinations are where a
  // count and its rows are most likely to disagree.
  let pairs = 0, badPairs = 0;
  for (const first of chips) {
    const withFirst = F.runWorkload(dataset, first.patch, { recordCodes });
    const promisedBy = {
      status: (c, k) => c.status[k], when: (c, k) => c.when[k],
      records: (c, k) => c.records[k], scope: (c, k) => c.scope[k],
    };
    for (const second of chips) {
      if (second.row === first.row) continue;
      pairs += 1;
      const promised = promisedBy[second.row](withFirst.counts, second.name);
      const actual = F.countCards(
        F.runWorkload(dataset, Object.assign({}, first.patch, second.patch), { recordCodes })
      );
      if (promised !== actual) {
        badPairs += 1;
        console.log('PAIR MISMATCH %s/%s + %s/%s: says %s, rows %s',
          first.row, first.name, second.row, second.name, promised, actual);
      }
    }
  }

  console.log('');
  console.log('%d chips, %d pairs checked', chips.length, pairs);
  if (bad || badPairs) {
    console.log('%d chips and %d pairs disagree with their rows', bad, badPairs);
    return 1;
  }
  console.log('every chip agrees with its rows');
  return 0;
}

main().then(code => process.exit(code)).catch(err => {
  console.error(err);
  process.exit(1);
});
