// Tests for dashboard/streamer_engine.js (streamer mode).
//
//   node --test tests/test_streamer_engine.js
//
// The property that matters: with the mode on, nothing that identifies a
// client, a contact, a supplier or a place survives into what the page
// renders. So the tests feed in records shaped like the real collections and
// then look for the original strings in the output, not for any particular
// star pattern. All names, numbers and places below are fictional.

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('path');

const S = require(path.join(__dirname, '..', 'dashboard', 'streamer_engine.js'));

const raw = () => ({
  clients: [
    { id: 'c1', name: 'Orchard Lane', city: 'Cromwell', lat: -45.04, lng: 169.20 },
    { id: 'c2', name: 'Clearwater One', city: 'New Plymouth', lat: -39.06, lng: 174.08 },
  ],
  contacts: [
    { id: 'p1', name: 'Dan Bennett', email: 'dan@orchardlane.example.co.nz', phone: '09 000 0042', client: 'c1' },
  ],
  suppliers: [
    { id: 's1', name: 'Taponera', email: 'export@taponera.example.es', phone: '+34 000 00 00 42' },
  ],
  jobs: [
    { id: 'j1', title: 'Orchard Lane / Dan - 375mL change parts', value: 12000, client: 'c1' },
  ],
  interactions: [
    { id: 'i1', summary: 'Emailed Dan Bennett at dan@orchardlane.example.co.nz re the quote' },
  ],
  equipment: [
    { id: 'e1', site_name: 'Orchard Lane Cromwell', lat: -45.0, lng: 169.2, location: 'Cromwell' },
  ],
  xero: { invoices: [{ number: 'INV-41353', contact: 'Tui Valley NZ', amount_due: 1000.50 }] },
});

test('client, contact and supplier names are gone from every string field', () => {
  S.setSeed(1);
  const m = S.mask(raw());
  const flat = JSON.stringify(m);
  for (const name of ['Orchard Lane', 'Clearwater One', 'Dan', 'Bennett', 'Taponera', 'Tui Valley']) {
    assert.ok(!flat.includes(name), name + ' leaked: ' + flat);
  }
});

test('emails, phones and address fields are gone', () => {
  S.setSeed(1);
  const m = S.mask(raw());
  const flat = JSON.stringify(m);
  assert.ok(!flat.includes('orchardlane.example.co.nz'));
  assert.ok(!flat.includes('taponera.example.es'));
  assert.ok(!flat.includes('000 0042'));
  assert.ok(!flat.includes('Cromwell'));
  assert.ok(!flat.includes('New Plymouth'));
});

test('coordinates move to a decoy inside the zones and hold still per seed', () => {
  S.setSeed(7);
  const a = S.mask(raw());
  const b = S.mask(raw());
  const c1a = a.clients[0], c1b = b.clients[0];
  assert.notStrictEqual(c1a.lat, -45.04);
  assert.strictEqual(c1a.lat, c1b.lat);
  assert.strictEqual(c1a.lng, c1b.lng);
  const inZone = S.ZONES.some(z =>
    c1a.lat >= z.latMin && c1a.lat <= z.latMax &&
    c1a.lng >= z.lngMin && c1a.lng <= z.lngMax);
  assert.ok(inZone, 'decoy outside every zone: ' + c1a.lat + ',' + c1a.lng);
  S.setSeed(8);
  const d = S.mask(raw());
  assert.notStrictEqual(d.clients[0].lat, c1a.lat);
});

test('two clients stay tellable apart after masking', () => {
  S.setSeed(1);
  const m = S.mask(raw());
  assert.notStrictEqual(m.clients[0].name, m.clients[1].name);
});

test('money survives the data copy but not the DOM text pass', () => {
  S.setSeed(1);
  const m = S.mask(raw());
  assert.strictEqual(m.jobs[0].value, 12000);
  assert.strictEqual(m.xero.invoices[0].amount_due, 1000.50);
  const t = S.maskTextAll('Pipeline $12,000 and NZD 1,000.50 due');
  assert.ok(!t.includes('12,000'));
  assert.ok(!t.includes('1,000.50'));
});

test('quote and invoice references are starred in text', () => {
  S.setSeed(1);
  S.mask(raw());
  const t = S.maskText('Chasing INV-41353 and QU-7994 with the client');
  assert.ok(!t.includes('41353'));
  assert.ok(!t.includes('7994'));
});

test('lowercase ordinary words survive a name token', () => {
  S.setSeed(1);
  S.mask(raw());
  const t = S.maskText('the clear water from one valley will be bottled daily');
  assert.strictEqual(t, 'the clear water from one valley will be bottled daily');
});

test('the DOM pass stars every rendered digit: counts, dates, times', () => {
  S.setSeed(1);
  S.mask(raw());
  const t = S.maskTextAll('46 Sites shown, 4 with no address, due 30 Sep at 14:30');
  assert.ok(!/\d/.test(t), 'digits leaked: ' + t);
  assert.ok(t.includes('Sites shown'), 'interface chrome should survive: ' + t);
});

test('the data copy keeps the structural fields the page logic needs', () => {
  S.setSeed(1);
  const m = S.mask({ jobs: [{ id: 'j1', title: 'Orchard Lane parts', status: 'quoted', due_date: '2026-09-30', value: 12000, client: 'c1' }] });
  const j = m.jobs[0];
  assert.strictEqual(j.status, 'quoted');
  assert.strictEqual(j.due_date, '2026-09-30');
  assert.strictEqual(j.value, 12000);
  assert.strictEqual(j.client, 'c1');
  assert.ok(!j.title.includes('Orchard Lane'));
});

test('every non structural record string stars out whole', () => {
  S.setSeed(1);
  const m = S.mask({ assignments: [{ id: 'a1', description: 'Ring Andrea about the GA drawing for the 20ft container' }] });
  assert.ok(!m.assignments[0].description.includes('Andrea'));
  assert.ok(!m.assignments[0].description.includes('container'));
});

test('masking is idempotent, so the observer cannot loop', () => {
  S.setSeed(1);
  S.mask(raw());
  const once = S.maskTextAll('Orchard Lane owes $12,000, call 09 000 0042');
  assert.strictEqual(S.maskTextAll(once), once);
});

test('the raw object is never mutated', () => {
  S.setSeed(1);
  const r = raw();
  const before = JSON.stringify(r);
  S.mask(r);
  assert.strictEqual(JSON.stringify(r), before);
});

test('bank account shapes are masked by the phone rule', () => {
  S.setSeed(1);
  S.mask(raw());
  const t = S.maskTextAll('BNK 00-0000-0000000-00');
  assert.ok(!t.includes('0000000'));
});
