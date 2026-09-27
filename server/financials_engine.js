// financials_engine.js
//
// Project margin maths: GST, currency conversion and the "true win" of a
// project (revenue minus cost, both ex-GST, in NZD).
//
// Input is a list of project_financials lines. Each line is one invoice to
// the client (direction 'in') or one bill, freight charge or duty payment
// (direction 'out'), with its gross amount, currency, FX rate and GST
// treatment. computeSummary() rolls them up into the fields stored in the
// project_financials_summary collection.
//
// Who uses this file
//   - tests/test_financials_engine.js and tests/test_quality_engine.js
//     require it directly.
//   - dashboard/index.html carries an inline copy of the same maths
//     (FinancialsPanel), because the dashboard is a single page with no
//     module loader. The two must be kept in step.
//   - dashboard/quality_engine.js mirrors isReady(), and a test checks that
//     the two agree.
// The files server (server/project-files-server.js) does not use it.
//
// Pure functions with no I/O, so every rule can be tested directly.
//
// Conventions
//   fx_rate  = NZD per 1 unit of the line's currency (1 for NZD).
//              net_nzd = net * fx_rate.
//   GST      = passed through to the tax authority, so margin is worked out
//              ex-GST on both sides.
//   billed_via 'partner' = the line was invoiced or paid through a partner
//              entity rather than Splatt itself. Those lines are kept out
//              of the main margin and summed separately as partner_nzd.

'use strict';

// Round to cents. EPSILON nudges values like 1.005 that are stored as
// 1.00499999... so they round the way a person would expect.
function round2(n) {
  return Math.round((Number(n) + Number.EPSILON) * 100) / 100;
}

// Ex-GST net amount in the line's ORIGINAL currency.
//   lineNet({ amount_gross: 690, gst_treatment: 'incl_15' })  // 600
function lineNet(line) {
  const gross = Number(line.amount_gross) || 0;
  switch (line.gst_treatment) {
    case 'incl_15':       return round2(gross / 1.15); // 15% GST is inside the gross
    case 'add_15':        return round2(gross);        // amount entered ex-GST already
    case 'no_gst':        return round2(gross);        // overseas supplier, no NZ GST
    case 'zero':          return round2(gross);        // zero-rated supply
    case 'is_import_gst': return 0;                     // GST paid at the border is claimed back, never a cost
    default:              return round2(gross);
  }
}

// Ex-GST net amount converted to NZD.
//
// Returns null for a foreign-currency line with no usable FX rate. Such a
// line cannot be converted, and treating EUR 1,000 as NZD 1,000 would give
// a wrong margin, so the caller must leave it out and flag it instead.
// An NZD line with a blank or zero fx_rate uses a rate of 1.
function lineNetNzd(line) {
  const cur = (line.currency || 'NZD').toUpperCase();
  const fx = Number(line.fx_rate);
  if (cur !== 'NZD') {
    if (!(fx > 0)) return null;          // foreign with no rate: cannot convert
    return round2(lineNet(line) * fx);
  }
  const rate = (fx && fx > 0) ? fx : 1;  // NZD: blank or 0 means 1
  return round2(lineNet(line) * rate);
}

// A line counts toward the totals only when it is "ready":
//   - its amounts have been confirmed by a person (parse_status is not
//     'parsed', which marks figures read automatically from a PDF), and
//   - it can be converted to NZD.
function isReady(line) {
  return line.parse_status !== 'parsed' && lineNetNzd(line) !== null;
}

// Roll a set of lines up into a project summary.
//
// Only ready lines are added to revenue, cost or the partner sub-total.
// Every other line is counted in unconfirmed_count, so a total that is
// missing lines says so. The result can be understated, but it never
// mixes currencies or includes unchecked figures.
//
//   computeSummary([
//     { direction: 'in',  billed_via: 'splatt', amount_gross: 690, gst_treatment: 'incl_15' },
//     { direction: 'out', billed_via: 'splatt', amount_gross: 230, gst_treatment: 'incl_15' },
//   ])
//   // { revenue_nzd: 600, cost_nzd: 200, margin_nzd: 400, margin_pct: 66.67,
//   //   partner_nzd: 0, line_count: 2, unconfirmed_count: 0 }
function computeSummary(lines) {
  let revenue = 0, cost = 0, partnerIn = 0, partnerOut = 0, unconfirmed = 0;
  for (const l of (lines || [])) {
    if (!isReady(l)) { unconfirmed += 1; continue; }
    const nzd = lineNetNzd(l);
    const isPartner = l.billed_via === 'partner';
    if (l.direction === 'in') {
      if (isPartner) partnerIn += nzd; else revenue += nzd;
    } else if (l.direction === 'out') {
      if (isPartner) partnerOut += nzd; else cost += nzd;
    }
  }
  revenue = round2(revenue);
  cost = round2(cost);
  const margin = round2(revenue - cost);
  // A project with costs but no revenue yet has a margin_pct of 0, not NaN.
  const marginPct = revenue ? round2((margin / revenue) * 100) : 0;
  const partner = round2(partnerIn - partnerOut);
  return {
    revenue_nzd: revenue,
    cost_nzd: cost,
    margin_nzd: margin,
    margin_pct: marginPct,
    partner_nzd: partner,
    line_count: (lines || []).length,
    unconfirmed_count: unconfirmed,
  };
}

module.exports = { round2, lineNet, lineNetNzd, isReady, computeSummary };
