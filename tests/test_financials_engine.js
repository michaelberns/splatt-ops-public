// Tests for server/financials_engine.js (GST, FX and project margin maths).
// Run with: node --test tests/test_financials_engine.js
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { round2, lineNet, lineNetNzd, isReady, computeSummary } = require('../server/financials_engine');

test('incl_15 strips NZ GST', () => {
  assert.strictEqual(lineNet({ amount_gross: 690, gst_treatment: 'incl_15' }), 600.00);
  assert.strictEqual(lineNet({ amount_gross: 3465.01, gst_treatment: 'incl_15' }), 3013.05);
});

test('add_15 / no_gst / zero keep gross as net', () => {
  assert.strictEqual(lineNet({ amount_gross: 100, gst_treatment: 'add_15' }), 100);
  assert.strictEqual(lineNet({ amount_gross: 873.20, gst_treatment: 'no_gst' }), 873.20);
  assert.strictEqual(lineNet({ amount_gross: 50, gst_treatment: 'zero' }), 50);
});

test('import GST is never a cost (net = 0)', () => {
  assert.strictEqual(lineNet({ amount_gross: 250, gst_treatment: 'is_import_gst' }), 0);
});

test('FX converts to NZD; blank/NZD fx treated as 1', () => {
  // USD bill 873.20 at 1.72057 NZD per USD is about 1502.40 NZD
  assert.strictEqual(
    lineNetNzd({ amount_gross: 873.20, gst_treatment: 'no_gst', currency: 'USD', fx_rate: 1.72057 }),
    1502.40
  );
  assert.strictEqual(
    lineNetNzd({ amount_gross: 690, gst_treatment: 'incl_15', currency: 'NZD' }), 600.00
  );
  assert.strictEqual(
    lineNetNzd({ amount_gross: 690, gst_treatment: 'incl_15', currency: 'NZD', fx_rate: 0 }), 600.00
  );
});

test('summary: revenue - cost, ex-GST, NZD', () => {
  const lines = [
    { direction: 'in',  billed_via: 'splatt', amount_gross: 690,  gst_treatment: 'incl_15' }, // 600
    { direction: 'out', billed_via: 'splatt', amount_gross: 230,  gst_treatment: 'incl_15' }, // 200
  ];
  const s = computeSummary(lines);
  assert.strictEqual(s.revenue_nzd, 600);
  assert.strictEqual(s.cost_nzd, 200);
  assert.strictEqual(s.margin_nzd, 400);
  assert.strictEqual(s.margin_pct, 66.67);
});

test('partner-entity lines excluded from main margin, summed separately', () => {
  const lines = [
    { direction: 'in',  billed_via: 'splatt', amount_gross: 1150, gst_treatment: 'incl_15', currency: 'NZD' }, // rev 1000
    { direction: 'out', billed_via: 'splatt', amount_gross: 575,  gst_treatment: 'incl_15', currency: 'NZD' }, // cost 500
    { direction: 'out', billed_via: 'partner', amount_gross: 300, gst_treatment: 'no_gst', currency: 'NZD' },  // partner -300
    { direction: 'in',  billed_via: 'partner', amount_gross: 100, gst_treatment: 'no_gst', currency: 'NZD' },  // partner +100
  ];
  const s = computeSummary(lines);
  assert.strictEqual(s.revenue_nzd, 1000);
  assert.strictEqual(s.cost_nzd, 500);
  assert.strictEqual(s.margin_nzd, 500);
  assert.strictEqual(s.partner_nzd, -200); // 100 in - 300 out, never in main margin
});

test('foreign line with NO fx is unconvertible (null) and excluded from totals', () => {
  assert.strictEqual(lineNetNzd({ amount_gross: 2635, gst_treatment: 'no_gst', currency: 'EUR' }), null);
  assert.strictEqual(lineNetNzd({ amount_gross: 2635, gst_treatment: 'no_gst', currency: 'EUR', fx_rate: 0 }), null);
  assert.strictEqual(isReady({ amount_gross: 2635, gst_treatment: 'no_gst', currency: 'EUR', parse_status: 'confirmed' }), false);
  const lines = [
    { direction: 'in',  billed_via: 'splatt', amount_gross: 1150, gst_treatment: 'incl_15', currency: 'NZD', parse_status: 'confirmed' }, // rev 1000
    { direction: 'out', billed_via: 'splatt', amount_gross: 2635, gst_treatment: 'no_gst',  currency: 'EUR', parse_status: 'confirmed' }, // unconvertible -> excluded
  ];
  const s = computeSummary(lines);
  assert.strictEqual(s.revenue_nzd, 1000);
  assert.strictEqual(s.cost_nzd, 0);          // EUR line is not counted as if it were NZD
  assert.strictEqual(s.margin_nzd, 1000);
  assert.strictEqual(s.unconfirmed_count, 1); // counted as needing input
});

test('auto-parsed line excluded from totals even if NZD', () => {
  const lines = [
    { direction: 'in',  billed_via: 'splatt', amount_gross: 1150, gst_treatment: 'incl_15', currency: 'NZD', parse_status: 'confirmed' },
    { direction: 'out', billed_via: 'splatt', amount_gross: 1248.14, gst_treatment: 'incl_15', currency: 'NZD', parse_status: 'parsed' },
  ];
  const s = computeSummary(lines);
  assert.strictEqual(s.cost_nzd, 0);
  assert.strictEqual(s.unconfirmed_count, 1);
});

test('import GST line does not reduce margin', () => {
  const lines = [
    { direction: 'in',  billed_via: 'splatt', amount_gross: 1150, gst_treatment: 'incl_15' }, // 1000
    { direction: 'out', billed_via: 'splatt', amount_gross: 250,  gst_treatment: 'is_import_gst' }, // 0
    { direction: 'out', billed_via: 'splatt', amount_gross: 115,  gst_treatment: 'incl_15' }, // 100
  ];
  const s = computeSummary(lines);
  assert.strictEqual(s.cost_nzd, 100);   // import GST excluded
  assert.strictEqual(s.margin_nzd, 900);
});

test('zero-revenue guard: margin_pct = 0, not NaN/Infinity', () => {
  const s = computeSummary([{ direction: 'out', billed_via: 'splatt', amount_gross: 115, gst_treatment: 'incl_15' }]);
  assert.strictEqual(s.revenue_nzd, 0);
  assert.strictEqual(s.cost_nzd, 100);
  assert.strictEqual(s.margin_pct, 0);
  assert.strictEqual(Number.isFinite(s.margin_pct), true);
});

test('unconfirmed (auto-parsed) lines are counted', () => {
  const lines = [
    { direction: 'in', billed_via: 'splatt', amount_gross: 1150, gst_treatment: 'incl_15', parse_status: 'parsed' },
    { direction: 'out', billed_via: 'splatt', amount_gross: 115, gst_treatment: 'incl_15', parse_status: 'confirmed' },
  ];
  const s = computeSummary(lines);
  assert.strictEqual(s.unconfirmed_count, 1);
  assert.strictEqual(s.line_count, 2);
});

test('empty input is safe', () => {
  const s = computeSummary([]);
  assert.deepStrictEqual(s, {
    revenue_nzd: 0, cost_nzd: 0, margin_nzd: 0, margin_pct: 0,
    partner_nzd: 0, line_count: 0, unconfirmed_count: 0,
  });
});
