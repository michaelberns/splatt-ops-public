// filter_engine.js
// The one filter pipeline behind the Workload page, plus every task date
// rule the dashboard uses.
//
// Purpose
//   Given the jobs (projects), assignments (tasks), clients, financial lines
//   and quality flags, and the current state of the filter bar, decide which
//   project cards and task rows are shown, what the summary line says, and
//   the number on every filter chip. The rows and the counts come out of the
//   same function, so they cannot disagree.
//
// Where it runs
//   In the browser, dashboard/index.html loads it with a plain script tag and
//   reads window.SplattFilters. In Node, tests/test_filter_engine.js and
//   tools/check_workload_counts.js require it. The wrapper below handles both.
//
// Public API (window.SplattFilters, or module.exports)
//   runWorkload(dataset, filters, { recordCodes })
//       The one call the view makes. Returns { projects, tasks, hostProjects,
//       scopeActive, filters, summary, counts }.
//   runCore(dataset, filters)       one pass of the pipeline, no counts
//   countsFor(dataset, filters, recordCodes)   the number on every chip
//   summarise(result)               open and overdue totals for the header
//   countCards(result)              what counts as one card (see below)
//   describeFilters, isDefaultFilters, FILTER_HELP, STATUS_LABELS,
//   WHEN_LABELS, SCOPE_LABELS       the words the filter bar and its info
//                                   panel show
//   Task dates: realDueOf, actionableDateOf, deadlineOf, daysLateOf,
//       sectionKind, isParked, isTaskOverdueLive, lateTierOf, enrichTask
//   Projects: isStale, countsTowardWorkload, tlIsOverdue, shippingStatus,
//       billsStatus, supplierStatus, isJobCritical, and the status groups
//   A dataset is { jobs, assignments, clients, financials, quality, now }.
//   `quality` is { byJob: { <job id>: [issue, ...] } } from quality_engine.js.
//
// The invariant it protects
//   A chip's count equals the number of project cards you get by clicking
//   it, with every other filter left as it is. The count is not estimated:
//   countsFor() re-runs the pipeline once per chip with that one value
//   swapped and counts the cards with countCards(). The view draws the same
//   cards, and the tests count with the same function.
//
//   Why: when the counts and the rows were computed by two separate sets of
//   predicates, they drifted, and a chip could promise more cards than the
//   page then showed.
//
// How it is tested
//   tests/test_filter_engine.js (node --test, also run from pytest by
//   tests/test_node_suites.py) builds a small fictional dataset and checks,
//   for every chip and every pair of chips from different rows, that the
//   promised count equals the cards produced by clicking. It also covers the
//   date rules and the status groups. tools/check_workload_counts.js runs the
//   same chip check against a live PocketBase, and tools/check_workload_ui.js
//   checks it in a real browser against the rendered page.
//
// Adding a filter
//   Add its predicate to the right pass in runCore(), then add its key to the
//   list the matching counts loop reads (STATUS_KEYS, DATE_WINDOWS or
//   SCOPE_KEYS). Nothing else has to change.
//
// Terms
//   real due date  the date a task was really due, which survives rescheduling
//   parked         a task in the Stalled, Waiting or Backlog column
//   host project   a project drawn only because an open task sits under it
//   gap            a quality flag from quality_engine.js, such as "no invoice"

'use strict';

(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (typeof root !== 'undefined') root.SplattFilters = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {

  // ── Status groups ────────────────────────────────────────────────────────
  // The colours and labels stay in index.html with the rest of the theme.
  // Which bucket a status falls in is a filter question, so it lives here.
  const STATUS_GROUPS = {
    open:            ['quoting', 'quoted'],
    confirmed:       ['won', 'invoicing', 'invoiced', 'commissioning'],
    closed:          ['completed'],
    closed_negative: ['lost', 'cancelled'],
    other:           ['on_hold'],
    parked:          ['reengage'],
  };

  const isOpenStatus      = (s) => STATUS_GROUPS.open.indexOf(s) !== -1;
  const isConfirmedStatus = (s) => STATUS_GROUPS.confirmed.indexOf(s) !== -1;
  const isReengageStatus  = (s) => STATUS_GROUPS.parked.indexOf(s) !== -1;
  const isTerminalStatus  = (s) => STATUS_GROUPS.closed.indexOf(s) !== -1
                                || STATUS_GROUPS.closed_negative.indexOf(s) !== -1;
  const isPipelineStatus  = (s) => isOpenStatus(s) || isConfirmedStatus(s);

  // ── Dates ────────────────────────────────────────────────────────────────
  const DAY_MS = 86400000;
  const startOfDay = (d) => { const x = new Date(d === undefined ? Date.now() : d); x.setHours(0, 0, 0, 0); return x; };
  const endOfDay   = (d) => { const x = new Date(d === undefined ? Date.now() : d); x.setHours(23, 59, 59, 999); return x; };
  const parseLocalDate = (s) => {
    if (!s) return null;
    const parts = String(s).split('-').map(Number);
    if (parts.length !== 3 || parts.some(isNaN)) return null;
    return new Date(parts[0], parts[1] - 1, parts[2]);
  };
  const daysBetween = (from, to) => Math.round(
    (new Date(to).setHours(0, 0, 0, 0) - new Date(from).setHours(0, 0, 0, 0)) / DAY_MS
  );

  const isToday = (dateStr, now) => {
    if (!dateStr) return false;
    const d = new Date(dateStr);
    const t = new Date(now === undefined ? Date.now() : now);
    return d.getFullYear() === t.getFullYear()
        && d.getMonth() === t.getMonth()
        && d.getDate() === t.getDate();
  };

  // ── A task's dates ───────────────────────────────────────────────────────
  //
  // Tasks in the Stalled column have their Todoist due date removed on
  // purpose, so that long-dead work stops showing up in Today, Upcoming and
  // every overdue filter. Their real date lives on instead, pinned in the
  // description ("Real due: 2026-07-12") and mirrored to assignments.real_due.
  //
  // Three sources, in order of how much they can be trusted:
  //   real_due   the PocketBase mirror, written by the daemon sweep
  //   the pin    in the description, written by every tool here
  //   due_date   the live Todoist date, absent on anything Stalled
  //
  // The pin is second rather than last because the mirror only fills in as
  // the daemon sweeps, while the pin is written the moment a task is touched.
  //
  // The regex reads the whole description rather than only the first line,
  // because other header lines (such as a Project line) can sit above the
  // pin. core/realdue.py reads the header block for the same reason.
  const REAL_DUE_PIN = /Real due:\s*(\d{4}-\d{2}-\d{2})/;
  const realDueOf = (task) => {
    if (!task) return null;
    if (task.real_due) return task.real_due;
    const pin = REAL_DUE_PIN.exec(task.description || '');
    if (pin) return pin[1];
    return task.due_date || null;
  };

  // When it lands, which is NOT the same as how late it is. A waiting task's
  // pin can say "this was due 12 July" while its Todoist date says "chase it
  // on 2 September". Both are true and they answer different questions.
  //   actionableDateOf   when it lands, the calendar, sorting, "this week"
  //   realDueOf          how late it is, the escalation ladder, age
  const actionableDateOf = (task) => {
    if (!task) return null;
    return task.due_date || realDueOf(task);
  };

  // The hard deadline from Todoist, separate from the due date. A due date is
  // when the operator plans to touch a task. A deadline is when it stops
  // mattering.
  const deadlineOf = (task) => (task && task.deadline) || null;

  const daysLateOf = (task, now) => {
    const due = realDueOf(task);
    return due ? daysBetween(due, now === undefined ? new Date() : now) : null;
  };

  // ── Where a task sits ────────────────────────────────────────────────────
  // Read off the Todoist column (section), because the column is what the
  // operator moves tasks between.
  const sectionKind = (task) => {
    const name = ((task && (task.section_name || task._sectionName)) || '').toLowerCase();
    if (name.indexOf('stalled') !== -1) return 'stalled';
    if (name.indexOf('waiting on client') !== -1) return 'waiting_client';
    if (name.indexOf('waiting on supplier') !== -1) return 'waiting_supplier';
    if (name.indexOf('backlog') !== -1) return 'backlog';
    if (name.indexOf('today') !== -1) return 'today';
    if (name.indexOf('upcoming') !== -1) return 'upcoming';
    if (name.indexOf('overdue') !== -1) return 'overdue';
    return '';
  };
  const WAITING_KINDS = ['waiting_client', 'waiting_supplier'];
  const isParked = (task) => {
    const kind = sectionKind(task);
    return kind === 'stalled' || kind === 'backlog' || WAITING_KINDS.indexOf(kind) !== -1;
  };

  // Overdue AND on the desk today (not parked). One definition, shared by the
  // stat cards, the chips and the row badges.
  const isTaskOverdueLive = (task, now) => {
    if (!task || task.status === 'completed' || task.archived) return false;
    if (isParked(task)) return false;
    const due = realDueOf(task);
    return !!due && new Date(due) < new Date(now === undefined ? Date.now() : now);
  };

  const tlIsOverdue = (job, now) => !!(job && job.due_date)
    && new Date(job.due_date) < new Date(now === undefined ? Date.now() : now)
    && !isTerminalStatus(job.status);

  // ── How loudly late work speaks ──────────────────────────────────
  // Day to day lateness is normal, so it stays quiet.
  // Amber from a week, red only from two weeks, which is the same day the
  // sweep's ladder starts warning about a stall. Client promises are the
  // exception: a date given to a client earns red as soon as it slips.
  //   ''          not late at all
  //   'quiet'     late under 7 days, part of the day to day
  //   'aging'     late 7 to 13 days
  //   'attention' late 14+ days, tagged stall-warning, or a client promise
  const lateTierOf = (task, now) => {
    if (!isTaskOverdueLive(task, now)) return '';
    const labels = String((task && task.labels) || '').toLowerCase();
    const late = daysLateOf(task, now) || 0;
    if (late >= 14 || labels.indexOf('stall-warning') !== -1
        || labels.indexOf('client-promise') !== -1) return 'attention';
    if (late >= 7) return 'aging';
    return 'quiet';
  };

  const tlDaysLateOf = (job, now) => (job && job.due_date)
    ? daysBetween(job.due_date, now === undefined ? new Date() : now)
    : null;

  const tlLateTierOf = (job, now) => {
    if (!tlIsOverdue(job, now)) return '';
    const late = tlDaysLateOf(job, now) || 0;
    return late >= 14 ? 'attention' : late >= 7 ? 'aging' : 'quiet';
  };

  // ── Is a project stale ───────────────────────────────────────────────────
  // Terminal, no dates at all, or a due date more than 45 days behind. An
  // on-hold project with an old start date counts too.
  const STALE_DAYS = 45;
  const isStale = (job, now) => {
    if (!job) return false;
    if (isTerminalStatus(job.status)) return true;
    if (!job.start_date && !job.due_date) return true;
    const today = new Date(now === undefined ? Date.now() : now);
    const cutoff = new Date(today.getTime() - STALE_DAYS * DAY_MS);
    const start = job.start_date ? new Date(job.start_date) : null;
    const due = job.due_date ? new Date(job.due_date) : null;
    if (due && due < cutoff) return true;
    if (!due && start && start < cutoff && job.status === 'on_hold') return true;
    return false;
  };

  // A project, and therefore its tasks, should not count toward the open or
  // overdue numbers when it is archived, on hold, terminal or stale. Those
  // are not being worked, and counting them inflates the true figures.
  const countsTowardWorkload = (job, now) => {
    if (!job) return false;
    if (job.archived) return false;
    if (job.status === 'on_hold') return false;
    if (isTerminalStatus(job.status)) return false;
    if (isStale(job, now)) return false;
    return true;
  };

  // ── The date window ──────────────────────────────────────────────────────
  //   recent   touched (created OR updated) within the last 7 days
  //   overdue  due strictly before today, and for projects still a live
  //            workload item
  //   today    due today
  //   week     due within the next 7 days, today included
  //   custom   due within the From/To range, either bound optional
  // Items with no due date are excluded by every window except all and recent.
  const DATE_WINDOWS = ['all', 'overdue', 'today', 'week', 'recent'];
  const passesDateWindow = (item, df, customStart, customEnd, isLive, now) => {
    if (!item || !df || df === 'all') return true;
    const nowMs = now === undefined ? Date.now() : new Date(now).getTime();
    if (df === 'recent') {
      const sevenAgo = new Date(nowMs - 7 * DAY_MS);
      const c = item.created ? new Date(item.created) : null;
      const u = item.updated ? new Date(item.updated) : null;
      return !!((c && c >= sevenAgo) || (u && u >= sevenAgo));
    }
    const due = item.due_date ? new Date(item.due_date) : null;
    if (df === 'overdue') {
      if (!due || !(due < startOfDay(nowMs))) return false;
      return isLive ? !!isLive(item) : true;
    }
    if (df === 'today') return !!due && due >= startOfDay(nowMs) && due <= endOfDay(nowMs);
    if (df === 'week') {
      if (!due) return false;
      return due >= startOfDay(nowMs) && due <= endOfDay(new Date(nowMs + 7 * DAY_MS));
    }
    if (df === 'custom') {
      if (!due) return false;
      const s = parseLocalDate(customStart);
      const e = parseLocalDate(customEnd);
      if (s && due < startOfDay(s)) return false;
      if (e && due > endOfDay(e)) return false;
      return true;
    }
    return true;
  };

  // Round to cents. Floating point addition of money without this puts
  // 0.30000000000000004 on a card.
  const r2 = (n) => Math.round((Number(n) || 0) * 100) / 100;

  // ── Money owed on a project ──────────────────────────────────────────────
  const SHIPPING_KINDS = ['freight', 'duty', 'import_gst'];
  const shippingStatus = (job, financials) => {
    if (!job) return 'none';
    const involves = !!(job.has_shipping || job.shipping_paid)
      || (financials || []).some(l => l && (l.job === job.id || l.project_key === job.id)
            && l.direction === 'out' && SHIPPING_KINDS.indexOf(l.kind) !== -1);
    if (!involves) return 'none';
    return job.shipping_paid ? 'paid' : 'due';
  };

  // ── Bills on a project ───────────────────────────────────────────────────
  // The money out side of project_financials, which is the only place a bill
  // is recorded. Freight, duty and import GST are left to shippingStatus above
  // so the same dollar is never counted by two chips.
  //
  // Gated on the client having actually been invoiced. A project that is only
  // quoting, quoted or won has nothing out the door, so it stays quiet no
  // matter what bills are sitting against it.
  const BILL_KINDS = ['supplier_bill', 'custom_cost'];
  // Paperwork starts at invoicing, the same line quality_engine.js draws for
  // the missing invoice and missing bill flags. One rule, so this chip and
  // the gap chips agree about when a bill is expected to exist. Payment is a
  // flag rather than a status, so isClientInvoiced checks it separately.
  const INVOICED_STATUSES = ['invoicing', 'invoiced', 'commissioning', 'completed'];

  // Paid money in the bank means the client was invoiced, whatever the status
  // field still says, so the paid flag counts as invoiced too.
  const isClientInvoiced = (job) => !!job
    && (INVOICED_STATUSES.indexOf(job.status) !== -1 || !!job.paid);

  const billsOf = (job, financials) => (financials || []).filter(l => l
    && l.direction === 'out'
    && (l.job === job.id || l.project_key === job.id)
    && BILL_KINDS.indexOf(l.kind) !== -1);

  // What a bill line is worth in NZD, GST inclusive, because that is the
  // figure that leaves the bank. A foreign line with no rate cannot be turned
  // into dollars, so it is counted as a line but left out of the total rather
  // than guessed at par.
  const billNzd = (l) => {
    const gross = (Number(l.amount_gross) || 0) * (l.gst_treatment === 'add_15' ? 1.15 : 1);
    const cur = String(l.currency || 'NZD').toUpperCase();
    const fx = Number(l.fx_rate);
    if (cur !== 'NZD') return fx > 0 ? r2(gross * fx) : null;
    return r2(gross * (fx > 0 ? fx : 1));
  };

  const NO_BILLS = { state: 'none', paid: 0, unpaid: 0, total: 0, count: 0, unpaidCount: 0, needsFx: 0 };

  // 'none'   nothing to say: the client is not invoiced yet, or no bills exist
  // 'paid'   every bill on the project is marked paid
  // 'unpaid' at least one bill is not paid, whatever the reason. A bill that
  //          will never be paid simply stays unpaid.
  const billsStatus = (job, financials) => {
    if (!job || !isClientInvoiced(job)) return NO_BILLS;
    const lines = billsOf(job, financials);
    if (!lines.length) return NO_BILLS;
    let paid = 0, unpaid = 0, unpaidCount = 0, needsFx = 0;
    lines.forEach(l => {
      const isPaid = l.status === 'paid';
      const v = billNzd(l);
      if (v === null) needsFx += 1;
      else if (isPaid) paid = r2(paid + v);
      else unpaid = r2(unpaid + v);
      if (!isPaid) unpaidCount += 1;
    });
    return {
      state: unpaidCount ? 'unpaid' : 'paid',
      paid: paid, unpaid: unpaid, total: r2(paid + unpaid),
      count: lines.length, unpaidCount: unpaidCount, needsFx: needsFx,
    };
  };

  // The same answer in three words ('none' | 'paid' | 'due'), used by the
  // supplier_due filter key. 'due' is the unpaid state.
  const supplierStatus = (job, financials) => {
    const b = billsStatus(job, financials);
    return b.state === 'unpaid' ? 'due' : b.state;
  };

  // ── Critical clients ─────────────────────────────────────────────────────
  const isClientCritical = (client) => {
    if (!client) return false;
    return client.critical === true || client.critical === 'true' || client.critical === 1;
  };
  const isJobCritical = (job, clients) => {
    if (!job || !clients) return false;
    const c = clients.find ? clients.find(cl => cl.id === job.client) : null;
    return isClientCritical(c);
  };

  // ── Search ───────────────────────────────────────────────────────────────
  // One matcher for projects and tasks, so the search box gives the same
  // answer whichever pass runs it. Matches title, content, description,
  // section name and the client's name or company.
  const makeSearcher = (term, clients) => {
    const q = String(term || '').trim().toLowerCase();
    if (!q) return () => true;
    const byId = {};
    for (const c of (clients || [])) byId[c.id] = c;
    return (item) => {
      if (!item) return false;
      const c = byId[item.client];
      const parts = [
        item.title, item.content, item.description, item.section_name,
        c && c.name, c && c.company,
      ];
      for (const p of parts) {
        if (p && String(p).toLowerCase().indexOf(q) !== -1) return true;
      }
      return false;
    };
  };

  // ── The filter set ───────────────────────────────────────────────────────
  const DEFAULT_FILTERS = {
    client: '',
    status: 'current',
    when: 'all',
    customStart: '',
    customEnd: '',
    records: [],
    search: '',
    createdToday: false,
    editedToday: false,
    archived: false,
    reengage: false,
    cancelled: false,
  };

  const withFilters = (filters, patch) => {
    const merged = Object.assign({}, DEFAULT_FILTERS, filters || {});
    return patch ? Object.assign(merged, patch) : merged;
  };

  // Statuses that mean something to a project and nothing to a task. When one
  // of these is picked, a task is only kept if its own project survived too.
  // Otherwise every open task would pass the task filter and drag its parent
  // back in as a host card, and picking On Hold would show Quoting projects.
  const PROJECT_SCOPED_FILTERS = [
    'critical', 'open', 'confirmed', 'on_hold', 'paid',
    'ship_due', 'supplier_due', 'reengage', 'stale', 'quoting',
  ];

  const passesProjectStatus = (job, sf, ctx) => {
    if (sf === 'critical') return isJobCritical(job, ctx.clients);
    if (sf === 'current') return !isStale(job, ctx.now);
    if (sf === 'open') return isOpenStatus(job.status);
    if (sf === 'confirmed') return isConfirmedStatus(job.status);
    if (sf === 'on_hold') return job.status === 'on_hold';
    if (sf === 'paid') return !!job.paid;
    if (sf === 'ship_due') return shippingStatus(job, ctx.financials) === 'due';
    if (sf === 'supplier_due') return supplierStatus(job, ctx.financials) === 'due';
    if (sf === 'reengage') return job.status === 'reengage';
    if (sf === 'stale') return isStale(job, ctx.now);
    if (sf === 'quoting') return job.status === 'quoting' || isOpenStatus(job.status);
    return true;
  };

  const passesTaskStatus = (task, sf, ctx) => {
    const kind = sectionKind(task);
    const labels = String(task.labels || '').toLowerCase();
    if (sf === 'current') return task.status !== 'completed';
    if (sf === 'overdue') return isTaskOverdueLive(task, ctx.now);
    if (sf === 'deadline') return !!deadlineOf(task);
    if (sf === 'today') return kind === 'today';
    if (sf === 'upcoming') return kind === 'upcoming';
    if (sf === 'waiting') return WAITING_KINDS.indexOf(kind) !== -1 || labels.indexOf('waiting') !== -1;
    if (sf === 'waiting_client') return kind === 'waiting_client';
    if (sf === 'waiting_supplier') return kind === 'waiting_supplier';
    if (sf === 'stalled') return kind === 'stalled';
    if (sf === 'backlog') return kind === 'backlog';
    if (sf === 'completed') return task.status === 'completed';
    return true;
  };

  const passesActivity = (item, filters, now) => {
    if (!filters.createdToday && !filters.editedToday) return true;
    const isNew = isToday(item.created, now);
    const isChanged = isToday(item.updated, now) && item.updated !== item.created;
    if (filters.createdToday) return isNew;
    if (filters.editedToday) return isChanged && !isNew;
    return true;
  };

  // Fill a task's dates in once, before anything downstream reads them.
  // due_date is WHEN IT LANDS, so rows sort and group by the day the operator
  // will touch them. _realDue is HOW LATE IT IS, which is what the red badge
  // means.
  const enrichTask = (task, now) => Object.assign({}, task, {
    due_date: actionableDateOf(task),
    _realDue: realDueOf(task),
    _daysLate: daysLateOf(task, now),
    _deadline: deadlineOf(task),
    _todoistDueDate: task.due_date || null,
    _sectionKind: sectionKind(task),
    _isParked: isParked(task),
  });

  // ── The pipeline ─────────────────────────────────────────────────────────
  // One pass. Everything else in this file either feeds it or counts what it
  // returned.
  function runCore(dataset, filters) {
    const f = withFilters(filters);
    const ctx = {
      clients: dataset.clients || [],
      financials: dataset.financials || [],
      quality: dataset.quality || { byJob: {} },
      now: dataset.now === undefined ? Date.now() : dataset.now,
    };
    const jobs = dataset.jobs || [];
    const assignments = dataset.assignments || [];
    const matches = makeSearcher(f.search, ctx.clients);
    const records = f.records || [];

    // A gap filter is a hunt for bad records, and half the bad records are
    // closed. A project that was completed with no invoice and no bill filed
    // is exactly the thing worth finding, so picking a gap chip stops the
    // normal "hide the finished work" rules from throwing it away.
    //
    // Only the resting state is relaxed. Pick a status yourself and it is
    // honoured, so "Confirmed + no bills" still means confirmed.
    const recordsActive = records.length > 0;

    // Archived and Cancelled are exclusive scopes, not extra rows. With either
    // on, the Workload shows only what matches the scope and the status chips
    // stop applying, so clicking Archived shows archived work and nothing else.
    const scopeActive = !!(f.archived || f.cancelled);
    const matchesScope = (j) => {
      if (f.archived && j.archived) return true;
      if (f.cancelled && isTerminalStatus(j.status) && !j.archived) return true;
      return false;
    };

    // One client, when one is picked. The Workload groups by client, and this
    // narrows it to one without relying on a name search.
    const onlyClient = f.client || '';
    const isTheClient = (row) => !onlyClient || row.client === onlyClient;

    const baseJobs = jobs.filter(j => {
      if (!isTheClient(j)) return false;
      if (scopeActive) return matchesScope(j);
      if (j.archived) return false;
      if (!f.reengage && j.status === 'reengage') return false;
      if (isTerminalStatus(j.status) && !recordsActive) return false;
      return true;
    });

    const baseTasks = assignments.filter(t => {
      if (!isTheClient(t)) return false;
      if (scopeActive) return f.archived && !!t.archived;
      if (t.archived) return false;
      if (t.status === 'completed') return false;
      return true;
    }).map(t => enrichTask(t, ctx.now));

    // A project passes the record filter when it carries every gap picked, so
    // two chips narrow rather than widen.
    const passesRecords = (j) => {
      if (!records.length) return true;
      const codes = ((ctx.quality.byJob || {})[j.id] || []).map(i => i.code);
      return records.every(code => codes.indexOf(code) !== -1);
    };

    const projects = baseJobs
      .filter(j => (scopeActive || (recordsActive && f.status === 'current'))
        ? true
        : passesProjectStatus(j, f.status, ctx))
      .filter(j => passesActivity(j, f, ctx.now))
      .filter(j => passesDateWindow(j, f.when, f.customStart, f.customEnd,
                                    (it) => countsTowardWorkload(it, ctx.now), ctx.now))
      .filter(passesRecords)
      .filter(matches);

    const survivingJobIds = new Set(projects.map(j => j.id));
    const reengageJobIds = new Set(jobs.filter(j => j.status === 'reengage').map(j => j.id));
    const projectScoped = scopeActive
      || records.length > 0
      || PROJECT_SCOPED_FILTERS.indexOf(f.status) !== -1;

    const tasks = baseTasks
      .filter(t => scopeActive ? true : passesTaskStatus(t, f.status, ctx))
      .filter(t => passesActivity(t, f, ctx.now))
      .filter(t => passesDateWindow(t, f.when, f.customStart, f.customEnd, null, ctx.now))
      .filter(matches)
      .filter(t => (f.reengage || !t.job || !reengageJobIds.has(t.job)))
      .filter(t => projectScoped ? (t.job && survivingJobIds.has(t.job)) : true);

    // A task whose project did not survive still needs its project drawn, or
    // the task disappears with no way to tell it was ever there. Those hosts
    // are kept apart from the projects that passed on their own merits: they
    // are here for their tasks, not because they matched.
    const hostIds = new Set();
    for (const t of tasks) {
      if (t.job && !survivingJobIds.has(t.job)) hostIds.add(t.job);
    }
    const hostProjects = [];
    for (const id of hostIds) {
      const real = jobs.find(j => j.id === id);
      if (real) hostProjects.push(Object.assign({}, real, { _orphanParent: true }));
    }

    return { projects, tasks, hostProjects, scopeActive, ctx, filters: f };
  }

  // What a project card is: a project the filters actually matched.
  //
  // A host project is not one. It is on the page because a task underneath it
  // is still open, it did not match anything itself, and the view folds those
  // away into one line per client rather than drawing them as results.
  //
  // The chips count these, the tests count these, and the view draws these.
  // This is the one definition all three share.
  const countCards = (result) => (result.projects || []).length;

  // ── The summary ──────────────────────────────────────────────────────────
  // The header line and the summary bar both read this, so the two numbers on
  // screen always agree.
  function summarise(result) {
    const { projects, hostProjects, tasks, scopeActive, ctx, filters } = result;
    // When the operator is deliberately looking at parked work (On Hold or
    // Stale/Old, or a scope), count it. Otherwise archived, on hold, terminal
    // and stale projects must not inflate the open and overdue figures.
    const inspecting = filters.status === 'on_hold' || filters.status === 'stale';
    const cards = projects.concat(hostProjects);
    const counting = (j) => scopeActive || inspecting || countsTowardWorkload(j, ctx.now);
    const countedJobs = cards.filter(counting);
    const liveJobIds = new Set(countedJobs.map(j => j.id));
    const countedTasks = tasks.filter(t => {
      if (scopeActive || inspecting) return true;
      if (t.job) return liveJobIds.has(t.job);
      return true;
    });
    return {
      openProjects: countedJobs.length,
      openTasks: countedTasks.length,
      overdueProjects: countedJobs.filter(j => tlIsOverdue(j, ctx.now)).length,
      overdueTasks: countedTasks.filter(t => isTaskOverdueLive(t, ctx.now)).length,
      value: countedJobs.reduce((s, j) => s + (Number(j.value) || 0), 0),
      cards: projects.length,
      hosts: hostProjects.length,
    };
  }

  // ── Faceted counts ───────────────────────────────────────────────────────
  // Every count below is measured, not derived: the pipeline runs again with
  // that one value swapped and the cards are counted. That is a few dozen
  // runs over a small dataset, cheap enough to do on every render, and it is
  // what guarantees a chip and its rows agree.
  const STATUS_KEYS = [
    'current', 'critical', 'open', 'confirmed', 'on_hold', 'paid',
    'ship_due', 'supplier_due', 'stale', 'all',
  ];
  const SCOPE_KEYS = ['archived', 'reengage', 'cancelled', 'createdToday', 'editedToday'];

  function countsFor(dataset, filters, recordCodes) {
    const f = withFilters(filters);
    const at = (patch) => countCards(runCore(dataset, withFilters(f, patch)));

    const status = {};
    for (const key of STATUS_KEYS) status[key] = at({ status: key });

    const when = {};
    for (const key of DATE_WINDOWS) {
      when[key] = at({ when: key, customStart: '', customEnd: '' });
    }

    // A record chip counts what picking it ADDS to what is already picked, so
    // two chips read as the narrowing they actually are, not as a count over
    // the whole open book.
    const records = {};
    for (const code of (recordCodes || [])) {
      const picked = f.records || [];
      const next = picked.indexOf(code) !== -1
        ? picked.filter(c => c !== code)
        : picked.concat([code]);
      records[code] = at({ records: next });
    }

    const scope = {};
    for (const key of SCOPE_KEYS) scope[key] = at({ [key]: !f[key] });

    return { status, when, records, scope };
  }

  // ── What is switched on, in words ────────────────────────────────────────
  // The bar's summary strip and the empty state read this, so "Nothing
  // matches Current + Overdue + No address" and the strip above it can never
  // name a different set of filters from each other.
  const STATUS_LABELS = {
    current: 'Current', critical: 'Critical', open: 'Open Pipeline',
    confirmed: 'Confirmed', on_hold: 'On Hold', paid: 'Paid',
    ship_due: 'Shipping due', supplier_due: 'Bills unpaid', stale: 'Stale/Old',
    active: 'Needs status', all: 'All',
    // the task columns, for the views that share this bar
    overdue: 'Overdue', deadline: 'Deadline', today: 'Today', upcoming: 'Upcoming',
    waiting: 'Waiting', waiting_client: 'Waiting on client',
    waiting_supplier: 'Waiting on supplier', stalled: 'Stalled',
    backlog: 'Backlog', completed: 'Completed', reengage: 'Re-engage',
    quoting: 'Quoting',
  };
  const WHEN_LABELS = {
    all: 'Any date', overdue: 'Overdue', today: 'Today',
    week: 'This week', recent: 'Recent', custom: 'Date range',
  };
  // Created and Edited, not New and Changed, so "Today" in the When row
  // (due today) is the only "Today" on the bar.
  const SCOPE_LABELS = {
    createdToday: 'Created today', editedToday: 'Edited today',
    archived: 'Archived', cancelled: 'Closed', reengage: 'Re-engage',
  };

  function describeFilters(filters, recordLabels) {
    const f = withFilters(filters);
    const out = [];
    out.push({
      row: 'status', key: f.status,
      label: STATUS_LABELS[f.status] || f.status,
      // Current is the resting state, so it is named but not removable.
      removable: f.status !== 'current',
    });
    if (f.when && f.when !== 'all') {
      out.push({ row: 'when', key: f.when, label: WHEN_LABELS[f.when] || f.when, removable: true });
    }
    for (const code of (f.records || [])) {
      out.push({ row: 'records', key: code, label: (recordLabels || {})[code] || code, removable: true });
    }
    for (const key of SCOPE_KEYS) {
      if (f[key]) out.push({ row: 'scope', key: key, label: SCOPE_LABELS[key], removable: true });
    }
    if (f.client) {
      out.push({
        row: 'client', key: f.client,
        label: (recordLabels && recordLabels[f.client]) || 'One client',
        removable: true,
      });
    }
    if (f.search) {
      out.push({ row: 'search', key: 'search', label: '\u201c' + f.search + '\u201d', removable: true });
    }
    return out;
  }

  const isDefaultFilters = (filters) => {
    const f = withFilters(filters);
    if (f.status !== 'current' || f.when !== 'all') return false;
    if ((f.records || []).length || f.search || f.customStart || f.customEnd || f.client) return false;
    return !SCOPE_KEYS.some(key => f[key]);
  };

  // ── What each filter actually does ───────────────────────────────────────
  //
  // The words the info panel shows, kept in this file next to the
  // predicates they describe, so a code change and its description are made
  // in the same place.
  //
  // Write what it does, not what it is for. "Due before today" is checkable
  // against the code below. "Work that needs attention" is not.
  const FILTER_HELP = {
    status: {
      title: 'Status',
      note: 'One at a time. It picks which projects are on the page. Tasks come along under whichever projects survive.',
      items: [
        ['Current', 'Every project except the stale ones. This is the resting state.'],
        ['Critical', 'Projects whose client is flagged critical in PocketBase.'],
        ['Open Pipeline', 'Quoting and Quoted.'],
        ['Confirmed', 'Won, Invoicing, Invoiced and Commissioning.'],
        ['On Hold', 'Status is On Hold.'],
        ['Paid', 'The paid flag is set on the project.'],
        ['Shipping due', 'The project carries freight, duty or import GST, and shipping is not marked paid.'],
        ['Bills unpaid', 'The client has been invoiced, the project carries supplier bills, and at least one of them is not marked paid.'],
        ['Stale/Old', 'Closed or cancelled, or no start date and no due date, or a due date more than 45 days behind today. An on hold project with an old start date counts too.'],
        ['Needs status', 'Still on the legacy Active status. It also catches every confirmed status today, which is wrong and is on the list to fix.'],
        ['All', 'Every project the scope is showing.'],
      ],
    },
    when: {
      title: 'When',
      note: 'One at a time, or a range. It reads the due date, and for a task that means the date it lands on, not the date it was really due.',
      items: [
        ['Any date', 'No date filter at all.'],
        ['Overdue', 'Due before today.'],
        ['Today', 'Due today.'],
        ['This week', 'Due within the next seven days, today included.'],
        ['Recent', 'Created or changed in the last seven days. This reads the record’s own stamps. Emails and interactions logged against a project are not counted.'],
        ['From / To', 'Due between the two dates. Either bound on its own works.'],
      ],
    },
    records: {
      title: 'Gaps',
      note: 'These combine. A project shows when it carries every gap you pick, so two chips narrow rather than widen. Picking one also brings in completed and closed work, because a job that finished with its paperwork missing is exactly what you are looking for. The counts are what you get by picking it, not the whole book.',
      items: [
        ['On the row', 'The same gaps appear as chips on each project, and clicking one there fills it in without leaving the page.'],
      ],
    },
    scope: {
      title: 'Scope',
      note: 'Work that is hidden by default.',
      items: [
        ['Created today', 'The record was created today.'],
        ['Edited today', 'The record changed today. It currently counts writes by the sync and the daemon as well as your own edits, which is on the list to fix.'],
        ['Archived', 'Shows only archived work. The status filters pause while it is on.'],
        ['Closed', 'Shows only completed, cancelled and lost projects. The status filters pause while it is on.'],
        ['Re-engage', 'Adds the re-engage projects, which are hidden the rest of the time.'],
      ],
    },
    tasks: {
      title: 'How tasks and projects fit together',
      note: '',
      items: [
        ['A task', 'Shows when it passes the filters itself, and it is never dropped just because its project did not.'],
        ['A project', 'Counted as a result only when it matched the filters on its own.'],
        ['A folded line', 'A project that did not match but still has open tasks. It is drawn only so the tasks have somewhere to sit, and it is not counted.'],
      ],
    },
  };

  // ── The one call the view makes ──────────────────────────────────────────
  function runWorkload(dataset, filters, options) {
    const opts = options || {};
    const result = runCore(dataset, filters);
    return {
      projects: result.projects,
      tasks: result.tasks,
      hostProjects: result.hostProjects,
      scopeActive: result.scopeActive,
      filters: result.filters,
      summary: summarise(result),
      counts: countsFor(dataset, filters, opts.recordCodes || []),
    };
  }

  return {
    // status
    STATUS_GROUPS: STATUS_GROUPS,
    isOpenStatus: isOpenStatus,
    isConfirmedStatus: isConfirmedStatus,
    isReengageStatus: isReengageStatus,
    isTerminalStatus: isTerminalStatus,
    isPipelineStatus: isPipelineStatus,
    // dates
    DAY_MS: DAY_MS,
    DATE_WINDOWS: DATE_WINDOWS,
    startOfDay: startOfDay,
    endOfDay: endOfDay,
    parseLocalDate: parseLocalDate,
    daysBetween: daysBetween,
    isToday: isToday,
    passesDateWindow: passesDateWindow,
    // tasks
    REAL_DUE_PIN: REAL_DUE_PIN,
    realDueOf: realDueOf,
    actionableDateOf: actionableDateOf,
    deadlineOf: deadlineOf,
    daysLateOf: daysLateOf,
    sectionKind: sectionKind,
    WAITING_KINDS: WAITING_KINDS,
    isParked: isParked,
    isTaskOverdueLive: isTaskOverdueLive,
    lateTierOf: lateTierOf,
    enrichTask: enrichTask,
    // projects
    STALE_DAYS: STALE_DAYS,
    isStale: isStale,
    countsTowardWorkload: countsTowardWorkload,
    tlIsOverdue: tlIsOverdue,
    tlDaysLateOf: tlDaysLateOf,
    tlLateTierOf: tlLateTierOf,
    shippingStatus: shippingStatus,
    supplierStatus: supplierStatus,
    billsStatus: billsStatus,
    isClientInvoiced: isClientInvoiced,
    isClientCritical: isClientCritical,
    isJobCritical: isJobCritical,
    // the pipeline
    DEFAULT_FILTERS: DEFAULT_FILTERS,
    STATUS_LABELS: STATUS_LABELS,
    WHEN_LABELS: WHEN_LABELS,
    SCOPE_LABELS: SCOPE_LABELS,
    FILTER_HELP: FILTER_HELP,
    describeFilters: describeFilters,
    isDefaultFilters: isDefaultFilters,
    STATUS_KEYS: STATUS_KEYS,
    SCOPE_KEYS: SCOPE_KEYS,
    PROJECT_SCOPED_FILTERS: PROJECT_SCOPED_FILTERS,
    makeSearcher: makeSearcher,
    runCore: runCore,
    countCards: countCards,
    summarise: summarise,
    countsFor: countsFor,
    runWorkload: runWorkload,
  };
});
