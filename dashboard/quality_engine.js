// quality_engine.js
// The record quality rules: what is missing or wrong on a record.
//
// Purpose
//   One list of rules (RULES) decides every gap flag the dashboard shows:
//   the chips on a row ("NO INVOICE", "NO ADDRESS"), the counts in the gap
//   filter pills, the numbers in the summary bar, and the quick-fix form that
//   fills a gap in. Because everything reads this one list, a chip on a row
//   and the count in a pill cannot disagree.
//
// Where it runs
//   In the browser, dashboard/index.html loads it with a plain script tag and
//   reads window.SplattQuality (the page refuses to start without it). In
//   Node, tests/test_quality_engine.js and tools/check_workload_counts.js
//   require it. It has no dependencies.
//
// Public API (window.SplattQuality, or module.exports)
//   issuesFor(record, entity, ctx)  every issue on one record, worst first.
//                                   entity is client | job | contact |
//                                   supplier | finline (a financial line)
//   jobIssuesDeep(job, ctx)         a project's own issues plus those on the
//                                   financial lines under it
//   auditAll(ctx)                   one pass over everything: { list, byCode,
//                                   issueCount, recordCount, blockerCount }
//   worst(issues)                   'blocker' | 'fix' | 'info' | null
//   RULES, rulesFor(entity), COLLECTION (entity -> PocketBase collection)
//   Helpers shared with the page: linesForJob, quotesForJob, revenueLines,
//   costLines, countsTowardTotals, isForeignUnconvertible, isWonOrLater,
//   isInvoicingOrLater, BILL_KINDS, hasAddress
//   ctx is { jobs, clients, contacts, suppliers, financials, quotes, now }.
//
// The invariant it protects
//   A flag is raised only when a record is really incomplete for its stage,
//   and never on records nobody expects anything from. Cancelled, lost and
//   archived projects raise nothing; one cause gives one chip (no client
//   suppresses "no address", and the unbilled-cost and missing-invoice flags
//   never fire together); the internal client record is never asked for
//   customer details. countsTowardTotals() must agree with
//   server/financials_engine.js about which financial lines are left out of
//   the margin, so a flag and the maths describe the same lines.
//
// How it is tested
//   tests/test_quality_engine.js (node --test, also run from pytest by
//   tests/test_node_suites.py) checks both directions for each rule: the
//   broken case is flagged and the complete case stays quiet. It also checks
//   that every rule is well formed and that countsTowardTotals agrees with
//   server/financials_engine.js.
//
// Adding a new check
//   Add one entry to RULES. Nothing else has to change: the dashboard builds
//   its chips, filter pills, counts and quick-fix forms from this list.
//
//   {
//     code:     'job_no_value',        unique id, used as the filter pill key
//     entity:   'job',                 client | job | contact | supplier | finline
//     severity: 'fix',                 blocker | fix | info
//     chip:     'NO VALUE',            short label for the chip on a row
//     label:    'No project value',    longer label for pills and lists
//     why:      'full sentence...',    tooltip and the attention band text
//     fix:      { field, kind, ... },  what the quick-fix form should edit
//     applies:  (rec, ctx) => bool,    is this rule even relevant here
//     broken:   (rec, ctx) => bool,    true means the record IS broken
//   }
//
// Severity
//   blocker  Red. Work cannot proceed or money is going unbilled. Counts in
//            the red number on the Workload summary bar. Currently: a
//            project with no client, work at invoicing with no invoice
//            recorded, money out with nothing billed, and a foreign-currency
//            line with no exchange rate (it silently drops out of the margin).
//   fix      Amber. A real gap, to fill in when there is a moment.
//   info     Grey. Worth knowing, never nags.

'use strict';

(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (typeof root !== 'undefined') root.SplattQuality = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {

  // ── Small helpers ────────────────────────────────────────────────────────
  const blank = (v) => v === null || v === undefined || String(v).trim() === '';
  const num = (v) => (Number(v) || 0);
  const DAY = 86400000;

  // Splatt's own work is filed under an internal client record so it does not
  // show up as a data gap. That record is not a real customer, so the client
  // checks below skip it: there is no address to visit, no invoice to send
  // and no contact to name. It is recognised by id or by a name containing
  // both "splatt" and "internal", so a rebuilt record still matches. The same
  // id is used by index.html.
  const INTERNAL_CLIENT_ID = 'internalclient0';
  const isInternalClient = (c) => {
    if (!c) return false;
    if (c.id === INTERNAL_CLIENT_ID) return true;
    const name = String(c.name || '').toLowerCase();
    return name.indexOf('splatt') !== -1 && name.indexOf('internal') !== -1;
  };

  const daysSince = (d, now) => {
    if (blank(d)) return null;
    const t = new Date(d).getTime();
    if (!isFinite(t)) return null;
    return Math.floor(((now || Date.now()) - t) / DAY);
  };

  // The eleven job statuses, in flow order. The same list as
  // core/job_status.py and JOB_STATUS_META in index.html, written out here so
  // the engine stays dependency free and the browser and the Node tests read
  // one file.
  const JOB_STATUSES = [
    'quoting', 'quoted', 'won', 'invoicing', 'invoiced', 'commissioning',
    'lost', 'on_hold', 'cancelled', 'completed', 'reengage',
  ];

  const CONFIRMED = ['won', 'invoicing', 'invoiced', 'commissioning'];
  const CLOSED_OK = ['completed'];
  const DEAD = ['cancelled', 'lost'];

  const isConfirmed = (s) => CONFIRMED.indexOf(s) !== -1;
  const isDead = (s) => DEAD.indexOf(s) !== -1;

  // "Won or later" answers the general "money should be moving" question.
  // It is NOT the line for paperwork (see isInvoicingOrLater).
  const isWonOrLater = (s) => isConfirmed(s) || CLOSED_OK.indexOf(s) !== -1;

  // Paperwork starts at invoicing. The business rule: an invoice and a bill
  // can only exist once the status says invoicing or later, so nothing before
  // that is ever chased for either. Commissioning and completed sit past that
  // line, so work that closed with no paperwork still gets asked about.
  // Payment is the paid flag on a completed job, not a status.
  const BILLABLE = ['invoicing', 'invoiced', 'commissioning', 'completed'];
  const isInvoicingOrLater = (s) => BILLABLE.indexOf(s) !== -1;

  // What counts as a bill: money out that Splatt is paying. Freight, duty and
  // import GST are shipping, tracked on their own, so a project with only
  // those has not had its supplier bill filed.
  const BILL_KINDS = ['supplier_bill', 'custom_cost'];

  // A job that nobody expects anything from right now. Cancelled, lost and
  // archived records must never raise flags, or the count becomes noise.
  const isDormant = (job) => !!(job && (job.archived || isDead(job.status)));

  // ── Financial line maths, mirrored from financials_engine.js ─────────────
  // Only the two facts the quality rules need: can this line be converted to
  // NZD, and does it count toward the totals yet. Kept small so there is one
  // obvious place to check if server/financials_engine.js changes. A test
  // compares the two.
  const isForeignUnconvertible = (l) => {
    const cur = String(l.currency || 'NZD').toUpperCase();
    return cur !== 'NZD' && !(num(l.fx_rate) > 0);
  };
  const countsTowardTotals = (l) => l.parse_status !== 'parsed' && !isForeignUnconvertible(l);

  // Every live quote against a job. Archived ones do not count: a quote
  // that was withdrawn is not evidence that the client has one.
  const quotesForJob = (job, ctx) => {
    const all = (ctx && ctx.quotes) || [];
    const id = job && job.id;
    if (!id) return [];
    return all.filter(q => q && q.job === id && !q.archived);
  };

  // Every financial line belonging to a job, whichever way it was linked.
  // A line may carry project_key or job. Both are honoured so no line is
  // missed and reported as an absent invoice by mistake.
  const linesForJob = (job, ctx) => {
    const all = (ctx && ctx.financials) || [];
    const id = job && job.id;
    if (!id) return [];
    return all.filter(l => l && (l.job === id || l.project_key === id));
  };
  const revenueLines = (job, ctx) => linesForJob(job, ctx).filter(l => l.direction === 'in');
  const costLines = (job, ctx) => linesForJob(job, ctx).filter(l => l.direction === 'out');

  // Does this thing have an address to put on a map. A project carries its
  // own site_address, otherwise it inherits the client's. The same rule the
  // map uses, so the flag and the map agree.
  const hasAddress = (job, ctx) => {
    if (job && !blank(job.site_address)) return true;
    const client = ((ctx && ctx.clients) || []).find(c => c.id === (job && job.client));
    return !!(client && !blank(client.address));
  };

  // ── THE RULES ────────────────────────────────────────────────────────────
  // Order matters only for display. Blockers are listed first within each
  // entity so the worst thing about a record is the first chip you see.
  const RULES = [

    // ---- Projects ---------------------------------------------------------
    {
      code: 'job_no_client',
      entity: 'job',
      severity: 'blocker',
      chip: 'NO CLIENT',
      label: 'No client linked',
      why: 'This project is not linked to a client, so it cannot be invoiced, cannot be filed, and will never appear on the map.',
      fix: { field: 'client', kind: 'relation', of: 'clients', labelText: 'Client' },
      applies: (j) => !isDormant(j),
      broken: (j) => blank(j.client),
    },
    {
      code: 'job_missing_invoice',
      entity: 'job',
      severity: 'blocker',
      chip: 'NO INVOICE',
      label: 'Missing invoice',
      why: 'This project is at invoicing or beyond and has no revenue line against it, so the invoice has been raised somewhere and never recorded here.',
      fix: { kind: 'financial_line', direction: 'in', lineKind: 'client_invoice', labelText: 'Add invoice line' },
      applies: (j) => !isDormant(j) && isInvoicingOrLater(j.status),
      broken: (j, ctx) => revenueLines(j, ctx).length === 0,
    },
    {
      code: 'job_missing_bill',
      entity: 'job',
      severity: 'fix',
      chip: 'NO BILLS',
      label: 'Missing bills',
      why: 'This project is at invoicing or beyond and carries no supplier bill, so either nothing was ever bought for it or the bill has not been filed. Freight and duty do not count here, they are shipping and tracked on their own.',
      fix: { kind: 'financial_line', direction: 'out', lineKind: 'supplier_bill', labelText: 'Add bill line' },
      applies: (j) => !isDormant(j) && isInvoicingOrLater(j.status),
      broken: (j, ctx) => costLines(j, ctx).filter(l => BILL_KINDS.indexOf(l.kind) !== -1).length === 0,
    },
    {
      code: 'job_cost_no_revenue',
      entity: 'job',
      severity: 'blocker',
      chip: 'UNBILLED COST',
      label: 'Cost with nothing billed',
      why: 'Money has gone out on this project and nothing has been billed back. Freight and duty recharges get missed exactly this way.',
      // Only fires where job_missing_invoice does not, so one project never
      // carries two chips saying the same thing. That rule starts at
      // invoicing, so this one covers everything short of it: money has gone
      // out on a project that has not reached invoicing yet.
      applies: (j) => !isDormant(j) && !isInvoicingOrLater(j.status),
      broken: (j, ctx) => costLines(j, ctx).length > 0 && revenueLines(j, ctx).length === 0,
    },
    {
      code: 'job_revenue_not_raised',
      entity: 'job',
      severity: 'fix',
      chip: 'INVOICE NOT SENT',
      label: 'Invoice line still a draft',
      why: 'The project is invoiced or already paid but its revenue line is still sitting at not raised or draft, so the paperwork and the status disagree.',
      fix: { kind: 'financial_line_status', labelText: 'Invoice status' },
      // Paid is a flag, not a status, so a paid job keeps being asked
      // whatever status it has moved on to.
      applies: (j, ctx) => !isDormant(j)
        && (['invoiced', 'commissioning'].indexOf(j.status) !== -1 || !!j.paid)
        && revenueLines(j, ctx).length > 0,
      broken: (j, ctx) => revenueLines(j, ctx).every(l => ['not_raised', 'draft'].indexOf(l.status) !== -1),
    },
    {
      code: 'job_quoted_no_quote',
      entity: 'job',
      severity: 'fix',
      chip: 'NO QUOTE',
      label: 'Quoted with no quote record',
      // A task can move a job to quoted on its own the moment the "send
      // quote" task is ticked off, often before anyone files the quote. The
      // validator's gate_quoted rule reports the gap as a warning, and this
      // chip keeps it visible on the row itself.
      why: 'This project says it has been quoted and no quote record is filed against it. The quote may exist as a document or only in an email, but nothing here can tell you what was offered or for how much, so the pipeline figure and the chase both have nothing to stand on.',
      applies: (j) => !isDormant(j) && j.status === 'quoted',
      broken: (j, ctx) => quotesForJob(j, ctx).length === 0,
    },
    {
      code: 'job_no_value',
      entity: 'job',
      severity: 'fix',
      chip: 'NO VALUE',
      label: 'No project value',
      why: 'No dollar value on this project, so it contributes nothing to the open value figure and the pipeline reads lower than it really is.',
      fix: { field: 'value', kind: 'money', labelText: 'Project value' },
      applies: (j) => !isDormant(j),
      broken: (j) => !(num(j.value) > 0),
    },
    {
      code: 'job_no_address',
      entity: 'job',
      severity: 'fix',
      chip: 'NO ADDRESS',
      label: 'No address',
      why: 'Neither this project nor its client has an address, so it will not appear on the map.',
      fix: { field: 'site_address', kind: 'text', labelText: 'Site address' },
      // Needs a client first. If there is no client, job_no_client is the
      // real problem and this would just be a second chip for one cause.
      applies: (j) => !isDormant(j) && !blank(j.client),
      broken: (j, ctx) => !hasAddress(j, ctx),
    },
    {
      code: 'job_no_due_date',
      entity: 'job',
      severity: 'fix',
      chip: 'NO DUE DATE',
      label: 'No due date',
      why: 'No due date, so this project can never show as overdue and will quietly go stale instead of chasing you.',
      fix: { field: 'due_date', kind: 'date', labelText: 'Due date' },
      applies: (j) => !isDormant(j),
      broken: (j) => blank(j.due_date),
    },
    {
      // A project on anything outside the eleven statuses, or on none at all,
      // is one that every filter, count and colour on the dashboard would
      // have to guess about.
      code: 'job_legacy_status',
      entity: 'job',
      severity: 'fix',
      chip: 'NEEDS STATUS',
      label: 'Status the board cannot read',
      why: 'This project is on a status that is not one of the eleven, or on none at all, so it falls out of the filters and draws as a grey unknown chip.',
      fix: { field: 'status', kind: 'select', labelText: 'Status' },
      applies: (j) => !isDormant(j),
      broken: (j) => JOB_STATUSES.indexOf(j.status) === -1,
    },
    {
      code: 'job_awaiting_payment_old',
      entity: 'job',
      severity: 'fix',
      chip: 'UNPAID 45D+',
      label: 'Invoice unpaid over 45 days',
      why: 'An invoice on this project has been awaiting payment for more than 45 days. Time to chase it.',
      applies: (j, ctx) => !isDormant(j) && revenueLines(j, ctx).length > 0,
      broken: (j, ctx) => revenueLines(j, ctx).some(l => {
        if (l.status !== 'awaiting_payment') return false;
        const d = daysSince(l.doc_date || l.created, ctx && ctx.now);
        return d !== null && d > 45;
      }),
    },
    {
      code: 'job_no_start_date',
      entity: 'job',
      severity: 'info',
      chip: 'NO START',
      label: 'No start date',
      why: 'No start date, so this project cannot be placed on the Gantt or the calendar.',
      fix: { field: 'start_date', kind: 'date', labelText: 'Start date' },
      applies: (j) => !isDormant(j),
      broken: (j) => blank(j.start_date),
    },

    // ---- Clients ----------------------------------------------------------
    {
      code: 'client_no_address',
      entity: 'client',
      severity: 'fix',
      chip: 'NO ADDRESS',
      label: 'Client has no address',
      why: 'No address on this client, so none of their projects can be placed on the map.',
      fix: { field: 'address', kind: 'text', labelText: 'Address' },
      applies: (c) => !isInternalClient(c),
      broken: (c) => blank(c.address),
    },
    {
      code: 'client_no_email',
      entity: 'client',
      severity: 'fix',
      chip: 'NO EMAIL',
      label: 'Client has no email',
      why: 'No email address on file, so quotes and invoices cannot be sent without going hunting for it first.',
      fix: { field: 'email_addresses', kind: 'text', labelText: 'Email addresses' },
      applies: (c) => !isInternalClient(c),
      broken: (c) => blank(c.email_addresses),
    },
    {
      code: 'client_no_contact',
      entity: 'client',
      severity: 'fix',
      chip: 'NO CONTACT',
      label: 'No named contact',
      why: 'No contact record for this client, so there is no named person to write to.',
      fix: { kind: 'new_contact', labelText: 'Add a contact' },
      applies: (c) => !isInternalClient(c),
      broken: (c, ctx) => !((ctx && ctx.contacts) || []).some(t => t && t.client === c.id),
    },
    {
      code: 'client_no_phone',
      entity: 'client',
      severity: 'info',
      chip: 'NO PHONE',
      label: 'Client has no phone',
      why: 'No phone number on file. Fine for most, awkward when something is urgent.',
      fix: { field: 'phone', kind: 'text', labelText: 'Phone' },
      applies: (c) => !isInternalClient(c),
      broken: (c) => blank(c.phone),
    },

    // ---- Financial lines --------------------------------------------------
    // These make the numbers wrong rather than absent. financials_engine.js
    // deliberately leaves unconfirmed and unconvertible lines out of revenue,
    // cost and margin, so without a flag the true-win (margin) figure would
    // be understated while reading as complete.
    {
      code: 'fin_unconvertible',
      entity: 'finline',
      severity: 'blocker',
      chip: 'NO FX RATE',
      label: 'Money missing from the margin',
      why: 'This line is in a foreign currency with no exchange rate, so it cannot be converted and is being left out of revenue, cost and margin entirely. The true win figure is wrong until this is filled in.',
      fix: { field: 'fx_rate', kind: 'number', labelText: 'FX rate, NZD per 1 unit' },
      applies: () => true,
      broken: (l) => isForeignUnconvertible(l),
    },
    {
      code: 'fin_unconfirmed',
      entity: 'finline',
      severity: 'fix',
      chip: 'UNCONFIRMED',
      label: 'Line read from a PDF, not confirmed',
      why: 'This line was read automatically from a document and has not been confirmed, so it is not counted in the totals yet. Check it and confirm it.',
      applies: () => true,
      broken: (l) => l.parse_status === 'parsed',
    },
    {
      code: 'fin_zero_amount',
      entity: 'finline',
      severity: 'fix',
      chip: 'ZERO AMOUNT',
      label: 'Line has no amount',
      why: 'This line has no amount on it, so it adds nothing and is probably a half finished entry.',
      fix: { field: 'amount_gross', kind: 'money', labelText: 'Gross amount' },
      applies: () => true,
      broken: (l) => !(num(l.amount_gross) > 0),
    },
    {
      code: 'fin_not_in_xero',
      entity: 'finline',
      severity: 'info',
      chip: 'NOT IN XERO',
      label: 'Not in Xero yet',
      why: 'This line has not been ticked off as being in Xero, so the books and the dashboard may not match.',
      applies: (l) => countsTowardTotals(l),
      broken: (l) => !l.in_xero,
    },

    // ---- Contacts ---------------------------------------------------------
    {
      code: 'contact_no_email',
      entity: 'contact',
      severity: 'fix',
      chip: 'NO EMAIL',
      label: 'Contact has no email',
      why: 'A named contact with no email address cannot actually be contacted.',
      fix: { field: 'email', kind: 'text', labelText: 'Email' },
      applies: () => true,
      broken: (c) => blank(c.email),
    },
    {
      code: 'contact_no_client',
      entity: 'contact',
      severity: 'fix',
      chip: 'NO CLIENT',
      label: 'Contact not linked to a client',
      why: 'This contact is not attached to any client, so they will never show up where you need them.',
      fix: { field: 'client', kind: 'relation', of: 'clients', labelText: 'Client' },
      applies: () => true,
      broken: (c) => blank(c.client),
    },

    // ---- Suppliers --------------------------------------------------------
    {
      code: 'supplier_no_email',
      entity: 'supplier',
      severity: 'info',
      chip: 'NO EMAIL',
      label: 'Supplier has no email',
      why: 'No email on this supplier, so ordering means finding the address somewhere else first.',
      fix: { field: 'email', kind: 'text', labelText: 'Email' },
      applies: () => true,
      broken: (s) => blank(s.email),
    },
  ];

  // Which PocketBase collection each entity type is saved back to. The
  // quick-fix form uses this to build its PATCH, so it never has to guess.
  const COLLECTION = {
    job: 'jobs',
    client: 'clients',
    contact: 'contacts',
    supplier: 'suppliers',
    finline: 'project_financials',
  };

  const SEVERITY_ORDER = { blocker: 0, fix: 1, info: 2 };

  function rulesFor(entity) {
    return RULES.filter(r => r.entity === entity);
  }

  // Every issue on one record. The returned objects carry everything the UI
  // needs, so no view has to look a rule up again by code.
  function issuesFor(record, entity, ctx) {
    if (!record) return [];
    const c = ctx || {};
    const out = [];
    for (const rule of rulesFor(entity)) {
      let relevant = true;
      try { relevant = rule.applies ? !!rule.applies(record, c) : true; } catch (e) { relevant = false; }
      if (!relevant) continue;
      let bad = false;
      try { bad = !!rule.broken(record, c); } catch (e) { bad = false; }
      if (!bad) continue;
      out.push({
        code: rule.code,
        entity: entity,
        severity: rule.severity,
        chip: rule.chip,
        label: rule.label,
        why: rule.why,
        fix: rule.fix || null,
        collection: COLLECTION[entity],
        recordId: record.id,
        recordName: record.title || record.name || record.ref || record.description || record.id,
      });
    }
    return out.sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]);
  }

  const worst = (issues) => {
    if (!issues || !issues.length) return null;
    if (issues.some(i => i.severity === 'blocker')) return 'blocker';
    if (issues.some(i => i.severity === 'fix')) return 'fix';
    return 'info';
  };

  // Everything wrong with a project, including the financial lines hanging off
  // it. The Workload row needs this, because a line with no exchange rate is a
  // problem with the project even though it lives on another record.
  function jobIssuesDeep(job, ctx) {
    const own = issuesFor(job, 'job', ctx);
    const lines = linesForJob(job, ctx);
    const lineIssues = [];
    for (const l of lines) {
      for (const iss of issuesFor(l, 'finline', ctx)) lineIssues.push(iss);
    }
    return own.concat(lineIssues);
  }

  // One pass over the whole database. Returns a flat list plus the counts the
  // filter pills and the summary bar read straight off.
  function auditAll(ctx) {
    const c = ctx || {};
    const list = [];
    const push = (records, entity) => {
      for (const r of (records || [])) {
        for (const iss of issuesFor(r, entity, c)) list.push(iss);
      }
    };
    push(c.jobs, 'job');
    push(c.clients, 'client');
    push(c.contacts, 'contact');
    push(c.suppliers, 'supplier');
    push(c.financials, 'finline');

    const byCode = {};
    const recordsSeen = {};
    let blockerCount = 0;
    for (const iss of list) {
      byCode[iss.code] = (byCode[iss.code] || 0) + 1;
      const key = iss.entity + ':' + iss.recordId;
      if (!recordsSeen[key]) recordsSeen[key] = [];
      recordsSeen[key].push(iss);
      if (iss.severity === 'blocker') blockerCount++;
    }
    return {
      list: list,
      byCode: byCode,
      issueCount: list.length,
      recordCount: Object.keys(recordsSeen).length,
      blockerCount: blockerCount,
    };
  }

  return {
    RULES: RULES,
    COLLECTION: COLLECTION,
    rulesFor: rulesFor,
    issuesFor: issuesFor,
    jobIssuesDeep: jobIssuesDeep,
    auditAll: auditAll,
    worst: worst,
    linesForJob: linesForJob,
    quotesForJob: quotesForJob,
    revenueLines: revenueLines,
    costLines: costLines,
    countsTowardTotals: countsTowardTotals,
    isForeignUnconvertible: isForeignUnconvertible,
    isWonOrLater: isWonOrLater,
    isInvoicingOrLater: isInvoicingOrLater,
    BILL_KINDS: BILL_KINDS,
    hasAddress: hasAddress,
  };
});
