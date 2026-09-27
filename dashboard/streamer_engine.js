// streamer_engine.js
// Streamer mode: makes the dashboard safe to show on a screen share or stream.
//
// Purpose
//   With the mode on, almost nothing that identifies a client, contact,
//   supplier, place or amount is readable: every record string, name,
//   rendered number, date, count and dollar figure is starred out, and every
//   map pin lands on a decoy position. The interface itself (page names,
//   buttons, column headers) stays readable, so the dashboard can still be
//   driven. The toggle is in the sidebar and is remembered per browser.
//
// Where it runs
//   In the browser, dashboard/index.html loads it with a plain script tag and
//   reads window.SplattStreamer (the page refuses to start without it). In
//   Node, tests/test_streamer_engine.js requires it.
//
// Public API (window.SplattStreamer, or module.exports)
//   mask(raw)          deep copy of the dashboard's data object with names,
//                      contacts and references starred and coordinates moved
//                      to decoys. Also rebuilds the list of known names.
//   maskText(s)        star names, emails, phones and quote/invoice refs in
//                      one string (money survives: used on data)
//   maskTextAll(s)     maskText plus money and every digit (used on the DOM)
//   setActive(on)      start or stop the DOM layer; active() reads the state
//   setTermsFrom(raw)  rebuild the name list without masking
//   scatterFor(key), setSeed(n), ZONES, STARS   decoy positions, for tests
//
// How it protects the data: two masking layers plus a write guard
//   1. Data layer. mask(raw) deep-copies the data object and stars every
//      string except the structural fields the page's own logic needs (ids,
//      statuses, dates, relation ids), and moves every lat/lng pair to a
//      decoy position. React and the Leaflet popups render from the copy, so
//      the sensitive strings never reach the page. The raw object is left
//      untouched, which is what lets the toggle switch back with no refetch.
//      Numbers are kept real in the copy, because the page sums and sorts
//      them.
//   2. DOM layer. setActive(true) starts a MutationObserver that runs
//      maskTextAll over every text node and a few attributes (title,
//      aria-label, alt, placeholder) as they reach the screen. That stars
//      every rendered digit (counts, dates, totals), money, emails, phones
//      and every known name, including text that arrives by a path the data
//      layer does not cover: the Logs page, playbook runs, the Xero panel,
//      toasts. The observer rewrites the text before the browser paints it.
//   3. Write guard. fetchAPI in index.html refuses any write whose body
//      contains a star while the mode is on, so an edit form opened over
//      masked data cannot save stars into PocketBase over the real record.
//
//   A masked value is four stars. An entity name keeps a stable two
//   character tag (four stars, a dot, then e.g. K3) so two clients stay
//   tellable apart on screen without saying who either one is. Masking is
//   idempotent, so the observer never loops on its own output.
//
// The invariant it protects
//   With the mode on, no original identifying string (name, email, phone,
//   address, reference) survives into what the page renders, and nothing the
//   mask produces can be written back to PocketBase.
//
// How it is tested
//   tests/test_streamer_engine.js (node --test, also run from pytest by
//   tests/test_node_suites.py) feeds in fictional records shaped like the
//   real collections and searches the output for the original strings,
//   rather than for any particular star pattern. It also checks that decoys
//   stay inside the zones and hold still per seed, that structural fields
//   and money survive the data copy, that the DOM pass stars every digit,
//   that masking is idempotent and that the raw object is never mutated.

'use strict';

(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.SplattStreamer = factory();
})(typeof self !== 'undefined' ? self : this, function () {

  const STARS = '★★★★';

  // Decoy zones the pins scatter into: four boxes over land in NZ, so the map
  // still looks plausible while every position is noise. Overseas sites land
  // in these boxes too, so even the country is hidden.
  const ZONES = [
    { latMin: -37.9, latMax: -35.6, lngMin: 174.0, lngMax: 175.8 },
    { latMin: -40.3, latMax: -38.1, lngMin: 174.9, lngMax: 177.8 },
    { latMin: -43.5, latMax: -41.3, lngMin: 171.5, lngMax: 173.8 },
    { latMin: -46.1, latMax: -43.8, lngMin: 168.3, lngMax: 171.1 },
  ];

  // In streamer mode almost nothing should be readable. So the data copy
  // stars EVERY string except the structural fields the page's own logic
  // needs to keep working (ids, statuses, dates, relations), and the DOM net
  // below stars every digit that gets rendered, which hides counts, dates
  // and totals on screen while the numbers underneath stay real for sums and
  // sorting.
  const KEEP_KEYS = new Set([
    'id', 'status', 'due', 'due_date', 'deadline', 'deadline_date', 'date',
    'created', 'updated', 'started', 'finished', 'started_at',
    'finished_at', 'completed_at', 'paid_at', 'currency', 'direction',
    'priority', 'url', 'view_url', 'path', 'file', 'folder',
    'collectionId', 'collectionName', 'kind', 'type', 'icon', 'color',
    'group', 'section', 'sectionId', 'section_id', 'client', 'job',
    'quote', 'supplier', 'contact', 'todoist_id', 'project', 'source',
  ]);

  // Fields that stay tellable apart: starred, with the stable tag.
  const TAG_KEYS = new Set([
    'name', 'site_name', 'title', 'content', 'company', 'summary',
  ]);

  const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}/;
  const ID_SHAPE_RE = /^[A-Za-z0-9_-]{12,22}$/;

  // Inside the Xero snapshot the identifying fields have their own names,
  // and they feed the term list for the DOM net.
  const XERO_KEYS = new Set([
    'name', 'contact', 'contact_name', 'reference', 'invoice_number',
    'number',
  ]);

  // Words too generic to treat as a name on their own, even when they are
  // part of one. "Orchard Lane Ltd" is caught by "Orchard" and "Lane", and
  // starring "New" or "Group" everywhere would chew up ordinary interface
  // text.
  const TOKEN_STOP = new Set([
    'the', 'and', 'new', 'ltd', 'limited', 'group', 'co', 'inc', 'pty',
    'nz', 'engineering',
  ]);

  const EMAIL_RE = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g;
  // Starts with +country or a leading 0 and runs for at least 8 digits, so
  // phone numbers and bank accounts match while dates and amounts do not.
  const PHONE_RE = /(?:\+\d{1,3}[\d\s().-]{6,}\d|\b0\d[\d\s().-]{6,}\d)/g;
  // Quote, invoice and order references still identify a deal on screen.
  const REF_RE = /\b(?:INV|QU|PO|SO)[- ]?\d{3,6}\b/gi;
  const MONEY_RE = /\$\s?\d[\d,]*(?:\.\d+)?/g;
  const CURRENCY_RE = /\b(NZD|AUD|USD|EUR|GBP)\s?\$?\s?\d[\d,]*(?:\.\d+)?/g;

  // --- state ---------------------------------------------------------------
  let active = false;
  let terms = [];        // [{ re: RegExp, tag: string }], longest term first
  let observer = null;
  let seedValue = null;

  // --- deterministic randomness -------------------------------------------
  const hash32 = (s) => {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  };

  const mulberry = (a) => function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };

  // One seed per browser session, so pins hold still across the 30 second
  // refresh but land somewhere new the next time the dashboard is opened.
  const seed = () => {
    if (seedValue != null) return seedValue;
    let s = null;
    try { s = sessionStorage.getItem('splatt_streamer_seed'); } catch (e) {}
    if (!s) {
      s = String(Math.floor(Math.random() * 4294967296));
      try { sessionStorage.setItem('splatt_streamer_seed', s); } catch (e) {}
    }
    seedValue = Number(s) >>> 0;
    return seedValue;
  };
  const setSeed = (n) => { seedValue = n >>> 0; };

  const scatterFor = (key) => {
    const rand = mulberry(hash32(String(key)) ^ seed());
    const zone = ZONES[Math.floor(rand() * ZONES.length)];
    return {
      lat: zone.latMin + rand() * (zone.latMax - zone.latMin),
      lng: zone.lngMin + rand() * (zone.lngMax - zone.lngMin),
    };
  };

  // --- the term list -------------------------------------------------------
  const tagFor = (term) => {
    const n = hash32(String(term).toLowerCase()) % 1296;
    return n.toString(36).toUpperCase().padStart(2, '0');
  };

  const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

  const collectTerms = (raw) => {
    // Two kinds of term. A phrase is a whole recorded name and matches
    // case-insensitively anywhere. A token is one word of a name, because
    // task titles and emails say "Dan" where the record says "Dan Bennett";
    // tokens match as whole capitalised words only, so a first name does not
    // star every ordinary word it happens to spell.
    const phrases = [];
    const tokens = [];
    const push = (v) => {
      const s = (v == null ? '' : String(v)).trim();
      if (s.length < 3) return;
      phrases.push(s);
      s.split(/[\s/,]+/).forEach((w) => {
        const word = w.replace(/[^A-Za-z0-9'-]/g, '');
        if (word.length >= 3 && !TOKEN_STOP.has(word.toLowerCase())) tokens.push(word);
      });
    };
    const pushAliases = (v) => {
      if (!v) return;
      (Array.isArray(v) ? v : String(v).split(',')).forEach(push);
    };
    (raw && raw.clients || []).forEach((r) => { push(r.name); pushAliases(r.aliases); });
    (raw && raw.suppliers || []).forEach((r) => {
      push(r.name); push(r.contact_person); push(r.email); push(r.phone);
      pushAliases(r.aliases);
    });
    (raw && raw.contacts || []).forEach((r) => {
      push(r.name); push(r.email); push(r.phone);
    });
    (raw && raw.equipment || []).forEach((r) => { push(r.site_name); });
    // The Xero snapshot names its contacts itself.
    const dig = (v) => {
      if (!v) return;
      if (Array.isArray(v)) { v.forEach(dig); return; }
      if (typeof v === 'object') {
        Object.keys(v).forEach((k) => {
          if (XERO_KEYS.has(k) && typeof v[k] === 'string') push(v[k]);
          else dig(v[k]);
        });
      }
    };
    dig(raw && raw.xero);
    return { phrases: phrases, tokens: tokens };
  };

  const setTermsFrom = (raw) => {
    const got = collectTerms(raw);
    const seen = {};
    const out = [];
    got.phrases.sort((a, b) => b.length - a.length).forEach((t) => {
      const k = 'p:' + t.toLowerCase();
      if (seen[k]) return;
      seen[k] = true;
      out.push({ re: new RegExp(escapeRe(t), 'gi'), tag: tagFor(t) });
    });
    got.tokens.sort((a, b) => b.length - a.length).forEach((t) => {
      const word = t.charAt(0).toUpperCase() + t.slice(1);
      const k = 't:' + word;
      if (seen[k]) return;
      seen[k] = true;
      // Case sensitive on purpose: "Will" stars, "will" does not.
      out.push({ re: new RegExp('\\b' + escapeRe(word) + '\\b', 'g'), tag: tagFor(t) });
    });
    terms = out;
  };

  // --- text masking --------------------------------------------------------
  // Names, emails and phones. Used on the data copy, where money must
  // survive because the page still sums it.
  const maskText = (s) => {
    if (typeof s !== 'string' || !s) return s;
    let out = s;
    for (let i = 0; i < terms.length; i++) {
      out = out.replace(terms[i].re, STARS + '·' + terms[i].tag);
    }
    out = out.replace(EMAIL_RE, STARS + '@' + STARS);
    out = out.replace(PHONE_RE, STARS + STARS);
    out = out.replace(REF_RE, STARS);
    return out;
  };

  // Everything, money and every digit included. Used on the DOM, where
  // text is display only, so counts, dates, times and totals all star out
  // while the numbers in the data stay real for the page's own sums.
  const DIGITS_RE = /\d+(?:[.,:]\d+)*/g;
  const maskTextAll = (s) => {
    if (typeof s !== 'string' || !s) return s;
    let out = maskText(s);
    out = out.replace(CURRENCY_RE, '$1 ' + STARS);
    out = out.replace(MONEY_RE, '$' + STARS);
    out = out.replace(DIGITS_RE, '\u2605');
    return out;
  };

  // --- the data copy -------------------------------------------------------
  const deepMask = (value, key, inXero) => {
    if (typeof value === 'string') {
      if (!value.trim()) return value;
      // The Xero snapshot's identifying fields wear structural names
      // ('contact' holds a company name there, not a record id), so they
      // are decided before the keep list gets a say.
      if (inXero && XERO_KEYS.has(key)) return STARS;
      if (TAG_KEYS.has(key)) return STARS + '\u00b7' + tagFor(value);
      // Structural strings the page's own logic reads survive, everything
      // else is information and stars out whole. Dates and record ids are
      // kept by shape as well as by key, because collections name them
      // inconsistently, and what a kept date shows on screen is starred
      // by the DOM digit pass anyway.
      if (KEEP_KEYS.has(key) || ISO_DATE_RE.test(value) || ID_SHAPE_RE.test(value)) {
        return value;
      }
      return STARS;
    }
    if (Array.isArray(value)) return value.map((v) => deepMask(v, key, inXero));
    if (value && typeof value === 'object') {
      const out = {};
      Object.keys(value).forEach((k) => {
        out[k] = deepMask(value[k], k, inXero || k === 'xero');
      });
      // A coordinate pair identifies the site on its own, so it moves to a
      // decoy spot. Keyed on the record, not the position, so a geocode
      // re-run on the real data cannot nudge the decoy and hint at change.
      if (Number.isFinite(Number(value.lat)) && Number.isFinite(Number(value.lng))
          && !(Number(value.lat) === 0 && Number(value.lng) === 0)) {
        const at = scatterFor(value.id || value.site_name || value.name || (value.lat + ',' + value.lng));
        out.lat = at.lat;
        out.lng = at.lng;
      }
      return out;
    }
    return value;
  };

  const mask = (raw) => {
    setTermsFrom(raw);
    return deepMask(raw, '', false);
  };

  // --- the DOM net ---------------------------------------------------------
  const ATTRS = ['title', 'aria-label', 'alt', 'placeholder'];

  const maskNode = (node) => {
    const masked = maskTextAll(node.nodeValue);
    if (masked !== node.nodeValue) node.nodeValue = masked;
  };

  const maskAttrs = (el) => {
    for (let i = 0; i < ATTRS.length; i++) {
      const v = el.getAttribute && el.getAttribute(ATTRS[i]);
      if (v) {
        const masked = maskTextAll(v);
        if (masked !== v) el.setAttribute(ATTRS[i], masked);
      }
    }
  };

  const sweep = (root) => {
    if (!root) return;
    if (root.nodeType === 3) { maskNode(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 9) return;
    const tag = root.nodeName;
    if (tag === 'SCRIPT' || tag === 'STYLE') return;
    if (root.nodeType === 1) maskAttrs(root);
    const doc = root.ownerDocument || root;
    const walker = doc.createTreeWalker(root, 5 /* elements + text */);
    let n = walker.currentNode;
    while (n) {
      if (n.nodeType === 3) maskNode(n);
      else if (n.nodeType === 1 && n.nodeName !== 'SCRIPT' && n.nodeName !== 'STYLE') maskAttrs(n);
      n = walker.nextNode();
    }
  };

  const start = () => {
    if (observer || typeof document === 'undefined') return;
    sweep(document.body);
    observer = new MutationObserver((muts) => {
      for (let i = 0; i < muts.length; i++) {
        const mu = muts[i];
        if (mu.type === 'characterData') maskNode(mu.target);
        else if (mu.type === 'attributes') maskAttrs(mu.target);
        else if (mu.type === 'childList') {
          for (let j = 0; j < mu.addedNodes.length; j++) sweep(mu.addedNodes[j]);
        }
      }
    });
    observer.observe(document.body, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: ATTRS,
    });
  };

  const stop = () => {
    if (observer) { observer.disconnect(); observer = null; }
  };

  const setActive = (on) => {
    active = !!on;
    if (active) start(); else stop();
  };

  return {
    STARS: STARS,
    ZONES: ZONES,
    mask: mask,
    maskText: maskText,
    maskTextAll: maskTextAll,
    setTermsFrom: setTermsFrom,
    setActive: setActive,
    active: () => active,
    setSeed: setSeed,
    scatterFor: scatterFor,
  };
});
