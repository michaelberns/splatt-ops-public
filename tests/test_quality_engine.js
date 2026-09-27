// Tests for dashboard/quality_engine.js, the record quality rules.
//
//   node --test tests/test_quality_engine.js
//
// The engine decides which gap flags show on the Workload page. Both
// directions are tested: a false flag teaches people to ignore flags, and a
// missed flag lets an unbilled job slip through. So each rule is checked on
// a broken record and on a complete one. All data below is fictional.
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const Q = require('../dashboard/quality_engine');

const codes = (issues) => issues.map(i => i.code).sort();
const has = (issues, code) => issues.some(i => i.code === code);

// A project with nothing wrong with it, used as the starting point so each
// test only has to say what it is breaking.
const goodJob = () => ({
  id: 'j1', title: 'Good job', client: 'c1', status: 'quoting',
  value: 5000, due_date: '2026-09-01', start_date: '2026-08-01',
});
const goodClient = () => ({
  id: 'c1', name: 'Good client', address: '12 Test Rd, Auckland',
  email_addresses: 'a@example.com', phone: '021 000 000',
});
const ctxWith = (over) => Object.assign({
  clients: [goodClient()], jobs: [], contacts: [{ id: 't1', client: 'c1', email: 'a@example.com' }],
  suppliers: [], financials: [], now: new Date('2026-08-13').getTime(),
}, over || {});

// ── the quiet case ────────────────────────────────────────────────────────

test('a complete project and client raise nothing', () => {
  const ctx = ctxWith({ jobs: [goodJob()] });
  assert.deepStrictEqual(codes(Q.issuesFor(goodJob(), 'job', ctx)), []);
  assert.deepStrictEqual(codes(Q.issuesFor(goodClient(), 'client', ctx)), []);
});

test('cancelled, lost and archived projects never raise flags', () => {
  const ctx = ctxWith({});
  for (const status of ['cancelled', 'lost']) {
    const j = Object.assign(goodJob(), { status, client: '', value: 0, due_date: '' });
    assert.deepStrictEqual(codes(Q.issuesFor(j, 'job', ctx)), [], status + ' should be silent');
  }
  const archived = Object.assign(goodJob(), { archived: true, client: '', value: 0, due_date: '' });
  assert.deepStrictEqual(codes(Q.issuesFor(archived, 'job', ctx)), []);
});

// ── blockers ──────────────────────────────────────────────────────────────

test('a project with no client is a blocker', () => {
  const j = Object.assign(goodJob(), { client: '' });
  const issues = Q.issuesFor(j, 'job', ctxWith({}));
  assert.ok(has(issues, 'job_no_client'));
  assert.strictEqual(issues.find(i => i.code === 'job_no_client').severity, 'blocker');
});

test('no client suppresses the no address flag, so one cause gives one chip', () => {
  const j = Object.assign(goodJob(), { client: '' });
  const issues = Q.issuesFor(j, 'job', ctxWith({}));
  assert.ok(!has(issues, 'job_no_address'), 'no address should stay quiet without a client');
});

test('invoicing or later with no revenue line is a missing invoice', () => {
  // commissioning sits after invoiced in the flow, so an invoice should
  // already exist by then.
  for (const status of ['invoicing', 'invoiced', 'commissioning', 'completed']) {
    const j = Object.assign(goodJob(), { status });
    const issues = Q.issuesFor(j, 'job', ctxWith({}));
    assert.ok(has(issues, 'job_missing_invoice'), status + ' should want an invoice');
  }
});

// The business rule: an invoice and a bill can only exist once the status
// says invoicing. Won is committed work, not paperwork, so it is not chased
// for an invoice that cannot exist yet.
test('won is too early to want an invoice', () => {
  for (const status of ['quoting', 'quoted', 'on_hold', 'won']) {
    const j = Object.assign(goodJob(), { status });
    assert.ok(!has(Q.issuesFor(j, 'job', ctxWith({})), 'job_missing_invoice'), status);
  }
});

test('invoicing or later with no supplier bill is a missing bill', () => {
  for (const status of ['invoicing', 'invoiced', 'commissioning', 'completed']) {
    const j = Object.assign(goodJob(), { status });
    const issues = Q.issuesFor(j, 'job', ctxWith({}));
    assert.ok(has(issues, 'job_missing_bill'), status + ' should want a bill');
  }
  // Amber, not red. Plenty of jobs are labour only and genuinely have no
  // supplier bill, so this one must never shout.
  const j = Object.assign(goodJob(), { status: 'invoiced' });
  const issue = Q.issuesFor(j, 'job', ctxWith({})).find(i => i.code === 'job_missing_bill');
  assert.strictEqual(issue.severity, 'fix');
});

test('nothing before invoicing is asked for a bill', () => {
  for (const status of ['quoting', 'quoted', 'won', 'on_hold']) {
    const j = Object.assign(goodJob(), { status });
    assert.ok(!has(Q.issuesFor(j, 'job', ctxWith({})), 'job_missing_bill'), status);
  }
});

test('a supplier bill clears the flag, freight and duty do not', () => {
  const j = Object.assign(goodJob(), { status: 'invoiced' });
  // Freight is shipping. It has its own chip, and it is not the supplier
  // bill for the parts, so it must not clear this.
  const freightOnly = ctxWith({ financials: [{ id: 'f1', job: 'j1', direction: 'out', kind: 'freight', amount_gross: 500 }] });
  assert.ok(has(Q.issuesFor(j, 'job', freightOnly), 'job_missing_bill'), 'freight is not a bill');
  const withBill = ctxWith({ financials: [{ id: 'f2', job: 'j1', direction: 'out', kind: 'supplier_bill', amount_gross: 500 }] });
  assert.ok(!has(Q.issuesFor(j, 'job', withBill), 'job_missing_bill'));
  // A line may link with project_key instead of job.
  const viaKey = ctxWith({ financials: [{ id: 'f3', project_key: 'j1', direction: 'out', kind: 'custom_cost', amount_gross: 500 }] });
  assert.ok(!has(Q.issuesFor(j, 'job', viaKey), 'job_missing_bill'));
});

test('a revenue line clears the missing invoice flag, either link field', () => {
  const j = Object.assign(goodJob(), { status: 'invoiced' });
  const viaJob = ctxWith({ financials: [{ id: 'f1', job: 'j1', direction: 'in', amount_gross: 100 }] });
  assert.ok(!has(Q.issuesFor(j, 'job', viaJob), 'job_missing_invoice'));
  // A line may link with project_key instead. Missing it would report a
  // billed job as unbilled.
  const viaKey = ctxWith({ financials: [{ id: 'f1', project_key: 'j1', direction: 'in', amount_gross: 100 }] });
  assert.ok(!has(Q.issuesFor(j, 'job', viaKey), 'job_missing_invoice'));
});

test('a cost with no revenue is flagged even before the job is won', () => {
  const j = Object.assign(goodJob(), { status: 'quoting' });
  const ctx = ctxWith({ financials: [{ id: 'f1', job: 'j1', direction: 'out', amount_gross: 500 }] });
  const issues = Q.issuesFor(j, 'job', ctx);
  assert.ok(has(issues, 'job_cost_no_revenue'));
  assert.strictEqual(issues.find(i => i.code === 'job_cost_no_revenue').severity, 'blocker');
});

test('an invoicing job never carries both invoice flags at once', () => {
  const j = Object.assign(goodJob(), { status: 'invoiced' });
  const ctx = ctxWith({ financials: [{ id: 'f1', job: 'j1', direction: 'out', kind: 'supplier_bill', amount_gross: 500 }] });
  const issues = Q.issuesFor(j, 'job', ctx);
  assert.ok(has(issues, 'job_missing_invoice'));
  assert.ok(!has(issues, 'job_cost_no_revenue'), 'would be two chips for one problem');
});

// The complement of the invoice rule. A won job with money out and nothing
// billed is the case that loses money, such as a freight recharge that never
// got invoiced.
test('a won job with cost and no revenue is the unbilled cost flag', () => {
  const j = Object.assign(goodJob(), { status: 'won' });
  const ctx = ctxWith({ financials: [{ id: 'f1', job: 'j1', direction: 'out', kind: 'freight', amount_gross: 500 }] });
  const issues = Q.issuesFor(j, 'job', ctx);
  assert.ok(has(issues, 'job_cost_no_revenue'));
  assert.ok(!has(issues, 'job_missing_invoice'), 'would be two chips for one problem');
});

// ── the money that goes missing from the margin ───────────────────────────

test('foreign currency with no rate is a blocker, because it breaks the total', () => {
  const line = { id: 'f1', currency: 'USD', fx_rate: 0, amount_gross: 800, direction: 'out' };
  const issues = Q.issuesFor(line, 'finline', ctxWith({}));
  assert.ok(has(issues, 'fin_unconvertible'));
  assert.strictEqual(issues.find(i => i.code === 'fin_unconvertible').severity, 'blocker');
});

test('foreign currency with a rate is fine, and NZD never needs one', () => {
  const withRate = { id: 'f1', currency: 'USD', fx_rate: 1.72, amount_gross: 800 };
  assert.ok(!has(Q.issuesFor(withRate, 'finline', ctxWith({})), 'fin_unconvertible'));
  const nzd = { id: 'f2', currency: 'NZD', fx_rate: 0, amount_gross: 800 };
  assert.ok(!has(Q.issuesFor(nzd, 'finline', ctxWith({})), 'fin_unconvertible'));
});

test('a line read from a PDF is flagged until it is confirmed', () => {
  const parsed = { id: 'f1', currency: 'NZD', amount_gross: 100, parse_status: 'parsed' };
  assert.ok(has(Q.issuesFor(parsed, 'finline', ctxWith({})), 'fin_unconfirmed'));
  const confirmed = Object.assign({}, parsed, { parse_status: 'confirmed' });
  assert.ok(!has(Q.issuesFor(confirmed, 'finline', ctxWith({})), 'fin_unconfirmed'));
});

test('countsTowardTotals agrees with the financials engine on what is dropped', () => {
  const engine = require('../server/financials_engine');
  const cases = [
    { currency: 'NZD', amount_gross: 100, gst_treatment: 'incl_15', parse_status: 'confirmed' },
    { currency: 'NZD', amount_gross: 100, gst_treatment: 'incl_15', parse_status: 'parsed' },
    { currency: 'USD', amount_gross: 100, gst_treatment: 'no_gst', fx_rate: 0, parse_status: 'confirmed' },
    { currency: 'USD', amount_gross: 100, gst_treatment: 'no_gst', fx_rate: 1.7, parse_status: 'confirmed' },
  ];
  // If these two ever disagree, the dashboard flags one set of lines while the
  // maths quietly drops a different set, which is worse than having no flag.
  for (const l of cases) {
    assert.strictEqual(Q.countsTowardTotals(l), engine.isReady(l), JSON.stringify(l));
  }
});

// ── addresses ─────────────────────────────────────────────────────────────

test('a project inherits its address from its client', () => {
  const j = goodJob();
  const withClientAddr = ctxWith({});
  assert.ok(!has(Q.issuesFor(j, 'job', withClientAddr), 'job_no_address'));

  const noAddrAnywhere = ctxWith({ clients: [Object.assign(goodClient(), { address: '' })] });
  assert.ok(has(Q.issuesFor(j, 'job', noAddrAnywhere), 'job_no_address'));

  // A site address on the project itself is enough on its own.
  const siteOnly = Object.assign(goodJob(), { site_address: '9 Site Lane' });
  assert.ok(!has(Q.issuesFor(siteOnly, 'job', noAddrAnywhere), 'job_no_address'));
});

// ── clients ───────────────────────────────────────────────────────────────

test('client gaps are found and are not blockers', () => {
  const c = { id: 'c9', name: 'Bare' };
  const issues = Q.issuesFor(c, 'client', ctxWith({ contacts: [] }));
  assert.deepStrictEqual(codes(issues),
    ['client_no_address', 'client_no_contact', 'client_no_email', 'client_no_phone']);
  assert.ok(!issues.some(i => i.severity === 'blocker'));
});

test('a client with a contact record is not flagged as having none', () => {
  const ctx = ctxWith({ contacts: [{ id: 't1', client: 'c1', email: 'x@example.com' }] });
  assert.ok(!has(Q.issuesFor(goodClient(), 'client', ctx), 'client_no_contact'));
});

// ── chasing unpaid invoices ───────────────────────────────────────────────

test('an invoice awaiting payment for over 45 days is flagged', () => {
  const j = Object.assign(goodJob(), { status: 'invoiced' });
  const old = ctxWith({
    financials: [{ id: 'f1', job: 'j1', direction: 'in', amount_gross: 100, status: 'awaiting_payment', doc_date: '2026-06-01' }],
  });
  assert.ok(has(Q.issuesFor(j, 'job', old), 'job_awaiting_payment_old'));

  const recent = ctxWith({
    financials: [{ id: 'f1', job: 'j1', direction: 'in', amount_gross: 100, status: 'awaiting_payment', doc_date: '2026-08-05' }],
  });
  assert.ok(!has(Q.issuesFor(j, 'job', recent), 'job_awaiting_payment_old'));
});

test('a project marked paid but whose invoice is still a draft is flagged', () => {
  // Paid is a flag, not a status, and the job is completed. The flag must
  // still fire after a paid job is moved to completed.
  const j = Object.assign(goodJob(), { status: 'completed', paid: true });
  const ctx = ctxWith({ financials: [{ id: 'f1', job: 'j1', direction: 'in', amount_gross: 100, status: 'draft' }] });
  assert.ok(has(Q.issuesFor(j, 'job', ctx), 'job_revenue_not_raised'));

  const sent = ctxWith({ financials: [{ id: 'f1', job: 'j1', direction: 'in', amount_gross: 100, status: 'paid' }] });
  assert.ok(!has(Q.issuesFor(sent, 'job', sent), 'job_revenue_not_raised'));
});

// ── roll ups the dashboard depends on ─────────────────────────────────────

test('jobIssuesDeep pulls in problems from the lines under the project', () => {
  const j = Object.assign(goodJob(), { status: 'won' });
  const ctx = ctxWith({
    financials: [{ id: 'f1', job: 'j1', direction: 'in', amount_gross: 800, currency: 'USD', fx_rate: 0 }],
  });
  const deep = Q.jobIssuesDeep(j, ctx);
  assert.ok(has(deep, 'fin_unconvertible'), 'the project should carry its line problems');
  assert.ok(!has(Q.issuesFor(j, 'job', ctx), 'fin_unconvertible'), 'but not on its own record');
});

test('worst picks the highest severity present', () => {
  assert.strictEqual(Q.worst([]), null);
  assert.strictEqual(Q.worst([{ severity: 'info' }]), 'info');
  assert.strictEqual(Q.worst([{ severity: 'info' }, { severity: 'fix' }]), 'fix');
  assert.strictEqual(Q.worst([{ severity: 'fix' }, { severity: 'blocker' }]), 'blocker');
});

test('issues come back worst first', () => {
  const j = Object.assign(goodJob(), { client: '', value: 0, start_date: '' });
  const sevs = Q.issuesFor(j, 'job', ctxWith({})).map(i => i.severity);
  const rank = { blocker: 0, fix: 1, info: 2 };
  for (let i = 1; i < sevs.length; i++) {
    assert.ok(rank[sevs[i - 1]] <= rank[sevs[i]], 'severity order broke at ' + i);
  }
});

test('auditAll counts issues, records and blockers separately', () => {
  const ctx = ctxWith({
    jobs: [Object.assign(goodJob(), { client: '', value: 0 }), goodJob()],
    clients: [goodClient()],
    contacts: [{ id: 't1', client: 'c1', email: 'a@example.com' }],
  });
  const a = Q.auditAll(ctx);
  assert.ok(a.issueCount >= 2);
  assert.strictEqual(a.recordCount, 1, 'only the broken job should count as a record');
  assert.strictEqual(a.blockerCount, 1, 'the missing client is the only blocker');
  assert.strictEqual(a.byCode.job_no_client, 1);
  assert.strictEqual(a.byCode.job_no_value, 1);
});

// ── the contract the dashboard relies on ──────────────────────────────────

test('every rule is well formed, so a new one cannot half work', () => {
  const seen = new Set();
  for (const r of Q.RULES) {
    assert.ok(r.code && !seen.has(r.code), 'duplicate or missing code: ' + r.code);
    seen.add(r.code);
    assert.ok(Q.COLLECTION[r.entity], 'rule ' + r.code + ' has no collection for entity ' + r.entity);
    assert.ok(['blocker', 'fix', 'info'].includes(r.severity), r.code + ' has a bad severity');
    assert.ok(r.chip && r.chip.length <= 16, r.code + ' chip is missing or too long for a row');
    assert.ok(r.label && r.why, r.code + ' needs a label and an explanation');
    assert.strictEqual(typeof r.broken, 'function', r.code + ' has no test');
  }
});

test('every issue carries what the fix form needs', () => {
  const j = Object.assign(goodJob(), { client: '', value: 0 });
  for (const iss of Q.issuesFor(j, 'job', ctxWith({}))) {
    assert.strictEqual(iss.collection, 'jobs');
    assert.strictEqual(iss.recordId, 'j1');
    assert.ok(iss.recordName, 'needs something to show in the list');
  }
});

test('a rule that throws is ignored rather than taking the page down', () => {
  const exploding = { id: null, get status() { throw new Error('boom'); } };
  assert.doesNotThrow(() => Q.issuesFor(exploding, 'job', ctxWith({})));
});

// ── Splatt's own work ─────────────────────────────────────────────────────
// Splatt's own work, rather than a customer's, is filed under an internal
// client record so it does not read as a data gap. That record is not a real
// customer, so it is never asked for an address, an email, a phone number
// or a named contact.

const internalClient = () => ({
  id: 'internalclient0', name: 'Splatt Engineering (internal)',
  address: '', email_addresses: '', phone: '',
});

test('the internal client is never asked for an address or contact details', () => {
  const c = internalClient();
  const ctx = ctxWith({ clients: [c], contacts: [] });
  assert.deepStrictEqual(codes(Q.issuesFor(c, 'client', ctx)), []);
});

test('the internal record is recognised by name, not only by its id', () => {
  const c = Object.assign(internalClient(), { id: 'rebuilt-record' });
  const ctx = ctxWith({ clients: [c], contacts: [] });
  assert.deepStrictEqual(codes(Q.issuesFor(c, 'client', ctx)), []);
});

test('a real client with the same gaps is still flagged', () => {
  const c = { id: 'c9', name: 'Real Customer Ltd', address: '', email_addresses: '', phone: '' };
  const ctx = ctxWith({ clients: [c], contacts: [] });
  const found = codes(Q.issuesFor(c, 'client', ctx));
  assert.ok(has(Q.issuesFor(c, 'client', ctx), 'client_no_address'), 'still want the address chip');
  assert.strictEqual(found.length, 4, 'all four client gaps should still fire');
});

test('the Australian company is a real counterparty, not internal work', () => {
  const c = { id: 'splattau0000001', name: 'Splatt Engineering Group PTY',
    address: '', email_addresses: '', phone: '' };
  const ctx = ctxWith({ clients: [c], contacts: [] });
  assert.ok(codes(Q.issuesFor(c, 'client', ctx)).length > 0,
    'Splatt AU invoices and gets invoiced, so its gaps still matter');
});

// ── Quoted with no quote record ───────────────────────────────────────────
// A task can move a job to quoted on its own, so a job can reach quoted
// before anybody files the quote. The validator's gate_quoted rule only
// warns about that, and this chip keeps the gap visible on the row.

test('a job in quoted with no quote record carries the NO QUOTE chip', () => {
  const j = Object.assign(goodJob(), { status: 'quoted' });
  const issues = Q.issuesFor(j, 'job', ctxWith({ quotes: [] }));
  assert.ok(has(issues, 'job_quoted_no_quote'));
  assert.strictEqual(
    issues.find(i => i.code === 'job_quoted_no_quote').severity, 'fix');
});

test('a quote record against the job clears it', () => {
  const j = Object.assign(goodJob(), { status: 'quoted' });
  const ctx = ctxWith({ quotes: [{ id: 'q1', job: 'j1', title: 'QU-8090' }] });
  assert.ok(!has(Q.issuesFor(j, 'job', ctx), 'job_quoted_no_quote'));
});

test('an archived quote is not evidence that the client has one', () => {
  const j = Object.assign(goodJob(), { status: 'quoted' });
  const ctx = ctxWith({ quotes: [{ id: 'q1', job: 'j1', archived: true }] });
  assert.ok(has(Q.issuesFor(j, 'job', ctx), 'job_quoted_no_quote'));
});

test('somebody else quote does not clear this job', () => {
  const j = Object.assign(goodJob(), { status: 'quoted' });
  const ctx = ctxWith({ quotes: [{ id: 'q1', job: 'j2', title: 'QU-9999' }] });
  assert.ok(has(Q.issuesFor(j, 'job', ctx), 'job_quoted_no_quote'));
});

test('only quoted is asked for a quote record', () => {
  // quoting has not sent one yet, and everything past quoted has moved on.
  // Asking all of them would put an amber chip on most of the board.
  for (const status of ['quoting', 'won', 'invoicing', 'invoiced', 'paid']) {
    const j = Object.assign(goodJob(), { status });
    assert.ok(!has(Q.issuesFor(j, 'job', ctxWith({ quotes: [] })),
      'job_quoted_no_quote'), status);
  }
});

test('a cancelled or archived project is never asked for a quote', () => {
  const dead = Object.assign(goodJob(), { status: 'quoted', archived: true });
  assert.ok(!has(Q.issuesFor(dead, 'job', ctxWith({ quotes: [] })),
    'job_quoted_no_quote'));
});

// ── the status the board cannot read ─────────────────────────────────────

test('a project on a status outside the eleven is flagged', () => {
  // The rule asks whether the project is on a status the board knows how to
  // draw. Retired values such as 'paid' and 'active', unknown values and an
  // empty status are all flagged.
  for (const status of ['paid', 'active', 'in_progress', '']) {
    const j = Object.assign(goodJob(), { status });
    assert.ok(has(Q.issuesFor(j, 'job', ctxWith({})), 'job_legacy_status'),
      JSON.stringify(status) + ' should be flagged');
  }
});

test('every one of the eleven passes it', () => {
  const eleven = ['quoting', 'quoted', 'won', 'invoicing', 'invoiced',
                  'commissioning', 'lost', 'on_hold', 'cancelled',
                  'completed', 'reengage'];
  for (const status of eleven) {
    const j = Object.assign(goodJob(), { status });
    assert.ok(!has(Q.issuesFor(j, 'job', ctxWith({})), 'job_legacy_status'), status);
  }
});
