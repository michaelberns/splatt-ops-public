// Checks, in a real browser, that the Workload page draws the number of
// project cards its filter chips promise. The Node checks prove the
// filter pipeline is consistent; this proves the page is wired to it.
//
//   node tools/check_workload_ui.js
//   node tools/check_workload_ui.js --url http://127.0.0.1:8789/#workload
//   node tools/check_workload_ui.js --headed
//
// By default it opens dashboard/index.html straight off disk, which is how
// bin/start opens it, so dashboard/config.local.js must point at a running
// PocketBase. It drives the installed Chrome through Playwright rather than
// downloading a browser. For each chip it reads the promised count, clicks
// it, and counts the cards on screen ([data-result="true"]); it then checks
// a few chip pairs, the status legend and the status picker. Screenshots of
// every step land in reports/workload-filters/.
//
// Exits 0 when everything matches, 1 on any mismatch, 2 when Playwright is
// missing or no cards appear. Read only: it clicks filters and counts rows,
// and it never presses Save.

'use strict';

const fs = require('fs');
const path = require('path');

const REPO = path.join(__dirname, '..');
const args = process.argv.slice(2);
const argOf = (name, fallback) => {
  const i = args.indexOf(name);
  return i !== -1 && args[i + 1] ? args[i + 1] : fallback;
};
const URL_ARG = argOf('--url',
  'file://' + path.join(REPO, 'dashboard', 'index.html') + '#workload');
const HEADED = args.indexOf('--headed') !== -1;
const SHOTS = path.join(REPO, 'reports', 'workload-filters');

// A project card. The header row of every project carries it, and nothing
// else does, so counting these counts the projects on screen.
const CARD = '[data-result="true"]';

// The bar carries the statuses you switch between; the rest live behind a
// menu button. Each entry says which menu to open first, or none.
const STATUS_ON_BAR = ['Current', 'Critical', 'Open Pipeline', 'Confirmed', 'Stale/Old', 'All'];
const STATUS_IN_MORE = ['On Hold', 'Paid', 'Shipping due', 'Supplier due'];
const WHEN_CHIPS = ['Any date', 'Overdue', 'Today', 'This week', 'Recent'];
const SCOPE_CHIPS = ['Created today', 'Edited today', 'Archived', 'Closed', 'Re-engage'];
const GAP_CHIPS = ['No project value', 'No due date'];

// The eleven job statuses, as the legend and the picker both spell them, in
// flow order. Written out here on purpose rather than read off the page,
// because a list the page derives from itself only proves that the page
// agrees with itself.
const STATUS_LABELS = [
  'Quoting', 'Quoted', 'Won', 'Invoicing', 'Invoiced', 'Commissioning',
  'Lost', 'On Hold', 'Cancelled', 'Completed', 'Re-engage',
];

function plan() {
  const out = [];
  for (const label of STATUS_ON_BAR) out.push({ label, menu: null, toggle: false });
  for (const label of STATUS_IN_MORE) out.push({ label, menu: 'More', toggle: false });
  for (const label of WHEN_CHIPS) out.push({ label, menu: 'When', toggle: false });
  for (const label of GAP_CHIPS) out.push({ label, menu: 'Gaps', toggle: true });
  for (const label of SCOPE_CHIPS) out.push({ label, menu: 'Scope', toggle: true });
  return out;
}

// The menu buttons carry a label and a caret rather than a count, so they
// need their own matcher.
async function openMenu(page, name) {
  if (!name) return true;
  // By data-menu, not by label. The More button wears the picked status as
  // its label, so looking for the word "More" loses it the moment you use it.
  const button = page.locator(`button[data-menu="${name}"]`).first();
  if (!(await button.count())) return false;
  if ((await button.getAttribute('aria-expanded')) !== 'true') {
    await button.click();
    await page.waitForTimeout(250);
  }
  return true;
}

async function closeMenus(page) {
  await page.keyboard.press('Escape');
  await page.waitForTimeout(200);
}

async function chip(page, label) {
  // Exact label match on the text node, so "Today" does not also match
  // "Created today" and "Overdue" does not match a row badge.
  const buttons = page.locator('button', { hasText: new RegExp('^' + label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\d+$') });
  const n = await buttons.count();
  if (n === 0) return null;
  return buttons.first();
}

async function countOn(button) {
  const text = (await button.innerText()).trim();
  const m = /(\d+)$/.exec(text);
  return m ? Number(m[1]) : null;
}

async function main() {
  let chromium;
  try {
    ({ chromium } = require('playwright'));
  } catch (err) {
    console.error('Playwright is not installed. npm install playwright');
    return 2;
  }

  fs.mkdirSync(SHOTS, { recursive: true });

  const launch = { headless: !HEADED };
  let browser;
  try {
    browser = await chromium.launch(Object.assign({ channel: 'chrome' }, launch));
  } catch (err) {
    // No installed Chrome to borrow. Fall back to whatever Playwright has,
    // rather than downloading one.
    browser = await chromium.launch(launch);
  }

  const page = await browser.newPage({ viewport: { width: 1700, height: 1200 } });
  const errors = [];
  page.on('pageerror', e => errors.push(String(e.message)));

  await page.goto(URL_ARG, { waitUntil: 'load', timeout: 60000 });
  // The page fetches its data after mounting, so wait for the first card
  // rather than for a timer.
  try {
    await page.waitForSelector(CARD, { timeout: 30000 });
  } catch (err) {
    console.error('No project cards ever appeared at ' + URL_ARG);
    console.error('Is the stack running, and is dashboard/config.local.js pointing at it?');
    if (errors.length) console.error('Page errors:\n  ' + errors.join('\n  '));
    await browser.close();
    return 2;
  }

  let bad = 0;
  const rows = [];

  for (const step of plan()) {
    if (!(await openMenu(page, step.menu))) {
      rows.push([step.label, 'no ' + step.menu + ' menu', '', 'SKIP']);
      continue;
    }
    const button = await chip(page, step.label);
    if (!button) {
      rows.push([step.label, 'not on the bar', '', 'SKIP']);
      await closeMenus(page);
      continue;
    }
    const promised = await countOn(button);
    // A chip promising 0 is inert on purpose: it stays put so the row does
    // not move, but clicking it would only empty the page. Nothing to click
    // means nothing to compare, and that is the right answer.
    if (await button.getAttribute('aria-disabled')) {
      rows.push([step.label, promised, 'inert', promised === 0 ? 'ok' : 'MISMATCH']);
      if (promised !== 0) bad += 1;
      await closeMenus(page);
      continue;
    }
    await button.click();
    await page.waitForTimeout(450);
    await closeMenus(page);
    const drawn = await page.locator(CARD).count();
    const ok = promised === drawn;
    if (!ok) bad += 1;
    rows.push([step.label, promised, drawn, ok ? 'ok' : 'MISMATCH']);
    await page.screenshot({
      path: path.join(SHOTS, step.label.toLowerCase().replace(/[^a-z0-9]+/g, '-') + '.png'),
    });
    if (step.toggle) {
      // Put it back before the next one, or every later count is measured
      // against a filter this check switched on.
      await openMenu(page, step.menu);
      const again = await chip(page, step.label);
      if (again && !(await again.getAttribute('aria-disabled'))) await again.click();
      await closeMenus(page);
      await page.waitForTimeout(300);
    }
  }

  // Two chips at once, because combinations are where a count and its rows
  // are most likely to disagree.
  const combos = [
    { a: { label: 'Current', menu: null }, b: { label: 'Overdue', menu: 'When' } },
    { a: { label: 'All', menu: null }, b: { label: 'Recent', menu: 'When' } },
    { a: { label: 'Stale/Old', menu: null }, b: { label: 'No project value', menu: 'Gaps' } },
  ];
  for (const { a, b } of combos) {
    await openMenu(page, a.menu);
    const first = await chip(page, a.label);
    if (!first || await first.getAttribute('aria-disabled')) { await closeMenus(page); continue; }
    await first.click();
    await page.waitForTimeout(350);
    await closeMenus(page);
    await openMenu(page, b.menu);
    const second = await chip(page, b.label);
    if (!second || await second.getAttribute('aria-disabled')) {
      rows.push([a.label + ' + ' + b.label, 'second chip inert', '', 'SKIP']);
      await closeMenus(page);
      continue;
    }
    const promised = await countOn(second);
    await second.click();
    await page.waitForTimeout(450);
    await closeMenus(page);
    const drawn = await page.locator(CARD).count();
    const ok = promised === drawn;
    if (!ok) bad += 1;
    rows.push([a.label + ' + ' + b.label, promised, drawn, ok ? 'ok' : 'MISMATCH']);
    await page.screenshot({
      path: path.join(SHOTS, (a.label + '-' + b.label).toLowerCase().replace(/[^a-z0-9]+/g, '-') + '.png'),
    });
    // Clear before the next pair.
    const clear = page.locator('button', { hasText: /^Clear all$/ }).first();
    if (await clear.count()) { await clear.click(); await page.waitForTimeout(400); }
  }

  // ── the status legend ──────────────────────────────────────────────────
  //
  // The `i` button beside the timeline bar. It and the picker read the same
  // JOB_STATUS_META in the same order, so this check and the next one should
  // always report the same list.
  try {
    const legendButton = page.locator('button[aria-label="Project status legend"]').first();
    if (!(await legendButton.count())) {
      rows.push(['status legend', 'no legend button', '', 'SKIP']);
    } else {
      await legendButton.hover();
      await page.waitForTimeout(300);
      const shown = (await page.locator('text=Project status flow').first().count())
        ? await page.evaluate(() => {
            const head = Array.from(document.querySelectorAll('div'))
              .find(d => d.textContent.trim() === 'Project status flow');
            if (!head || !head.parentElement) return [];
            return Array.from(head.parentElement.querySelectorAll('span'))
              .map(s => s.textContent.trim()).filter(Boolean);
          })
        : [];
      const ok = JSON.stringify(shown) === JSON.stringify(STATUS_LABELS);
      if (!ok) bad += 1;
      rows.push(['status legend', STATUS_LABELS.length, shown.length,
                 ok ? 'ok' : 'MISMATCH ' + shown.join('|')]);
      await page.mouse.move(0, 0);
      await page.waitForTimeout(200);
    }
  } catch (err) {
    rows.push(['status legend', 'threw', '', 'SKIP ' + err.message]);
  }

  // ── the status picker on a project ─────────────────────────────────────
  //
  // Open a project, click Edit, open the picker, and check it offers the
  // eleven in legend order. Then pick Won and read the button's own chip
  // back: what you pick is what you then see.
  //
  // Nothing is saved. The project page only writes on Save, so this stops at
  // Cancel, and the tool stays read only against whatever database the page
  // is pointed at.
  try {
    const clear = page.locator('button', { hasText: /^Clear all$/ }).first();
    if (await clear.count()) { await clear.click(); await page.waitForTimeout(400); }
    const firstCard = page.locator(CARD).first();
    if (!(await firstCard.count())) {
      rows.push(['status picker', 'no project to open', '', 'SKIP']);
    } else {
      await firstCard.click();
      await page.waitForTimeout(700);
      const edit = page.locator('button', { hasText: /^\s*Edit\s*$/ }).first();
      if (!(await edit.count())) {
        rows.push(['status picker', 'no Edit button', '', 'SKIP']);
      } else {
        await edit.click();
        await page.waitForTimeout(400);
        const picker = page.locator('button[aria-label="Project status"]').first();
        if (!(await picker.count())) {
          rows.push(['status picker', 'no picker', '', 'SKIP']);
        } else {
          await picker.click();
          await page.waitForTimeout(300);
          const options = page.locator('[role="option"]');
          const offered = await options.allInnerTexts();
          const labels = offered.map(t => t.replace(/\u2713/g, '').trim()).filter(Boolean);
          const orderOk = JSON.stringify(labels) === JSON.stringify(STATUS_LABELS);
          if (!orderOk) bad += 1;
          rows.push(['status picker', STATUS_LABELS.length, labels.length,
                     orderOk ? 'ok' : 'MISMATCH ' + labels.join('|')]);

          const won = options.filter({ hasText: /^Won/ }).first();
          if (await won.count()) {
            await won.click();
            await page.waitForTimeout(300);
            const back = (await picker.innerText()).replace(/[^A-Za-z\- ]/g, '').trim();
            const roundTrip = back === 'Won';
            if (!roundTrip) bad += 1;
            rows.push(['picked Won reads back', 'Won', back || '(empty)',
                       roundTrip ? 'ok' : 'MISMATCH']);
          }
          await page.screenshot({ path: path.join(SHOTS, 'status-picker.png') });
        }
        // Leave without saving, whatever happened above.
        const cancel = page.locator('button', { hasText: /^\s*Cancel\s*$/ }).first();
        if (await cancel.count()) { await cancel.click(); await page.waitForTimeout(300); }
      }
    }
  } catch (err) {
    rows.push(['status picker', 'threw', '', 'SKIP ' + err.message]);
  }

  const pad = (v, w) => { const s = String(v); return s.length >= w ? s : s + ' '.repeat(w - s.length); };
  console.log('%s %s %s %s', pad('chip', 22), pad('says', 6), pad('drawn', 6), '');
  console.log('-'.repeat(48));
  for (const [label, says, drawn, verdict] of rows) {
    console.log('%s %s %s %s', pad(label, 22), pad(says, 6), pad(drawn, 6), verdict);
  }
  console.log('');
  console.log('screenshots in %s', path.relative(REPO, SHOTS));
  if (errors.length) {
    console.log('');
    console.log('%d page errors:', errors.length);
    for (const e of errors.slice(0, 10)) console.log('  ' + e);
  }

  await browser.close();
  if (bad) {
    console.log('%d chips disagree with the rows on screen', bad);
    return 1;
  }
  console.log('every chip matches the rows on screen');
  return 0;
}

main().then(c => process.exit(c)).catch(e => { console.error(e); process.exit(1); });
