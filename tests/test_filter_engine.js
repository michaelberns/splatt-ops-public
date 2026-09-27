// Tests for dashboard/filter_engine.js, the Workload filter pipeline.
//
//   node --test tests/test_filter_engine.js
//
// The main property: every chip promises the number of project cards you
// actually get by clicking it. That is checked for every chip, and then for
// every pair of chips from different rows, because combinations are where a
// count and its rows are most likely to disagree. The rest covers host
// projects, the exclusive scopes, the task date rules, search, the filter
// descriptions and the status groups. All data below is fictional.

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('path');

const F = require(path.join(__dirname, '..', 'dashboard', 'filter_engine.js'));

// A fixed "now", so the relative dates below never change meaning.
const NOW = new Date('2026-09-05T09:00:00Z').getTime();
const DAY = 86400000;
const iso = (offsetDays) => new Date(NOW + offsetDays * DAY).toISOString().slice(0, 10);

const CLIENTS = [
  { id: 'c1', name: 'Amberleaf', critical: false },
  { id: 'c2', name: 'Kowhai Health', critical: true },
  { id: 'c3', name: 'Glacier Water', critical: false },
];

// One project of each shape the pipeline treats differently.
const JOBS = [
  { id: 'j_live',     client: 'c1', title: 'Large quote',      status: 'quoting',   start_date: iso(-10), due_date: iso(3),   value: 1000 },
  { id: 'j_overdue',  client: 'c1', title: 'Capper parts',     status: 'quoted',    start_date: iso(-30), due_date: iso(-2),  value: 2000 },
  { id: 'j_nodates',  client: 'c3', title: 'New enquiry',      status: 'quoting',   value: 500 },
  { id: 'j_stale',    client: 'c3', title: 'Old line',         status: 'quoted',    start_date: iso(-200), due_date: iso(-90), value: 300 },
  { id: 'j_closed',   client: 'c1', title: 'Finished job',     status: 'completed', start_date: iso(-100), due_date: iso(-60), value: 4000 },
  { id: 'j_archived', client: 'c2', title: 'Archived job',     status: 'quoting',   archived: true, start_date: iso(-5), due_date: iso(5), value: 700 },
  // j_legacy is on commissioning. j_paid shows how payment is recorded: a
  // completed job carrying the paid flag, which is what the Paid chip reads.
  { id: 'j_legacy',   client: 'c2', title: 'Commissioning job', status: 'commissioning', start_date: iso(-5), due_date: iso(6),   value: 900 },
  { id: 'j_paid',     client: 'c2', title: 'Paid job',         status: 'completed', paid: true, start_date: iso(-20), due_date: iso(-1), value: 5000 },
  { id: 'j_hold',     client: 'c3', title: 'Parked job',       status: 'on_hold',   start_date: iso(-3), due_date: iso(9),   value: 100 },
  { id: 'j_cancel',   client: 'c3', title: 'Cancelled job',    status: 'cancelled', start_date: iso(-9), due_date: iso(-4),  value: 0 },
];

// A stalled task has its Todoist date cleared on purpose. Its real date lives
// on the "Real due:" pin in the description, and reading due_date instead
// would drop it out of every overdue figure.
const ASSIGNMENTS = [
  { id: 't_live',    job: 'j_live',    client: 'c1', content: 'Chase the quote', status: 'open',
    section_name: '📅 Upcoming', due_date: iso(2) },
  { id: 't_late',    job: 'j_overdue', client: 'c1', content: 'Send the parts list', status: 'open',
    section_name: '🔥 Overdue', due_date: iso(-3) },
  { id: 't_stalled', job: 'j_overdue', client: 'c1', content: 'Waiting on Taponera', status: 'open',
    section_name: '💤 Stalled', description: '📁 Project: x\n\n📌 Real due: ' + iso(-40) + ' | 40 days late' },
  { id: 't_host',    job: 'j_closed',  client: 'c1', content: 'Invoice not raised', status: 'open',
    section_name: '📅 Upcoming', due_date: iso(1) },
  { id: 't_other',   job: 'j_legacy',  client: 'c2', content: 'Other client work', status: 'open',
    section_name: '📅 Upcoming', due_date: iso(4) },
  { id: 't_done',    job: 'j_live',    client: 'c1', content: 'Already finished', status: 'completed',
    section_name: '📅 Upcoming', due_date: iso(-1) },
  { id: 't_arch',    job: 'j_archived', client: 'c2', content: 'Archived task', status: 'open',
    archived: true, section_name: '📅 Upcoming', due_date: iso(1) },
  { id: 't_noclient', client: null, content: 'Needs an owner', status: 'open',
    section_name: '📥 Backlog', due_date: iso(6) },
];

const RECORD_CODES = ['job_no_value', 'job_no_due_date', 'job_no_address'];
const QUALITY = {
  byJob: {
    j_nodates: [{ code: 'job_no_due_date' }, { code: 'job_no_address' }],
    j_live:    [{ code: 'job_no_address' }],
    j_stale:   [{ code: 'job_no_value' }],
  },
};

const DATASET = {
  jobs: JOBS,
  assignments: ASSIGNMENTS,
  clients: CLIENTS,
  financials: [],
  quality: QUALITY,
  now: NOW,
};

const run = (patch) => F.runWorkload(DATASET, patch || {}, { recordCodes: RECORD_CODES });

// Every chip on the bar, as (row, patch to apply, where its count is stored).
function everyChip() {
  const chips = [];
  for (const key of F.STATUS_KEYS) {
    chips.push({ row: 'status', name: key, patch: { status: key },
                 count: (c) => c.status[key] });
  }
  for (const key of F.DATE_WINDOWS) {
    chips.push({ row: 'when', name: key, patch: { when: key },
                 count: (c) => c.when[key] });
  }
  for (const code of RECORD_CODES) {
    chips.push({ row: 'records', name: code, patch: { records: [code] },
                 count: (c) => c.records[code] });
  }
  for (const key of F.SCOPE_KEYS) {
    chips.push({ row: 'scope', name: key, patch: { [key]: true },
                 count: (c) => c.scope[key] });
  }
  return chips;
}

// ── the invariant ─────────────────────────────────────────────────────────
test('every chip counts the rows you get by clicking it', () => {
  const base = run();
  for (const chip of everyChip()) {
    const promised = chip.count(base.counts);
    const actual = F.countCards(run(chip.patch));
    assert.strictEqual(promised, actual,
      `${chip.row}/${chip.name}: chip says ${promised}, clicking it gives ${actual}`);
  }
});

test('every chip still counts its own rows once another row is picked', () => {
  // Combinations are where a count and its rows are most likely to disagree.
  const chips = everyChip();
  for (const first of chips) {
    const withFirst = F.runWorkload(DATASET, first.patch, { recordCodes: RECORD_CODES });
    for (const second of chips) {
      if (second.row === first.row) continue;
      const promised = second.count(withFirst.counts);
      const actual = F.countCards(run(Object.assign({}, first.patch, second.patch)));
      assert.strictEqual(promised, actual,
        `${first.row}/${first.name} + ${second.row}/${second.name}: ` +
        `chip says ${promised}, clicking gives ${actual}`);
    }
  }
});

test('the summary counts the same cards the page draws', () => {
  for (const chip of everyChip()) {
    const one = run(chip.patch);
    assert.strictEqual(one.summary.cards, F.countCards(one), chip.name);
  }
});

// ── host projects ─────────────────────────────────────────────────────────
test('a project only present for its tasks is never counted as a result', () => {
  const one = run({ status: 'current' });
  const ids = one.projects.map(j => j.id);
  assert.ok(ids.indexOf('j_closed') === -1,
    'a completed project matched the Current filter');
  const hosts = one.hostProjects.map(j => j.id);
  assert.ok(hosts.indexOf('j_closed') !== -1,
    'the open task under the completed project lost its project');
  assert.ok(one.tasks.some(t => t.id === 't_host'),
    'the task itself was dropped');
});

test('a host project is never in both lists', () => {
  for (const chip of everyChip()) {
    const one = run(chip.patch);
    const ids = new Set(one.projects.map(j => j.id));
    for (const h of one.hostProjects) {
      assert.ok(!ids.has(h.id), `${h.id} is a result and a host at once (${chip.name})`);
    }
  }
});

// ── records combine ───────────────────────────────────────────────────────
test('two record chips narrow rather than widen', () => {
  const a = F.countCards(run({ status: 'all', records: ['job_no_due_date'] }));
  const b = F.countCards(run({ status: 'all', records: ['job_no_address'] }));
  const both = F.countCards(run({ status: 'all', records: ['job_no_due_date', 'job_no_address'] }));
  assert.ok(both <= a && both <= b,
    `both (${both}) is bigger than one of them alone (${a}, ${b})`);
});

// ── the scopes are exclusive ──────────────────────────────────────────────
test('Archived shows the archived work and nothing else', () => {
  const one = run({ archived: true });
  const cards = one.projects.concat(one.hostProjects);
  assert.ok(cards.length > 0, 'nothing came back');
  for (const j of cards) assert.ok(j.archived, `${j.id} is not archived`);
  for (const t of one.tasks) assert.ok(t.archived, `${t.id} is not archived`);
});

test('with a scope on, the status chips stop changing the answer', () => {
  const counts = run({ archived: true }).counts.status;
  const values = F.STATUS_KEYS.map(k => counts[k]);
  assert.strictEqual(new Set(values).size, 1,
    'a status chip still moved the numbers while Archived was on');
});

// ── the date split ────────────────────────────────────────────────────────
test('a stalled task keeps the date it was really due', () => {
  const t = ASSIGNMENTS.find(x => x.id === 't_stalled');
  assert.strictEqual(F.realDueOf(t), iso(-40));
  // Nothing else has a date to offer, so what it lands on is the same day.
  assert.strictEqual(F.actionableDateOf(t), iso(-40));
});

test('a waiting task is late by the pin and lands on the chase date', () => {
  const t = { due_date: iso(2), description: '📌 Real due: ' + iso(-40) };
  assert.strictEqual(F.realDueOf(t), iso(-40), 'how late it is');
  assert.strictEqual(F.actionableDateOf(t), iso(2), 'when it lands');
});

test('a pin under a Project line is still read', () => {
  // Other header lines can sit above the pin; the whole description is read.
  const t = { description: '📁 Project: SS Folders/x/PROJECT.md\n\n📌 Real due: 2026-07-12 | late' };
  assert.strictEqual(F.realDueOf(t), '2026-07-12');
});

test('parked work is late but not on the desk', () => {
  const stalled = ASSIGNMENTS.find(x => x.id === 't_stalled');
  assert.ok(F.isParked(stalled));
  assert.ok(!F.isTaskOverdueLive(stalled, NOW),
    'a stalled task counted as overdue on the desk today');
  const live = ASSIGNMENTS.find(x => x.id === 't_late');
  assert.ok(F.isTaskOverdueLive(live, NOW));
});

// ── search ────────────────────────────────────────────────────────────────
test('one search matcher covers projects and tasks', () => {
  const byClient = run({ status: 'all', search: 'glacier' });
  assert.ok(byClient.projects.length > 0, 'a client name found no projects');
  for (const j of byClient.projects) assert.strictEqual(j.client, 'c3');

  const byTask = run({ status: 'all', search: 'taponera' });
  assert.ok(byTask.tasks.some(t => t.id === 't_stalled'),
    'a word in a task title found nothing');
});

// ── staleness and the live workload ──────────────────────────────────────
test('a stale project does not inflate the open figures', () => {
  assert.ok(F.isStale(JOBS.find(j => j.id === 'j_stale'), NOW));
  assert.ok(!F.countsTowardWorkload(JOBS.find(j => j.id === 'j_stale'), NOW));
  assert.ok(!F.countsTowardWorkload(JOBS.find(j => j.id === 'j_hold'), NOW));
  assert.ok(F.countsTowardWorkload(JOBS.find(j => j.id === 'j_live'), NOW));
});

// ── what the bar says is switched on ──────────────────────────────────────
test('the strip and the empty state name the same filters', () => {
  // One function, so "Nothing matches Current + Overdue" and the strip above
  // it can never name a different set.
  const named = F.describeFilters(
    { status: 'stale', when: 'overdue', records: ['job_no_value'], archived: true, search: 'coastline' },
    { job_no_value: 'No project value' }
  );
  assert.deepStrictEqual(named.map(f => f.label),
    ['Stale/Old', 'Overdue', 'No project value', 'Archived', '\u201ccoastline\u201d']);
  assert.deepStrictEqual(named.map(f => f.row),
    ['status', 'when', 'records', 'scope', 'search']);
});

test('Current is named but not removable, because it is the resting state', () => {
  const named = F.describeFilters({});
  assert.strictEqual(named.length, 1);
  assert.strictEqual(named[0].label, 'Current');
  assert.strictEqual(named[0].removable, false);
});

test('the strip appears exactly when something is not default', () => {
  assert.ok(F.isDefaultFilters({}));
  assert.ok(F.isDefaultFilters({ status: 'current', when: 'all', records: [] }));
  assert.ok(!F.isDefaultFilters({ status: 'stale' }));
  assert.ok(!F.isDefaultFilters({ when: 'today' }));
  assert.ok(!F.isDefaultFilters({ records: ['job_no_value'] }));
  assert.ok(!F.isDefaultFilters({ search: 'coastline' }));
  assert.ok(!F.isDefaultFilters({ archived: true }));
  assert.ok(!F.isDefaultFilters({ customStart: '2026-09-01' }));
});

test('Today means one thing per row', () => {
  // "Today" in the When row means due today; the toggles say Created today
  // and Edited today.
  assert.strictEqual(F.WHEN_LABELS.today, 'Today');
  assert.strictEqual(F.SCOPE_LABELS.createdToday, 'Created today');
  assert.strictEqual(F.SCOPE_LABELS.editedToday, 'Edited today');
});

test('completed tasks and archived rows stay out of the normal view', () => {
  const one = run({ status: 'all' });
  const ids = one.tasks.map(t => t.id);
  assert.ok(ids.indexOf('t_done') === -1, 'a completed task is in the list');
  assert.ok(ids.indexOf('t_arch') === -1, 'an archived task is in the list');
});

// ── one list, no gaps, no overlaps ───────────────────────────────────────
//
// There is one list of eleven job statuses, shared with core/job_status.py.
// This keeps the grouping correct on the browser side: a status in no group
// would fall out of every chip and vanish from the board, and a status in two
// groups would be counted twice in the money forecast.

const ELEVEN = [
  'quoting', 'quoted', 'won', 'invoicing', 'invoiced', 'commissioning',
  'lost', 'on_hold', 'cancelled', 'completed', 'reengage',
];

test('every status belongs to exactly one group', () => {
  const groups = F.STATUS_GROUPS;
  const seen = {};
  Object.keys(groups).forEach((name) => {
    groups[name].forEach((status) => {
      assert.ok(ELEVEN.indexOf(status) !== -1,
        status + ' is in group ' + name + ' but is not a job status');
      assert.ok(!seen[status], status + ' is in both ' + seen[status] + ' and ' + name);
      seen[status] = name;
    });
  });
  ELEVEN.forEach((status) => {
    assert.ok(seen[status], status + ' is in no group, so no chip can ever show it');
  });
});

test('the three predicates cover all eleven with no gaps and no overlaps', () => {
  ELEVEN.forEach((status) => {
    const hits = [
      F.isPipelineStatus(status),
      F.isTerminalStatus(status),
      F.isReengageStatus(status),
      status === 'on_hold',
    ].filter(Boolean).length;
    assert.strictEqual(hits, 1,
      status + ' is matched by ' + hits + ' of the four, expected exactly 1');
  });
});

test('the retired statuses are matched by nothing', () => {
  ['paid', 'active'].forEach((dead) => {
    assert.ok(!F.isPipelineStatus(dead), dead + ' still counts as pipeline');
    assert.ok(!F.isTerminalStatus(dead), dead + ' still counts as terminal');
    assert.ok(!F.isOpenStatus(dead), dead + ' still counts as open');
    assert.ok(!F.isConfirmedStatus(dead), dead + ' still counts as confirmed');
  });
});

test('a job on each of the eleven is reachable on the board', () => {
  // The same check through the real pipeline rather than the predicates.
  // One job on every status, then the three views a person can reach: the
  // normal board, the board with Re-engage shown, and the closed view.
  // Between them every status has to appear somewhere, or a project on it
  // would silently leave the board.
  const jobs = ELEVEN.map((status, i) => ({
    id: 'cover_' + status, client: 'c1', title: 'Cover ' + status,
    status: status, start_date: iso(-2), due_date: iso(4), value: 100 + i,
  }));
  const dataset = { jobs: jobs, assignments: [], clients: CLIENTS,
                    financials: [], quality: {}, now: NOW };
  const views = [
    { status: 'all', when: 'all' },
    { status: 'all', when: 'all', reengage: true },
    { status: 'all', when: 'all', cancelled: true },
  ];
  const seen = {};
  views.forEach((filters) => {
    F.runWorkload(dataset, filters, { recordCodes: RECORD_CODES })
      .projects.forEach((j) => { seen[j.status] = true; });
  });
  const missing = ELEVEN.filter((status) => !seen[status]);
  assert.deepStrictEqual(missing, [],
    'no view on the board draws: ' + missing.join(', '));
});
