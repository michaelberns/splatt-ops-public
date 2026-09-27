// snapshot_shim.js
// Snapshot mode: turns the live dashboard into a read-only static page.
//
// Purpose
//   The dashboard normally talks to three things that exist only on the
//   machine running it: PocketBase (port 8090), the files server (port
//   8092), and the Todoist API with a token that must never leave that
//   machine. A hosted copy can reach none of them, and should not. Instead of
//   a second build of the dashboard, this file sits in front of window.fetch
//   and answers every call the page makes from one snapshot.json, built
//   locally by tools/snapshot_build.py (run by bin/publish-dashboard). The
//   same index.html runs in both places without knowing the difference.
//
// When it is active
//   Only when window.SPLATT_CONFIG.SNAPSHOT_URL is set, which only the
//   config.local.js written by the snapshot builder does. Otherwise it
//   returns immediately and the local dashboard behaves exactly as before.
//
// What it does to each request
//   PocketBase GET      answered from snapshot.collections by a small query
//                       engine: one record by id, or a list with equality
//                       (=, !=, ~, <, >) filters joined by && and ||, sorts on
//                       one or more fields, and page / perPage paging.
//   Files server GET    answered from snapshot.endpoints, keyed by path and
//                       query exactly as the dashboard asks. Anything not
//                       captured returns an empty result (or a plain 404 for
//                       a file) instead of hanging.
//   Any write           refused with a 403 and a short toast. Nothing is sent.
//   Any Todoist call    refused with a 403, reads included: there is no token
//                       on the hosted page.
//   Anything else       passed through untouched (CDN scripts, map tiles, the
//                       snapshot itself).
//   It also shows a small "Read only snapshot, as of <time>" pill, and a full
//   page explanation if snapshot.json cannot be loaded.
//
// Public API
//   None. It replaces window.fetch as a side effect of loading, and must be
//   loaded before the dashboard's own script (index.html does this).
//
// The invariant it protects
//   The hosted page can never write anywhere or reach a private service, and
//   the reads it serves are the same rows the live page would have seen when
//   the snapshot was taken. An unparseable filter returns more rows rather
//   than fewer, because extra rows are visible and debuggable while a blank
//   page is not.
//
// How it is tested
//   tests/test_snapshot_shim.js (a plain Node script: node
//   tests/test_snapshot_shim.js, also run from pytest by
//   tests/test_node_suites.py) loads the shim into a vm sandbox with a fake
//   window and fetch, then checks that it is a no-op without SNAPSHOT_URL,
//   that lists, lookups, filters, sorts and paging return the right rows,
//   that captured and uncaptured files-server paths behave, that every write
//   and every Todoist call gets a 403, that CDN requests pass through, and
//   that the as-of pill is shown. See also docs/HOSTED-DASHBOARD.md.

(function () {
  'use strict';

  var CFG = window.SPLATT_CONFIG || {};
  if (!CFG.SNAPSHOT_URL) return;

  var PB = String(CFG.PB_URL || 'http://localhost:8090').replace(/\/+$/, '');
  var FILES = String(CFG.FILES_URL || 'http://localhost:8092').replace(/\/+$/, '');
  var TODOIST = 'https://api.todoist.com';

  var realFetch = window.fetch.bind(window);

  // Start loading the snapshot immediately. Every intercepted call awaits
  // this, so nothing races the app's first render.
  var ready = realFetch(CFG.SNAPSHOT_URL, { cache: 'no-cache' })
    .then(function (r) {
      if (!r.ok) throw new Error('snapshot ' + r.status);
      return r.json();
    })
    .then(function (snap) {
      banner(snap.generated_at);
      return snap;
    })
    .catch(function (err) {
      fatal(err);
      throw err;
    });

  // ---------------------------------------------------------------------
  // Response helpers
  // ---------------------------------------------------------------------

  function json(body, status) {
    return new Response(JSON.stringify(body), {
      status: status || 200,
      headers: { 'Content-Type': 'application/json' }
    });
  }

  function blocked(what) {
    toast(what);
    return json({ code: 403, message: 'Read only snapshot. Nothing was saved.' }, 403);
  }

  function empty() {
    return json({ page: 1, perPage: 0, totalItems: 0, totalPages: 0, items: [] });
  }

  // ---------------------------------------------------------------------
  // A very small PocketBase query engine
  // ---------------------------------------------------------------------
  // The dashboard only ever uses equality filters joined by || and &&, plain
  // field sorts with an optional leading minus, and page/perPage. That is the
  // whole surface, so that is all this implements. Anything it cannot parse
  // falls through returning more rows rather than fewer, because a page with
  // extra rows is debuggable and a blank page is not.

  var ATOM = /^\s*([A-Za-z_][A-Za-z0-9_.]*)\s*(!=|>=|<=|~|=|>|<)\s*(.+?)\s*$/;

  function literal(raw) {
    var s = String(raw).trim();
    if ((s[0] === "'" && s[s.length - 1] === "'") ||
        (s[0] === '"' && s[s.length - 1] === '"')) {
      return s.slice(1, -1);
    }
    if (s === 'true') return true;
    if (s === 'false') return false;
    if (s === 'null') return null;
    if (s !== '' && !isNaN(Number(s))) return Number(s);
    return s;
  }

  function field(rec, path) {
    return path.split('.').reduce(function (o, k) {
      return (o === null || o === undefined) ? undefined : o[k];
    }, rec);
  }

  function atom(rec, expr) {
    var m = ATOM.exec(expr.replace(/^\s*\(|\)\s*$/g, ''));
    if (!m) return null;
    var have = field(rec, m[1]);
    var want = literal(m[3]);
    switch (m[2]) {
      case '=':  return String(have) === String(want);
      case '!=': return String(have) !== String(want);
      case '~':  return String(have === undefined ? '' : have)
                        .toLowerCase()
                        .indexOf(String(want).toLowerCase()) !== -1;
      case '>':  return have > want;
      case '<':  return have < want;
      case '>=': return have >= want;
      case '<=': return have <= want;
    }
    return null;
  }

  function matches(rec, filter) {
    var ors = filter.split('||');
    for (var i = 0; i < ors.length; i++) {
      var ands = ors[i].split('&&');
      var all = true;
      for (var j = 0; j < ands.length; j++) {
        var r = atom(rec, ands[j]);
        if (r === null) return true;   // unparseable, do not hide the row
        if (!r) { all = false; break; }
      }
      if (all) return true;
    }
    return false;
  }

  function sorted(rows, spec) {
    if (!spec) return rows;
    var keys = spec.split(',').map(function (k) {
      k = k.trim();
      return k[0] === '-' ? { k: k.slice(1), dir: -1 } : { k: k, dir: 1 };
    }).filter(function (k) { return k.k; });
    if (!keys.length) return rows;
    return rows.slice().sort(function (a, b) {
      for (var i = 0; i < keys.length; i++) {
        var av = field(a, keys[i].k), bv = field(b, keys[i].k);
        if (av === undefined || av === null) av = '';
        if (bv === undefined || bv === null) bv = '';
        if (av < bv) return -1 * keys[i].dir;
        if (av > bv) return 1 * keys[i].dir;
      }
      return 0;
    });
  }

  function queryCollection(snap, name, id, params) {
    var all = (snap.collections && snap.collections[name]) || [];

    if (id) {
      for (var i = 0; i < all.length; i++) {
        if (all[i].id === id) return json(all[i]);
      }
      return json({ code: 404, message: 'Not in this snapshot.' }, 404);
    }

    var rows = all;
    var filter = params.get('filter');
    if (filter) {
      rows = rows.filter(function (r) { return matches(r, filter); });
    }
    rows = sorted(rows, params.get('sort'));

    var perPage = parseInt(params.get('perPage'), 10) || 30;
    var page = parseInt(params.get('page'), 10) || 1;
    var start = (page - 1) * perPage;

    return json({
      page: page,
      perPage: perPage,
      totalItems: rows.length,
      totalPages: Math.max(1, Math.ceil(rows.length / perPage)),
      items: rows.slice(start, start + perPage)
    });
  }

  // ---------------------------------------------------------------------
  // Files server
  // ---------------------------------------------------------------------
  // These were captured whole by the builder, keyed by path and query exactly
  // as the dashboard asks for them. A path the builder did not capture returns
  // an empty result with a note, so the Files browser degrades to "nothing
  // here" instead of spinning forever on a request that can never land.

  function queryFiles(snap, pathAndQuery) {
    var store = snap.endpoints || {};
    if (Object.prototype.hasOwnProperty.call(store, pathAndQuery)) {
      return json(store[pathAndQuery]);
    }
    // Same path, no query. Covers callers that add a cache buster.
    var bare = pathAndQuery.split('?')[0];
    if (Object.prototype.hasOwnProperty.call(store, bare)) {
      return json(store[bare]);
    }
    console.warn('[snapshot] not captured:', pathAndQuery);
    if (bare.indexOf('/api/folder-contents') === 0) return json({ items: [], entries: [] });
    if (bare.indexOf('/api/file') === 0) {
      return new Response('This file is not part of the snapshot.', {
        status: 404, headers: { 'Content-Type': 'text/plain' }
      });
    }
    return empty();
  }

  // ---------------------------------------------------------------------
  // The interceptor
  // ---------------------------------------------------------------------

  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var method = ((init && init.method) ||
                  (input && input.method) || 'GET').toUpperCase();

    var isPB = url.indexOf(PB) === 0;
    var isFiles = url.indexOf(FILES) === 0;
    var isTodoist = url.indexOf(TODOIST) === 0;

    if (!isPB && !isFiles && !isTodoist) {
      return realFetch(input, init);   // CDN scripts, map tiles, the snapshot itself
    }

    if (method !== 'GET') {
      return Promise.resolve(blocked(
        isTodoist ? 'Todoist changes need the live dashboard.'
                  : 'This is a read only snapshot.'
      ));
    }

    if (isTodoist) return Promise.resolve(blocked('Todoist is not in the snapshot.'));

    return ready.then(function (snap) {
      var rest = url.slice((isPB ? PB : FILES).length);
      var qi = rest.indexOf('?');
      var path = qi === -1 ? rest : rest.slice(0, qi);
      var params = new URLSearchParams(qi === -1 ? '' : rest.slice(qi + 1));

      if (isPB) {
        var m = /^\/api\/collections\/([^/]+)\/records(?:\/([^/?]+))?/.exec(path);
        if (!m) return empty();
        return queryCollection(snap, decodeURIComponent(m[1]),
                               m[2] ? decodeURIComponent(m[2]) : null, params);
      }
      return queryFiles(snap, rest);
    }).catch(function () {
      return json({ code: 503, message: 'Snapshot did not load.' }, 503);
    });
  };

  // ---------------------------------------------------------------------
  // Chrome: the "as of" pill, the blocked write toast, the load failure
  // ---------------------------------------------------------------------

  function css(el, styles) {
    Object.keys(styles).forEach(function (k) { el.style[k] = styles[k]; });
    return el;
  }

  function whenBody(fn) {
    if (document.body) return fn();
    document.addEventListener('DOMContentLoaded', fn);
  }

  function banner(generatedAt) {
    whenBody(function () {
      var when = generatedAt ? new Date(generatedAt) : null;
      var label = when && !isNaN(when)
        ? when.toLocaleString(undefined, {
            weekday: 'short', day: 'numeric', month: 'short',
            hour: '2-digit', minute: '2-digit'
          })
        : 'unknown time';
      var pill = document.createElement('div');
      pill.textContent = 'Read only snapshot · as of ' + label;
      pill.title = 'Nothing on this page can be edited. Rebuilt each time a '
                 + 'new snapshot is published.';
      css(pill, {
        position: 'fixed', left: '12px', bottom: '12px', zIndex: '2147483647',
        padding: '6px 12px', borderRadius: '999px',
        font: '11px/1.4 -apple-system, BlinkMacSystemFont, Inter, Segoe UI, sans-serif',
        letterSpacing: '0.02em', color: '#cbd5e1',
        background: 'rgba(11,13,20,0.88)',
        border: '1px solid rgba(190,210,235,0.28)',
        boxShadow: '0 4px 14px rgba(0,0,0,0.35)',
        backdropFilter: 'blur(6px)', webkitBackdropFilter: 'blur(6px)',
        pointerEvents: 'auto', userSelect: 'none'
      });
      document.body.appendChild(pill);
    });
  }

  var toastEl = null, toastTimer = null;
  function toast(msg) {
    whenBody(function () {
      if (!toastEl) {
        toastEl = css(document.createElement('div'), {
          position: 'fixed', left: '50%', bottom: '56px',
          transform: 'translateX(-50%)', zIndex: '2147483647',
          padding: '10px 16px', borderRadius: '10px',
          font: '12px/1.5 -apple-system, BlinkMacSystemFont, Inter, Segoe UI, sans-serif',
          color: '#fde68a', background: 'rgba(20,14,4,0.94)',
          border: '1px solid rgba(251,191,36,0.45)',
          boxShadow: '0 8px 24px rgba(0,0,0,0.45)',
          maxWidth: '70ch', textAlign: 'center'
        });
        document.body.appendChild(toastEl);
      }
      toastEl.textContent = msg + ' Open the local dashboard to make changes.';
      toastEl.style.display = 'block';
      clearTimeout(toastTimer);
      toastTimer = setTimeout(function () { toastEl.style.display = 'none'; }, 4200);
    });
  }

  function fatal(err) {
    console.error('[snapshot] failed to load', err);
    whenBody(function () {
      var root = document.getElementById('root');
      if (!root) return;
      root.innerHTML =
        '<div style="font:14px/1.6 system-ui;color:#f1f5f9;background:#0f1320;' +
        'padding:48px;min-height:100vh">' +
        '<h1 style="font-size:20px;margin:0 0 12px">The snapshot did not load</h1>' +
        '<p style="color:#94a3b8;max-width:52ch">This page reads a single ' +
        'snapshot.json that is built locally and published with the rest of ' +
        'the files. It is either missing from this deploy or the publish was ' +
        'cut short. Rebuild and publish again.</p></div>';
    });
  }
})();
