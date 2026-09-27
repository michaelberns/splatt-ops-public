#!/usr/bin/env node
// Files and playbook server for the Splatt operations dashboard.
//
// A small HTTP API (Node built-ins only, nothing to install) that gives the
// dashboard and the agent controlled access to the project folders and runs
// the playbook state machine.
//
// "SS Folders" is the folder tree that holds the business documents:
//   <Client>/<Project>/PROJECT.md      one folder per client, one per project
//   _Suppliers/<Supplier>/RELATIONSHIP.md
//   _Equipment Library/*.md
//   _howtotasks/                       how-to notes, playbook specs and runs
//
// Routes
//   GET    /api/project-files        every PROJECT.md, RELATIONSHIP.md and how-to note
//   PUT    /api/project-files        rewrite one text file
//   POST   /api/upload-file          write one binary file (base64), creating folders
//   GET    /api/folder-contents      list a folder, plus files one level down
//   GET    /api/file                 serve one file (HTML links rewritten to this API)
//   DELETE /api/file                 delete one file (never a folder or a symlink)
//   POST   /api/playbook/start       start a playbook run
//   POST   /api/playbook/step        submit the current step of a run
//   GET    /api/playbook/runs        list runs (?status=, ?limit=)
//   GET    /api/playbook/run/:id     one run in full
//   DELETE /api/playbook/run/:id     delete a run
//   GET    /api/playbook/specs       every playbook spec
//   GET    /api/playbook/health      pass and failure counts per playbook and step
//   GET    /api/assignments          Todoist task mirror, one row per task
//   GET    /api/xero                 the latest Xero snapshot and its findings
//   GET    /api/logs, /api/logs/tail the log files the dashboard's Logs page shows
//   POST   /api/publish              start bin/publish-dashboard (loopback only)
//   GET    /api/publish/status       whether a publish is running, and its output
//   GET    /api/health               liveness, plus which .env and SS Folders are in use
//
// Every path a caller passes goes through validateRelativePath(), which
// refuses anything that resolves outside SS Folders.
//
// Configuration (environment variables)
//   PORT               default 8092
//   HOST               default 127.0.0.1 (loopback only). Set 0.0.0.0 to
//                      listen on every interface.
//   SS_FOLDERS_PATH    default ~/Documents/Splatt/SS Folders
//   PB_URL             PocketBase, default http://localhost:8090
//   SPLATT_STATE_DIR   where the Xero snapshot is read from, default <repo>/state
//   SPLATT_ENV_FILE        .env to load, default <repo>/.env

const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

// PORT lets a second copy run beside the live one (the tests start one on a
// free port). HOST defaults to loopback, so the API, which has no login, is
// reachable only from this machine unless HOST is set on purpose.
const PORT = parseInt(process.env.PORT || '8092', 10);
const HOST = process.env.HOST || '127.0.0.1';
const HOME = process.env.HOME || process.env.USERPROFILE || '';

// The SS Folders root. The default matches the rest of the system; tests
// point SS_FOLDERS_PATH at a throwaway tree.
const SS_FOLDERS = process.env.SS_FOLDERS_PATH
  || path.join(HOME || os.homedir(), 'Documents', 'Splatt', 'SS Folders');

// ----- Env loader. Read-only, no dotenv dependency. -----
//
// Loads KEY=value lines from the first readable file in this list, without
// overriding variables that are already set:
//   1. SPLATT_ENV_FILE, if set;
//   2. the repo's own .env (one directory up from server/).
// The validators below read their API credentials (Gmail, Todoist, Xero)
// from the environment. /api/health reports which file was loaded.
const ENV_CANDIDATES = [
  process.env.SPLATT_ENV_FILE,
  path.join(__dirname, '..', '.env'),
].filter(Boolean);
function _loadEnvFile() {
  for (const candidate of ENV_CANDIDATES) {
    try {
      const raw = fs.readFileSync(candidate, 'utf-8');
      for (const line of raw.split('\n')) {
        const m = line.match(/^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$/);
        if (m && !process.env[m[1]]) {
          process.env[m[1]] = m[2].replace(/^['"](.*)['"]$/, '$1');
        }
      }
      return candidate;
    } catch { /* try next */ }
  }
  return null;
}
const ENV_LOADED_FROM = _loadEnvFile();

// Where the Xero snapshot and its findings are read from. It sits beside the
// repo rather than inside SS Folders because it is generated output, not a
// document the operator edits. state/ is gitignored, so a fresh clone has
// neither file and /api/xero must handle that. Overridable so a test can
// point it at a throwaway directory.
const STATE_DIR = process.env.SPLATT_STATE_DIR
  || path.join(__dirname, '..', 'state');

const MIME_TYPES = {
  '.pdf': 'application/pdf', '.html': 'text/html', '.htm': 'text/html',
  '.txt': 'text/plain', '.md': 'text/markdown', '.csv': 'text/csv',
  '.json': 'application/json', '.xml': 'application/xml',
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
  '.gif': 'image/gif', '.webp': 'image/webp', '.svg': 'image/svg+xml',
  '.doc': 'application/msword', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.xls': 'application/vnd.ms-excel', '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.zip': 'application/zip',
};

// Turn a caller-supplied path into an absolute path inside SS Folders, or
// return null if it would land anywhere else.
//
// Accepted forms:
//   - relative to the SS Folders root, e.g. "Alex Carter/Triblock Line/PROJECT.md"
//   - absolute, e.g. "<SS_FOLDERS>/Alex Carter/Triblock Line/PROJECT.md"
//
// This is the security boundary for every route that touches the disk: the
// API has no login, so this check is what keeps the rest of the disk out of
// reach. path.normalize collapses any '..' segments before the prefix check,
// so "Client/../../etc/passwd" is refused. The string check cannot see
// symlinks, so the file-serving, folder-listing and delete routes also
// lstat the result and refuse a symlink.
function validateRelativePath(relPath) {
  if (!relPath || typeof relPath !== 'string') return null;

  const candidate = path.isAbsolute(relPath)
    ? relPath
    : path.join(SS_FOLDERS, relPath);

  const normalized = path.normalize(candidate);
  if (normalized !== SS_FOLDERS && !normalized.startsWith(SS_FOLDERS + path.sep)) return null;
  return normalized;
}

// Read a text file with its modification time, or null if it cannot be read.
function readMd(filePath) {
  try {
    const content = fs.readFileSync(filePath, 'utf-8');
    const modified = fs.statSync(filePath).mtime.toISOString();
    return { content, modified };
  } catch { return null; }
}

// Collect every Markdown document the dashboard shows, as
// { type, client, project, path, modified, content }:
//   - project:   <Client>/<Project>/PROJECT.md
//   - supplier:  _Suppliers/README.md and _Suppliers/<Supplier>/RELATIONSHIP.md
//   - equipment: _Equipment Library/*.md
//   - howto:     _howtotasks/*.md and one level of subfolders
// A missing folder is skipped, not an error.
function scanProjectFiles() {
  const results = [];
  let entries;
  try { entries = fs.readdirSync(SS_FOLDERS, { withFileTypes: true }); } catch { return results; }

  for (const clientDir of entries) {
    if (!clientDir.isDirectory() || clientDir.name.startsWith('.')) continue;

    // _Suppliers, _Equipment Library and _howtotasks are scanned separately below.
    if (clientDir.name === '_Suppliers' || clientDir.name === '_Equipment Library' || clientDir.name === '_howtotasks') continue;

    const clientPath = path.join(SS_FOLDERS, clientDir.name);
    let projectDirs;
    try { projectDirs = fs.readdirSync(clientPath, { withFileTypes: true }); } catch { continue; }

    for (const projDir of projectDirs) {
      if (!projDir.isDirectory() || projDir.name.startsWith('.')) continue;
      const mdPath = path.join(clientPath, projDir.name, 'PROJECT.md');
      const md = readMd(mdPath);
      if (!md) continue;

      results.push({
        type: 'project',
        client: clientDir.name,
        project: projDir.name,
        path: `${clientDir.name}/${projDir.name}/PROJECT.md`,
        modified: md.modified,
        content: md.content,
      });
    }
  }

  // Scan _Suppliers/*/RELATIONSHIP.md
  const suppliersDir = path.join(SS_FOLDERS, '_Suppliers');
  try {
    const supplierEntries = fs.readdirSync(suppliersDir, { withFileTypes: true });
    // README.md at the root of _Suppliers
    const readmeMd = readMd(path.join(suppliersDir, 'README.md'));
    if (readmeMd) {
      results.push({
        type: 'supplier',
        client: '_Suppliers',
        project: 'Index',
        path: '_Suppliers/README.md',
        modified: readmeMd.modified,
        content: readmeMd.content,
      });
    }
    for (const supplierDir of supplierEntries) {
      if (!supplierDir.isDirectory() || supplierDir.name.startsWith('.')) continue;
      const mdPath = path.join(suppliersDir, supplierDir.name, 'RELATIONSHIP.md');
      const md = readMd(mdPath);
      if (!md) continue;

      results.push({
        type: 'supplier',
        client: '_Suppliers',
        project: supplierDir.name,
        path: `_Suppliers/${supplierDir.name}/RELATIONSHIP.md`,
        modified: md.modified,
        content: md.content,
      });
    }
  } catch { /* no _Suppliers folder */ }

  // Scan _Equipment Library/*.md
  const equipDir = path.join(SS_FOLDERS, '_Equipment Library');
  try {
    const equipEntries = fs.readdirSync(equipDir, { withFileTypes: true });
    for (const file of equipEntries) {
      if (!file.isFile() || !file.name.endsWith('.md')) continue;
      const mdPath = path.join(equipDir, file.name);
      const md = readMd(mdPath);
      if (!md) continue;

      results.push({
        type: 'equipment',
        client: '_Equipment Library',
        project: file.name.replace(/\.md$/, ''),
        path: `_Equipment Library/${file.name}`,
        modified: md.modified,
        content: md.content,
      });
    }
  } catch { /* no _Equipment Library folder */ }

  // Scan _howtotasks: README.md + subdirs (playbooks, standards, templates) containing *.md
  const howtoDir = path.join(SS_FOLDERS, '_howtotasks');
  try {
    const howtoReadme = readMd(path.join(howtoDir, 'README.md'));
    if (howtoReadme) {
      results.push({
        type: 'howto',
        client: '_howtotasks',
        project: 'Index',
        path: '_howtotasks/README.md',
        modified: howtoReadme.modified,
        content: howtoReadme.content,
      });
    }
    const howtoEntries = fs.readdirSync(howtoDir, { withFileTypes: true });
    for (const entry of howtoEntries) {
      if (entry.isFile() && entry.name.endsWith('.md') && entry.name !== 'README.md') {
        const md = readMd(path.join(howtoDir, entry.name));
        if (md) {
          results.push({
            type: 'howto',
            client: '_howtotasks',
            project: entry.name.replace(/\.md$/, ''),
            path: `_howtotasks/${entry.name}`,
            modified: md.modified,
            content: md.content,
          });
        }
      }
      if (entry.isDirectory() && !entry.name.startsWith('.')) {
        const subDir = path.join(howtoDir, entry.name);
        const subEntries = fs.readdirSync(subDir, { withFileTypes: true });
        for (const sub of subEntries) {
          if (!sub.isFile() || !sub.name.endsWith('.md')) continue;
          const md = readMd(path.join(subDir, sub.name));
          if (!md) continue;
          results.push({
            type: 'howto',
            client: '_howtotasks',
            project: `${entry.name}/${sub.name.replace(/\.md$/, '')}`,
            path: `_howtotasks/${entry.name}/${sub.name}`,
            modified: md.modified,
            content: md.content,
          });
        }
      }
    }
  } catch { /* no _howtotasks folder */ }

  return results;
}

// ============================================================================
// PLAYBOOK STATE MACHINE
//
// A playbook is a fixed sequence of steps for a routine job, defined in
// _howtotasks/playbooks/<playbook_id>.spec.json. The spec lists the inputs
// it needs, its steps, the validators each step must pass, and a final
// validation for the whole run.
//
// How a run moves
//   1. POST /api/playbook/start checks the required inputs, creates a run
//      with status 'open' and returns the first step.
//   2. POST /api/playbook/step must name the run's current step; any other
//      step id is refused with 409, so steps happen in order.
//   3. With status 'done' the step's validators run. If any fails, the
//      reply is HTTP 400 with failed_validators and the run stays on the
//      same step. 'skip' is allowed only for steps marked skippable;
//      'fail' records the step as failed and moves on.
//   4. After the last step, the spec's final_validation runs and the run is
//      closed as 'completed' or 'failed_validation'.
//
// Validators check the real system rather than the agent's report of it:
// a PocketBase record exists, a file changed after the run started, a Gmail
// draft or a Todoist task or a Xero invoice is in the stated state.
//
// Storage: every run is saved as JSON in _howtotasks/runs/ and mirrored to
// the playbook_runs collection in PocketBase when PocketBase is reachable.
// ============================================================================

const PB_URL = process.env.PB_URL || 'http://localhost:8090';
const PLAYBOOKS_DIR = path.join(SS_FOLDERS, '_howtotasks', 'playbooks');
const RUNS_DIR = path.join(SS_FOLDERS, '_howtotasks', 'runs');

function ensureRunsDir() {
  try { fs.mkdirSync(RUNS_DIR, { recursive: true }); } catch {}
}

function loadPlaybookSpec(playbook_id) {
  const safe = String(playbook_id).replace(/[^a-z0-9_-]/gi, '');
  if (!safe || safe !== playbook_id) return null;
  const specPath = path.join(PLAYBOOKS_DIR, `${safe}.spec.json`);
  try {
    const raw = fs.readFileSync(specPath, 'utf-8');
    return JSON.parse(raw);
  } catch { return null; }
}

function newRunId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

// A request to the playbook_runs collection. Throws on any non-2xx answer.
async function pbFetch(method, endpoint, body) {
  const res = await fetch(`${PB_URL}/api/collections/playbook_runs${endpoint}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`PB ${method} ${endpoint} ${res.status}`);
  return res.json();
}

// Save a run: mirror it to PocketBase if possible, then always write the
// local JSON file. If PocketBase is down the run is still saved, with
// _storage 'local' and the reason in _pb_error.
async function saveRun(run) {
  try {
    if (run._pb_id) {
      await pbFetch('PATCH', `/records/${run._pb_id}`, runToPbRecord(run));
    } else {
      const created = await pbFetch('POST', '/records', runToPbRecord(run));
      run._pb_id = created.id;
    }
    run._storage = 'pocketbase';
  } catch (e) {
    run._storage = 'local';
    run._pb_error = e.message;
  }
  // The local file is written every time; the list, health and step
  // routes all read runs from disk.
  ensureRunsDir();
  fs.writeFileSync(path.join(RUNS_DIR, `${run.run_id}.json`), JSON.stringify(run, null, 2));
  return run;
}

function loadRun(run_id) {
  const safe = String(run_id).replace(/[^a-z0-9_-]/gi, '');
  if (!safe || safe !== run_id) return null;
  try {
    const raw = fs.readFileSync(path.join(RUNS_DIR, `${safe}.json`), 'utf-8');
    return JSON.parse(raw);
  } catch { return null; }
}

// Parsed run files, keyed by filename and invalidated when mtime or size
// changes. /api/playbook/health and /api/playbook/runs both walk every run
// file, and the dashboard polls them, so each file is parsed once and then
// only stat'ed on later requests.
const _runCache = new Map(); // filename -> { mtimeMs, size, run }

function listRuns(filter) {
  ensureRunsDir();
  let files;
  try { files = fs.readdirSync(RUNS_DIR).filter(f => f.endsWith('.json')); }
  catch { return []; }
  const runs = [];
  const present = new Set();
  for (const f of files) {
    present.add(f);
    const full = path.join(RUNS_DIR, f);
    let st;
    try { st = fs.statSync(full); } catch { continue; }
    const hit = _runCache.get(f);
    let r;
    if (hit && hit.mtimeMs === st.mtimeMs && hit.size === st.size) {
      r = hit.run;
    } else {
      try { r = JSON.parse(fs.readFileSync(full, 'utf-8')); } catch { continue; }
      _runCache.set(f, { mtimeMs: st.mtimeMs, size: st.size, run: r });
    }
    if (!filter || !filter.status || r.status === filter.status) runs.push(r);
  }
  // Drop cache entries for runs that have been deleted off disk.
  if (_runCache.size > present.size) {
    for (const k of _runCache.keys()) if (!present.has(k)) _runCache.delete(k);
  }
  runs.sort((a, b) => (b.started_at || '').localeCompare(a.started_at || ''));
  return runs;
}

// Delete a run from both PocketBase (best-effort) and the local JSON mirror.
// Returns { ok: true, deleted_pb, deleted_local } on success, or { ok: false, error }.
async function deleteRun(run_id) {
  const safe = String(run_id).replace(/[^a-z0-9_-]/gi, '');
  if (!safe || safe !== run_id) return { ok: false, error: 'invalid run_id' };

  const run = loadRun(safe);
  if (!run) return { ok: false, error: 'not_found' };

  let deleted_pb = false;
  let pb_error = null;
  if (run._pb_id) {
    try {
      // Called directly rather than through pbFetch, because a DELETE answer
      // may have an empty body that pbFetch would try to parse as JSON.
      const res = await fetch(`${PB_URL}/api/collections/playbook_runs/records/${run._pb_id}`, {
        method: 'DELETE',
      });
      if (res.ok || res.status === 404) {
        deleted_pb = true;
      } else {
        pb_error = `PB DELETE returned ${res.status}`;
      }
    } catch (e) {
      pb_error = e.message;
    }
  }

  let deleted_local = false;
  try {
    fs.unlinkSync(path.join(RUNS_DIR, `${safe}.json`));
    deleted_local = true;
  } catch (e) {
    return { ok: false, error: `local unlink failed: ${e.message}`, deleted_pb, pb_error };
  }

  return { ok: true, run_id: safe, deleted_pb, deleted_local, pb_error };
}

function runToPbRecord(run) {
  return {
    playbook_id: run.playbook_id,
    status: run.status,
    trigger: run.trigger || '',
    inputs: run.inputs || {},
    context: run.context || {},
    steps: run.steps || [],
    current_step: run.current_step || '',
    started_at: run.started_at,
    completed_at: run.completed_at || null,
    validation_summary: run.validation_summary || null,
  };
}

// Fill $inputs.x, $evidence.x, $run.x and $context.x placeholders in a spec
// string, for example "$inputs.project_path" or "$run.started_at". Dotted
// keys reach into nested objects. A missing value becomes "".
function resolvePlaceholders(template, ctx) {
  if (typeof template !== 'string') return template;
  return template.replace(/\$(inputs|evidence|run|context)\.([\w.]+)/g, (m, scope, key) => {
    const source = ctx[scope] || {};
    const val = key.split('.').reduce((o, k) => (o == null ? undefined : o[k]), source);
    return val == null ? '' : String(val);
  });
}

// resolvePlaceholders applied to every string inside an object or array.
function resolveObject(obj, ctx) {
  if (obj == null) return obj;
  if (typeof obj === 'string') return resolvePlaceholders(obj, ctx);
  if (Array.isArray(obj)) return obj.map(o => resolveObject(o, ctx));
  if (typeof obj === 'object') {
    const out = {};
    for (const k of Object.keys(obj)) out[k] = resolveObject(obj[k], ctx);
    return out;
  }
  return obj;
}

// ----- Validators -----
// Each validator takes its spec entry and the run context and returns
// { ok: true, ...details } or { ok: false, reason }.

// extracted_fields: every named field is present and non-empty in the evidence.
function validateExtractedFields(spec, ctx) {
  const evidence = ctx.evidence || {};
  const missing = (spec.fields || []).filter(f => evidence[f] == null || evidence[f] === '');
  return missing.length === 0
    ? { ok: true }
    : { ok: false, reason: `missing fields: ${missing.join(', ')}` };
}

// file_modified: the file exists inside SS Folders and was modified at or
// after `since` (usually $run.started_at).
function validateFileModified(spec, ctx) {
  const resolved = resolveObject(spec, ctx);
  const fullPath = validateRelativePath(resolved.path);
  if (!fullPath) return { ok: false, reason: `invalid path: ${resolved.path}` };
  let stat;
  try { stat = fs.statSync(fullPath); }
  catch { return { ok: false, reason: `file not found: ${resolved.path}` }; }
  const since = resolved.since ? new Date(resolved.since).getTime() : 0;
  const mtime = stat.mtime.getTime();
  if (mtime < since) {
    return { ok: false, reason: `file ${resolved.path} not modified since ${resolved.since} (mtime ${stat.mtime.toISOString()})` };
  }
  return { ok: true, mtime: stat.mtime.toISOString() };
}

// pocketbase_record: a record in `collection` matches every field in
// `match` (and was created at or after `since`, if given). With
// capture_id_as, the record's id is saved into the step's evidence under
// that name so later steps can refer to it.
async function validatePocketbaseRecord(spec, ctx) {
  const resolved = resolveObject(spec, ctx);
  const filterParts = [];
  for (const [k, v] of Object.entries(resolved.match || {})) {
    filterParts.push(`${k}="${String(v).replace(/"/g, '\\"')}"`);
  }
  if (resolved.since) {
    // PocketBase stores datetimes as "YYYY-MM-DD HH:MM:SS.sssZ" (a space,
    // not ISO's "T"), and its filter compares them as strings. run.started_at
    // is ISO with a "T", and "T" sorts after " ", so an unconverted value
    // would make every since-check fail. Convert to PocketBase's form.
    const pbSince = String(resolved.since).replace('T', ' ');
    filterParts.push(`created>="${pbSince}"`);
  }
  const filterStr = filterParts.join(' && ');
  const url = `${PB_URL}/api/collections/${encodeURIComponent(resolved.collection)}/records?perPage=1&filter=${encodeURIComponent(filterStr)}`;
  try {
    const res = await fetch(url);
    if (!res.ok) return { ok: false, reason: `PB query failed: ${res.status}` };
    const data = await res.json();
    if (!data.items || data.items.length === 0) {
      return { ok: false, reason: `no ${resolved.collection} record matching ${filterStr}` };
    }
    const out = { ok: true, record_id: data.items[0].id };
    if (resolved.capture_id_as) out.capture_id_as = resolved.capture_id_as;
    return out;
  } catch (e) {
    return { ok: false, reason: `PB error: ${e.message}` };
  }
}

// ----- Validators that call external APIs: gmail_message, todoist_task, xero_invoice -----
// Credentials come from the environment (.env). If they are missing the
// validator returns ok:false naming the variables to set. Every network call
// has a 10 second timeout so one slow API cannot hold a step open.

function _withTimeout(promise, ms, label) {
  let to;
  const timeout = new Promise((_, rej) => {
    to = setTimeout(() => rej(new Error(`${label} timed out after ${ms}ms`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(to));
}

// Gmail OAuth access tokens, cached in memory per account and refreshed a
// minute before they expire. `account` selects GMAIL_REFRESH_TOKEN_<ACCOUNT>.
const _gmailTokenCache = {}; // account -> { access_token, expires_at }
async function _gmailAccessToken(account) {
  const cached = _gmailTokenCache[account];
  if (cached && cached.expires_at > Date.now() + 60_000) return cached.access_token;
  const refreshKey = `GMAIL_REFRESH_TOKEN_${account.toUpperCase()}`;
  const clientIdKey = 'GMAIL_OAUTH_CLIENT_ID';
  const clientSecretKey = 'GMAIL_OAUTH_CLIENT_SECRET';
  const refresh = process.env[refreshKey];
  const clientId = process.env[clientIdKey];
  const clientSecret = process.env[clientSecretKey];
  if (!refresh || !clientId || !clientSecret) {
    throw new Error(`gmail credentials missing, set ${refreshKey}, ${clientIdKey}, ${clientSecretKey} in .env`);
  }
  const params = new URLSearchParams({
    client_id: clientId,
    client_secret: clientSecret,
    refresh_token: refresh,
    grant_type: 'refresh_token',
  });
  const res = await _withTimeout(fetch('https://oauth2.googleapis.com/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: params.toString(),
  }), 10_000, 'gmail token refresh');
  if (!res.ok) throw new Error(`gmail token refresh failed: ${res.status}`);
  const data = await res.json();
  _gmailTokenCache[account] = {
    access_token: data.access_token,
    expires_at: Date.now() + (data.expires_in || 3500) * 1000,
  };
  return data.access_token;
}

// gmail_message: a draft (kind 'draft', the default) or a message (any
// other kind) with this id exists, and optionally is in the given thread,
// is addressed to `to`, and is newer than `since`.
async function validateGmailMessage(spec, ctx) {
  const resolved = resolveObject(spec, ctx);
  const kind = resolved.kind || 'draft';
  const account = (resolved.account || 'u1').toLowerCase();
  const match = resolved.match || {};
  const id = match.id;
  if (!id) return { ok: false, reason: 'gmail_message: match.id is required' };
  let token;
  try { token = await _gmailAccessToken(account); }
  catch (e) { return { ok: false, reason: e.message }; }
  const base = 'https://gmail.googleapis.com/gmail/v1/users/me';
  const url = kind === 'draft' ? `${base}/drafts/${encodeURIComponent(id)}?format=metadata`
            : `${base}/messages/${encodeURIComponent(id)}?format=metadata`;
  let res;
  try {
    res = await _withTimeout(fetch(url, { headers: { Authorization: `Bearer ${token}` } }),
                             10_000, `gmail ${kind} get`);
  } catch (e) { return { ok: false, reason: `gmail network: ${e.message}` }; }
  if (res.status === 404) return { ok: false, reason: `gmail ${kind} ${id} not found` };
  if (!res.ok) return { ok: false, reason: `gmail ${kind} get failed: ${res.status}` };
  const data = await res.json();
  const msg = kind === 'draft' ? (data.message || {}) : data;
  if (match.threadId && msg.threadId !== match.threadId) {
    return { ok: false, reason: `gmail ${kind} threadId mismatch: got ${msg.threadId}, want ${match.threadId}` };
  }
  if (match.to) {
    const headers = (msg.payload && msg.payload.headers) || [];
    const toHdr = headers.find(h => h.name && h.name.toLowerCase() === 'to');
    if (!toHdr || !String(toHdr.value).toLowerCase().includes(String(match.to).toLowerCase())) {
      return { ok: false, reason: `gmail ${kind} 'to' header missing ${match.to}` };
    }
  }
  if (resolved.since) {
    const sinceMs = new Date(resolved.since).getTime();
    if (msg.internalDate && Number(msg.internalDate) < sinceMs) {
      return { ok: false, reason: `gmail ${kind} internalDate older than $run.started_at` };
    }
  }
  return { ok: true, message_id: msg.id || id, thread_id: msg.threadId };
}

// todoist_task: the task exists and its section, project, parent, completion
// and priority match the spec; optionally its content or description
// contains a given string.
async function validateTodoistTask(spec, ctx) {
  const resolved = resolveObject(spec, ctx);
  const match = resolved.match || {};
  const id = match.id;
  if (!id) return { ok: false, reason: 'todoist_task: match.id is required' };
  const token = process.env.TODOIST_API_TOKEN;
  if (!token) return { ok: false, reason: 'todoist_task: TODOIST_API_TOKEN not set in .env' };
  let res;
  try {
    // Todoist's unified API v1. The older /rest/v2 endpoints answer 410 Gone.
    res = await _withTimeout(fetch(`https://api.todoist.com/api/v1/tasks/${encodeURIComponent(id)}`,
      { headers: { Authorization: `Bearer ${token}` } }), 10_000, 'todoist task get');
  } catch (e) { return { ok: false, reason: `todoist network: ${e.message}` }; }
  if (res.status === 404) return { ok: false, reason: `todoist task ${id} not found` };
  // 410 means Todoist has retired this endpoint too; say so plainly instead
  // of reporting it as a missing task.
  if (res.status === 410) {
    return { ok: false, reason: 'todoist endpoint retired, this code needs updating' };
  }
  if (res.status === 400) {
    // v1 refuses the older numeric task ids with a 400. The task may exist,
    // so the reason names the likely cause instead of a bare status.
    return { ok: false, reason: `todoist rejected id ${id}, it may be an old style id` };
  }
  if (!res.ok) return { ok: false, reason: `todoist task get failed: ${res.status}` };
  const task = await res.json();
  // v1 has no is_completed field; it reports completion as `checked`, with
  // `completed_at` beside it. Specs are written against is_completed, so it
  // is derived here. Read directly it would be undefined, and a spec asking
  // for a completed task could never pass.
  const normalised = Object.assign({}, task, {
    is_completed: task.is_completed !== undefined
      ? task.is_completed
      : Boolean(task.checked || task.completed_at),
  });
  const checks = ['section_id', 'project_id', 'parent_id', 'is_completed', 'priority'];
  for (const k of checks) {
    if (match[k] === undefined) continue;
    const want = match[k];
    const got = normalised[k];
    if (typeof want === 'boolean' ? Boolean(got) !== want : String(got) !== String(want)) {
      return { ok: false, reason: `todoist task ${id} ${k} mismatch: got ${got}, want ${want}` };
    }
  }
  if (match.content_includes && !String(task.content || '').includes(match.content_includes)) {
    return { ok: false, reason: `todoist task ${id} content missing '${match.content_includes}'` };
  }
  // The same check on the description, which is where this system keeps its
  // own lines on a task (the estimate, the project path, the real due date
  // and the trigger line). It lets a spec confirm the agent actually wrote
  // such a line.
  if (match.description_includes
      && !String(task.description || '').includes(match.description_includes)) {
    return { ok: false,
      reason: `todoist task ${id} description missing '${match.description_includes}'` };
  }
  return { ok: true, task_id: task.id,
    task_state: { section_id: task.section_id, is_completed: normalised.is_completed } };
}

// Xero OAuth2 access token, cached in memory and refreshed a minute before
// it expires. Needs XERO_CLIENT_ID, XERO_CLIENT_SECRET, XERO_REFRESH_TOKEN
// and XERO_TENANT_ID.
let _xeroTokenCache = null;
async function _xeroAccessToken() {
  if (_xeroTokenCache && _xeroTokenCache.expires_at > Date.now() + 60_000) {
    return _xeroTokenCache;
  }
  const clientId = process.env.XERO_CLIENT_ID;
  const clientSecret = process.env.XERO_CLIENT_SECRET;
  const refresh = process.env.XERO_REFRESH_TOKEN;
  const tenantId = process.env.XERO_TENANT_ID;
  if (!clientId || !clientSecret || !refresh || !tenantId) {
    throw new Error('xero credentials missing, set XERO_CLIENT_ID, XERO_CLIENT_SECRET, XERO_REFRESH_TOKEN, XERO_TENANT_ID in .env');
  }
  const auth = Buffer.from(`${clientId}:${clientSecret}`).toString('base64');
  const params = new URLSearchParams({ grant_type: 'refresh_token', refresh_token: refresh });
  const res = await _withTimeout(fetch('https://identity.xero.com/connect/token', {
    method: 'POST',
    headers: { Authorization: `Basic ${auth}`, 'Content-Type': 'application/x-www-form-urlencoded' },
    body: params.toString(),
  }), 10_000, 'xero token refresh');
  if (!res.ok) throw new Error(`xero token refresh failed: ${res.status}, re-auth needed`);
  const data = await res.json();
  _xeroTokenCache = {
    access_token: data.access_token,
    tenant_id: tenantId,
    expires_at: Date.now() + (data.expires_in || 1800) * 1000,
  };
  return _xeroTokenCache;
}

// xero_invoice: the invoice exists and its Status, Type and Total match the
// spec. Status is the important one: when a playbook creates a draft
// invoice, this confirms Xero really holds it as DRAFT.
async function validateXeroInvoice(spec, ctx) {
  const resolved = resolveObject(spec, ctx);
  const match = resolved.match || {};
  const invoiceId = match.InvoiceID;
  if (!invoiceId) return { ok: false, reason: 'xero_invoice: match.InvoiceID is required' };
  let tok;
  try { tok = await _xeroAccessToken(); }
  catch (e) { return { ok: false, reason: e.message }; }
  let res;
  try {
    res = await _withTimeout(fetch(`https://api.xero.com/api.xro/2.0/Invoices/${encodeURIComponent(invoiceId)}`, {
      headers: {
        Authorization: `Bearer ${tok.access_token}`,
        'Xero-tenant-id': tok.tenant_id,
        Accept: 'application/json',
      },
    }), 10_000, 'xero invoice get');
  } catch (e) { return { ok: false, reason: `xero network: ${e.message}` }; }
  if (res.status === 404) return { ok: false, reason: `xero invoice ${invoiceId} not found` };
  if (!res.ok) return { ok: false, reason: `xero invoice get failed: ${res.status}` };
  const data = await res.json();
  const inv = (data.Invoices && data.Invoices[0]) || null;
  if (!inv) return { ok: false, reason: `xero invoice ${invoiceId} not in response` };
  for (const k of ['Status', 'Type']) {
    if (match[k] !== undefined && String(inv[k]) !== String(match[k])) {
      return { ok: false, reason: `xero invoice ${invoiceId} ${k} mismatch: got ${inv[k]}, want ${match[k]}` };
    }
  }
  if (match.Total !== undefined && Number(inv.Total) !== Number(match.Total)) {
    return { ok: false, reason: `xero invoice ${invoiceId} Total mismatch: got ${inv.Total}, want ${match.Total}` };
  }
  return { ok: true, invoice_id: invoiceId, invoice_state: { Status: inv.Status, Type: inv.Type, Total: inv.Total } };
}

// Run a list of validator specs in order and return one result per entry.
// An unknown type fails rather than being skipped.
async function runValidators(validators, ctx) {
  const results = [];
  for (const v of validators || []) {
    let r;
    if (v.type === 'extracted_fields') r = validateExtractedFields(v, ctx);
    else if (v.type === 'file_modified') r = validateFileModified(v, ctx);
    else if (v.type === 'pocketbase_record') r = await validatePocketbaseRecord(v, ctx);
    else if (v.type === 'gmail_message') r = await validateGmailMessage(v, ctx);
    else if (v.type === 'todoist_task') r = await validateTodoistTask(v, ctx);
    else if (v.type === 'xero_invoice') r = await validateXeroInvoice(v, ctx);
    else r = { ok: false, reason: `unknown validator type: ${v.type}` };
    results.push({ type: v.type, ...r });
  }
  return results;
}

// ----- Helpers for endpoint logic -----

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', c => { body += c; });
    req.on('end', () => {
      try { resolve(body ? JSON.parse(body) : {}); }
      catch (e) { reject(e); }
    });
  });
}

// The step after currentStepId in the spec, or null if it is the last one.
function nextStep(spec, currentStepId) {
  const idx = spec.steps.findIndex(s => s.id === currentStepId);
  if (idx === -1 || idx === spec.steps.length - 1) return null;
  return spec.steps[idx + 1];
}

// ============================================================================
// XERO SNAPSHOT (GET /api/xero)
//
// The dashboard is a file:// page and cannot read the disk, so this route
// hands it the Xero snapshot and the findings that were checked against it.
//
// Nothing here talks to Xero. The Xero connector is available to the agent
// in its session, so capturing is a step the agent runs: it writes
// state/xero-snapshot.json (tools/xero_snapshot.py) and then
// state/xero-gaps.json (tools/xero_gaps.py). This route only reads them.
//
// Rules for the reply:
//   1. A missing file is not an error. state/ is gitignored, so a fresh
//      clone has neither file. The reply is 200 with available:false and a
//      reason the dashboard can show.
//   2. Missing data never comes back as zero. A money panel showing $0 owed
//      could not be told apart from everyone having paid, so without a
//      snapshot there is no `money` at all.
//   3. The age travels with the numbers (captured_at, age_hours, stale), so
//      an old figure is never shown as if it were current.
// ============================================================================

// After this many hours the snapshot is still served but marked stale. A
// day old is old enough for a payment received since to be missing.
const XERO_STALE_HOURS = 24;

// Read one JSON file from STATE_DIR as { ok, data } or { ok: false, reason }.
// missingReason is passed in because the two files mean different things
// when absent: no snapshot means Xero has not been captured; no findings
// means it was captured but the checks have not been run. Each needs a
// different fix.
function _readStateJson(name, missingReason) {
  const file = path.join(STATE_DIR, name);
  let raw;
  try {
    raw = fs.readFileSync(file, 'utf-8');
  } catch (e) {
    if (e.code === 'ENOENT') {
      return { ok: false, reason: missingReason };
    }
    return { ok: false, reason: `Could not read ${name}: ${e.message}` };
  }
  try {
    return { ok: true, data: JSON.parse(raw) };
  } catch (e) {
    // A file caught mid-write reads as broken JSON. Report that, so it is
    // not mistaken for "no data".
    return { ok: false, reason: `${name} is not valid JSON: ${e.message}` };
  }
}

// Hours since captured_at, to one decimal place, or null if the date cannot be parsed.
function _ageHours(captured_at) {
  const t = Date.parse(captured_at || '');
  if (Number.isNaN(t)) return null;
  return Math.round(((Date.now() - t) / 3600000) * 10) / 10;
}

// Build the /api/xero reply from the two state files.
function readXeroState() {
  const snap = _readStateJson('xero-snapshot.json',
    'Xero has not been captured yet. Ask the agent for an ops pass.');
  if (!snap.ok) {
    return {
      available: false,
      reason: snap.reason,
      stale: true,
      stale_after_hours: XERO_STALE_HOURS,
      findings: [],
      findings_available: false,
      findings_reason: 'There is no snapshot to check.',
    };
  }

  const s = snap.data || {};
  const age = _ageHours(s.captured_at);
  const gaps = _readStateJson('xero-gaps.json',
    'The Xero checks have not been run on this snapshot yet.');
  const g = gaps.ok ? (gaps.data || {}) : {};

  // Only show findings that were checked against this exact snapshot. The
  // two files are written by separate commands, so the money can be
  // refreshed while older findings remain. Showing them together could list
  // a problem that is already fixed, or miss a new one.
  const matched = Boolean(
    gaps.ok && s.captured_at && g.snapshot_captured_at === s.captured_at
  );

  return {
    available: true,
    reason: null,
    organisation: s.organisation || null,
    base_currency: s.base_currency || null,
    captured_at: s.captured_at || null,
    // Xero caches its own reports, so this is how old the figures already
    // were at capture time. It is separate from captured_at.
    xero_last_refreshed: s.xero_last_refreshed || null,
    age_hours: age,
    // An age that cannot be worked out is treated as old, not as fresh.
    stale: age === null || age > XERO_STALE_HOURS,
    stale_after_hours: XERO_STALE_HOURS,
    money: s.money || null,
    completeness: s.completeness || null,
    counts: {
      invoices: (s.invoices || []).length,
      receivables: (s.receivables || []).length,
      payables: (s.payables || []).length,
      contacts: (s.contacts || []).length,
    },
    findings: matched ? (g.findings || []) : [],
    findings_available: matched,
    findings_reason: matched
      ? null
      : (gaps.ok
        ? 'The checks were run against a different snapshot, so they are not being shown.'
        : gaps.reason),
    findings_checked_at: matched ? (g.checked_at || null) : null,
    total_amount_flagged: matched ? (g.total_amount_flagged || 0) : null,
    overdue_days_threshold: matched ? (g.overdue_days_threshold || null) : null,
  };
}

// ==================================================================
// Logs page and publishing
// ==================================================================
const LOGS_DIR = path.join(__dirname, '..', 'logs');

// Is a publish in flight? bin/publish-dashboard writes its pid to
// state/publish.lock and removes the file when it finishes. A run that was
// killed can leave the lock behind, so the pid is checked rather than
// trusted: signal 0 asks whether that process still exists without
// affecting it.
function publishRunning() {
  const lock = path.join(STATE_DIR, 'publish.lock');
  try {
    const pid = parseInt(fs.readFileSync(lock, 'utf-8').trim(), 10);
    if (!pid) return { running: false };
    process.kill(pid, 0);
    return { running: true, pid, since: fs.statSync(lock).mtimeMs };
  } catch {
    return { running: false };
  }
}

// The log files the dashboard's Logs page shows, in display order (the ones
// that show whether the stack is healthy come first). Each entry names the
// process that writes the file and what the file records, because a bare
// filename like "daemon.log" does not say that.
const LOG_REGISTRY = [
  { file: 'daemon.log', label: 'Sync loop',
    writer: 'python3 -m daemon loop, started by bin/start',
    what: 'The 60 second loop. Todoist into PocketBase, overdue sweeps, the auto validator run, and draining the Telegram outbox. This is the one that proves the stack is alive.' },
  { file: 'project-files-server.log', label: 'Files + playbook server',
    writer: 'node server/project-files-server.js, port 8092',
    what: 'Serves this dashboard and the playbook API. Every playbook start and step, every file read and write, lands here.' },
  { file: 'autostart.log', label: 'launchd agent',
    writer: 'com.splatt.stack, only when launchd starts the stack',
    what: 'Only written when the launchd agent runs. If you start the stack by hand in a terminal this file stays stale, which is normal and not a fault.' },
  { file: 'validator.log', label: 'Validator',
    writer: 'bin/splatt-validate',
    what: 'Every validation run, its verdict, and which rules blocked. Exit 2 means it could not run, which is not a pass.' },
  { file: 'ss-folders-guard.log', label: 'SS Folders guard',
    writer: 'guard process, started by bin/start',
    what: 'Puts project files back where they belong when something writes them to the wrong place.' },
  { file: 'ss-deliverables-guard.log', label: 'Deliverables guard',
    writer: 'guard process, started by bin/start',
    what: 'Same idea for finished documents, so a quote or report does not get stranded outside its project folder.' },
  { file: 'tick_off.log', label: 'Cleanup',
    writer: 'python3 -m tools.tick_off',
    what: 'The cleanup run that closes tasks already proven done by email. Dry run unless it was called with --write.' },
  { file: 'backup.log', label: 'Backups',
    writer: 'the backup script',
    what: 'PocketBase backup runs.' },
  { file: 'publish-dashboard.log', label: 'Dashboard publishes',
    writer: 'bin/publish-dashboard, from the Publish page or from cron',
    what: 'Every push of the hosted read only copy, appended run after run. The Publish page shows the same runs as rows you can read; this is the raw output if a row says failed and you want to know why.' },
];

const server = http.createServer(async (req, res) => {
  const parsedUrl = new URL(req.url, `http://localhost:${PORT}`);
  const pathname = parsedUrl.pathname;

  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, PUT, POST, DELETE, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }

  if (pathname === '/api/project-files' && req.method === 'GET') {
    const files = scanProjectFiles();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(files));
  } else if (pathname === '/api/project-files' && req.method === 'PUT') {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      try {
        const { path: filePath, content } = JSON.parse(body);
        if (!filePath || typeof content !== 'string') {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'path and content required' }));
          return;
        }
        const fullPath = validateRelativePath(filePath);
        if (!fullPath) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Invalid path' }));
          return;
        }
        fs.writeFileSync(fullPath, content, 'utf-8');
        const modified = fs.statSync(fullPath).mtime.toISOString();
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, modified }));
      } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: e.message }));
      }
    });
  } else if (pathname === '/api/upload-file' && req.method === 'POST') {
    // Binary-safe upload: { path, contentBase64 }. Creates parent dirs (e.g.
    // <project>/bills/ or <project>/invoices/) and writes the decoded file.
    // validateRelativePath keeps the path inside SS Folders.
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      try {
        const { path: filePath, contentBase64 } = JSON.parse(body);
        if (!filePath || typeof contentBase64 !== 'string') {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'path and contentBase64 required' }));
          return;
        }
        const fullPath = validateRelativePath(filePath);
        if (!fullPath) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Invalid path' }));
          return;
        }
        fs.mkdirSync(path.dirname(fullPath), { recursive: true });
        const buf = Buffer.from(contentBase64, 'base64');
        fs.writeFileSync(fullPath, buf);
        const modified = fs.statSync(fullPath).mtime.toISOString();
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, path: filePath, size: buf.length, modified }));
      } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: e.message }));
      }
    });
  } else if (pathname === '/api/folder-contents' && req.method === 'GET') {
    const relPath = parsedUrl.searchParams.get('path');
    const fullPath = validateRelativePath(relPath);
    if (!fullPath) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Invalid path' }));
      return;
    }
    try {
      const stat = fs.lstatSync(fullPath);
      if (!stat.isDirectory()) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Not a directory' }));
        return;
      }
      const entries = [];
      const dirEntries = fs.readdirSync(fullPath, { withFileTypes: true });
      for (const entry of dirEntries) {
        if (entry.name.startsWith('.')) continue;
        const entryPath = path.join(fullPath, entry.name);
        const entryLstat = fs.lstatSync(entryPath);
        if (entryLstat.isSymbolicLink()) continue;
        if (entryLstat.isDirectory()) {
          entries.push({ name: entry.name, type: 'directory', size: 0, modified: entryLstat.mtime.toISOString(), extension: '', parentDir: null });
          // Recurse one level
          try {
            const subEntries = fs.readdirSync(entryPath, { withFileTypes: true });
            for (const sub of subEntries) {
              if (sub.name.startsWith('.')) continue;
              const subPath = path.join(entryPath, sub.name);
              const subLstat = fs.lstatSync(subPath);
              if (subLstat.isSymbolicLink() || subLstat.isDirectory()) continue;
              entries.push({ name: sub.name, type: 'file', size: subLstat.size, modified: subLstat.mtime.toISOString(), extension: path.extname(sub.name).toLowerCase(), parentDir: entry.name });
            }
          } catch { /* skip unreadable subdirs */ }
        } else if (entryLstat.isFile()) {
          entries.push({ name: entry.name, type: 'file', size: entryLstat.size, modified: entryLstat.mtime.toISOString(), extension: path.extname(entry.name).toLowerCase(), parentDir: null });
        }
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ folderPath: fullPath, entries }));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
  } else if (pathname === '/api/file' && req.method === 'GET') {
    const relPath = parsedUrl.searchParams.get('path');
    const fullPath = validateRelativePath(relPath);
    if (!fullPath) {
      res.writeHead(400, { 'Content-Type': 'text/plain' });
      res.end('Invalid path');
      return;
    }
    try {
      const stat = fs.lstatSync(fullPath);
      if (!stat.isFile() || stat.isSymbolicLink()) {
        res.writeHead(404, { 'Content-Type': 'text/plain' });
        res.end('Not a file');
        return;
      }
      const ext = path.extname(fullPath).toLowerCase();
      const mime = MIME_TYPES[ext] || 'application/octet-stream';
      // For HTML files, rewrite relative URLs so embedded resources resolve through the API
      if (ext === '.html' || ext === '.htm') {
        let html = fs.readFileSync(fullPath, 'utf-8');
        const fileDir = path.relative(SS_FOLDERS, path.dirname(fullPath));
        html = html.replace(/(src|href)="(?!https?:\/\/|mailto:|#|data:)([^"]+)"/g, (match, attr, url) => {
          const rel = fileDir + '/' + url;
          return `${attr}="/api/file?path=${encodeURIComponent(rel)}"`;
        });
        res.writeHead(200, { 'Content-Type': mime, 'Content-Length': Buffer.byteLength(html) });
        res.end(html);
        return;
      }
      const stream = fs.createReadStream(fullPath);
      res.writeHead(200, { 'Content-Type': mime, 'Content-Length': stat.size });
      stream.pipe(res);
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'text/plain' });
      res.end(e.message);
    }
  } else if (pathname === '/api/file' && req.method === 'DELETE') {
    // Delete a single file. Containment enforced by validateRelativePath (must
    // stay under SS_FOLDERS); symlinks and directories are refused.
    const relPath = parsedUrl.searchParams.get('path');
    const fullPath = validateRelativePath(relPath);
    if (!fullPath) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Invalid path' }));
      return;
    }
    try {
      const stat = fs.lstatSync(fullPath);
      if (stat.isSymbolicLink() || !stat.isFile()) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Not a deletable file' }));
        return;
      }
      fs.unlinkSync(fullPath);
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, path: relPath }));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
  } else if (pathname === '/api/playbook/start' && req.method === 'POST') {
    try {
      const body = await readJsonBody(req);
      const { playbook_id, inputs = {}, context = {}, trigger = '' } = body;
      const spec = loadPlaybookSpec(playbook_id);
      if (!spec) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: `Playbook not found: ${playbook_id}` }));
        return;
      }
      const missing = (spec.inputs_required || []).filter(k => inputs[k] == null || inputs[k] === '');
      if (missing.length) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: `Missing required inputs: ${missing.join(', ')}` }));
        return;
      }
      const run = {
        run_id: newRunId(),
        playbook_id,
        status: 'open',
        trigger,
        inputs,
        context: Object.assign({}, context, { _spec_version: spec.version || 1, _spec_last_updated: spec.last_updated || null }),
        steps: [],
        current_step: spec.steps[0].id,
        started_at: new Date().toISOString(),
      };
      await saveRun(run);
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        run_id: run.run_id,
        playbook_id,
        step: spec.steps[0],
        total_steps: spec.steps.length,
        storage: run._storage,
      }));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
  } else if (pathname === '/api/playbook/step' && req.method === 'POST') {
    try {
      const body = await readJsonBody(req);
      const { run_id, step_id, status, evidence = {} } = body;
      const run = loadRun(run_id);
      if (!run) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: `Run not found: ${run_id}` }));
        return;
      }
      if (run.status !== 'open') {
        res.writeHead(409, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: `Run is ${run.status}, not open` }));
        return;
      }
      if (run.current_step !== step_id) {
        res.writeHead(409, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: `Expected step ${run.current_step}, got ${step_id}` }));
        return;
      }
      const spec = loadPlaybookSpec(run.playbook_id);
      const stepSpec = spec.steps.find(s => s.id === step_id);
      if (!stepSpec) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: `Step ${step_id} not in spec (spec drift?)` }));
        return;
      }

      // Validators see the evidence from every earlier step plus this one.
      const allEvidence = Object.assign({}, ...run.steps.map(s => s.evidence || {}), evidence);
      const ctx = { inputs: run.inputs, context: run.context, run, evidence: allEvidence };

      let validation = [];
      if (status === 'done') {
        validation = await runValidators(stepSpec.validates, ctx);
        const failed = validation.filter(v => !v.ok);
        if (failed.length) {
          // Do not advance. The agent must fix what the validators check and
        // resubmit the same step.
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({
            error: 'Step validation failed',
            step_id,
            failed_validators: failed,
            retry: stepSpec,
          }));
          return;
        }
        // Save record ids the validators captured (capture_id_as) into this
        // step's evidence, so later steps can use them.
        for (const v of validation) {
          if (v.capture_id_as && v.record_id) evidence[v.capture_id_as] = v.record_id;
        }
      } else if (status === 'skip') {
        if (!stepSpec.skippable) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: `Step ${step_id} is not skippable` }));
          return;
        }
      } else if (status !== 'fail') {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: `Invalid status: ${status} (expected done|skip|fail)` }));
        return;
      }

      run.steps.push({
        step_id,
        status,
        evidence,
        validation,
        ts: new Date().toISOString(),
      });

      const next = nextStep(spec, step_id);
      if (next) {
        run.current_step = next.id;
        await saveRun(run);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ run_id, step: next, validation }));
      } else {
        // Last step done: run the final validation and close the run.
        const finalCtx = { inputs: run.inputs, context: run.context, run, evidence: Object.assign({}, ...run.steps.map(s => s.evidence || {})) };
        const finalValidation = await runValidators(spec.final_validation, finalCtx);
        const finalFailed = finalValidation.filter(v => !v.ok);
        run.current_step = '';
        run.completed_at = new Date().toISOString();
        run.validation_summary = { steps_passed: run.steps.filter(s => s.status === 'done').length, final: finalValidation };
        run.status = finalFailed.length ? 'failed_validation' : 'completed';
        await saveRun(run);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          run_id,
          complete: true,
          status: run.status,
          validation_summary: run.validation_summary,
        }));
      }
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
  } else if (pathname === '/api/playbook/runs' && req.method === 'GET') {
    const status = parsedUrl.searchParams.get('status');
    // Optional ?limit=N keeps only the newest N runs; without it every run
    // is returned. The dashboard polls this route, so it asks for a limit.
    const limitRaw = parseInt(parsedUrl.searchParams.get('limit') || '', 10);
    const limit = Number.isFinite(limitRaw) && limitRaw > 0 ? limitRaw : null;
    let runs = listRuns(status ? { status } : null);
    if (limit) runs = runs.slice(0, limit);
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(runs.map(r => ({
      run_id: r.run_id,
      playbook_id: r.playbook_id,
      status: r.status,
      started_at: r.started_at,
      completed_at: r.completed_at,
      current_step: r.current_step,
      steps_recorded: (r.steps || []).length,
      storage: r._storage,
    }))));
  } else if (pathname.startsWith('/api/playbook/run/') && req.method === 'GET') {
    const run_id = pathname.replace('/api/playbook/run/', '');
    const run = loadRun(run_id);
    if (!run) { res.writeHead(404); res.end(JSON.stringify({ error: 'Not found' })); return; }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(run));
  } else if (pathname.startsWith('/api/playbook/run/') && req.method === 'DELETE') {
    const run_id = pathname.replace('/api/playbook/run/', '');
    try {
      const result = await deleteRun(run_id);
      if (!result.ok) {
        const code = result.error === 'not_found' ? 404 : (result.error === 'invalid run_id' ? 400 : 500);
        res.writeHead(code, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(result));
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(result));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: e.message }));
    }
  } else if (pathname === '/api/logs' && req.method === 'GET') {
    // The dashboard's Logs page. Only files in LOG_REGISTRY are listed, with
    // their size and modification time; anything else in logs/ (for example
    // files written by test runs) is not shown.
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(LOG_REGISTRY.map(l => {
      const f = path.join(LOGS_DIR, l.file);
      let size = null, mtime = null;
      try { const st = fs.statSync(f); size = st.size; mtime = st.mtimeMs; } catch {}
      return { ...l, size, mtime, exists: size !== null };
    })));
  } else if (pathname === '/api/logs/tail' && req.method === 'GET') {
    // The last `lines` lines (default 200, max 2000) of one registered log.
    // Logs can be several MB, so only the final 512 KB is read, and the
    // first, probably partial, line of that chunk is dropped.
    const name = parsedUrl.searchParams.get('name');
    const lines = Math.min(parseInt(parsedUrl.searchParams.get('lines') || '200', 10) || 200, 2000);
    const entry = LOG_REGISTRY.find(l => l.file === name);
    if (!entry) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Unknown log. Only logs in the registry can be read.' }));
      return;
    }
    const f = path.join(LOGS_DIR, entry.file);
    try {
      const st = fs.statSync(f);
      const want = 512 * 1024;
      const start = Math.max(0, st.size - want);
      const fd = fs.openSync(f, 'r');
      const buf = Buffer.alloc(Math.min(want, st.size));
      fs.readSync(fd, buf, 0, buf.length, start);
      fs.closeSync(fd);
      let text = buf.toString('utf-8');
      if (start > 0) text = text.slice(text.indexOf('\n') + 1);
      const all = text.split('\n');
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        file: entry.file, label: entry.label,
        size: st.size, mtime: st.mtimeMs,
        truncated: start > 0,
        lines: all.slice(-lines),
      }));
    } catch (e) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ file: entry.file, label: entry.label, size: 0, mtime: null, lines: [], missing: true }));
    }
  } else if (pathname === '/api/playbook/specs' && req.method === 'GET') {
    let files;
    try { files = fs.readdirSync(PLAYBOOKS_DIR).filter(f => f.endsWith('.spec.json')); }
    catch { files = []; }
    const specs = files.map(f => {
      try { return JSON.parse(fs.readFileSync(path.join(PLAYBOOKS_DIR, f), 'utf-8')); }
      catch { return null; }
    }).filter(Boolean);
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(specs));
  } else if (pathname === '/api/playbook/health' && req.method === 'GET') {
    // Per playbook and per step: how many runs, how many completed or
    // failed, and the last failure reason. needs_review is set when any step
    // has failed review_after_failures times (default 5), a sign the spec or
    // the process behind it needs attention.
    try {
      // Every run is written to local JSON (see saveRun), so the local
      // files are the complete history.
      const allRuns = listRuns();
      let specFiles = [];
      try { specFiles = fs.readdirSync(PLAYBOOKS_DIR).filter(f => f.endsWith('.spec.json')); } catch {}
      const specs = {};
      for (const f of specFiles) {
        try {
          const s = JSON.parse(fs.readFileSync(path.join(PLAYBOOKS_DIR, f), 'utf-8'));
          specs[s.playbook_id] = s;
        } catch {}
      }
      const out = {};
      for (const pb_id of Object.keys(specs)) {
        const spec = specs[pb_id];
        out[pb_id] = {
          playbook_id: pb_id,
          version: spec.version || 1,
          last_updated: spec.last_updated || null,
          review_after_failures: spec.review_after_failures || 5,
          total_runs: 0,
          completed: 0,
          failed_validation: 0,
          abandoned: 0,
          open: 0,
          completion_rate: 0,
          last_run_at: null,
          last_failure_at: null,
          steps: spec.steps.map(s => ({
            step_id: s.id,
            description: s.description,
            attempts: 0,
            failures: 0,
            failure_rate: 0,
            last_failure_reason: null,
            last_failure_at: null,
          })),
          needs_review: false,
        };
      }
      for (const r of allRuns) {
        const agg = out[r.playbook_id];
        if (!agg) continue;
        agg.total_runs++;
        if (r.status === 'completed') agg.completed++;
        else if (r.status === 'failed_validation') agg.failed_validation++;
        else if (r.status === 'abandoned') agg.abandoned++;
        else if (r.status === 'open') agg.open++;
        if (!agg.last_run_at || (r.started_at || '') > agg.last_run_at) agg.last_run_at = r.started_at;
        // Each recorded step is one attempt. It counts as a failure if the
        // step was reported as 'fail' or any of its validators failed.
        for (const s of (r.steps || [])) {
          const stepAgg = agg.steps.find(x => x.step_id === s.step_id);
          if (!stepAgg) continue;
          stepAgg.attempts++;
          const failedV = (s.validation || []).filter(v => !v.ok);
          if (s.status === 'fail' || failedV.length) {
            stepAgg.failures++;
            const reason = failedV.length ? failedV[0].reason : 'step_status_fail';
            stepAgg.last_failure_reason = reason;
            stepAgg.last_failure_at = s.ts || r.started_at;
            if (!agg.last_failure_at || stepAgg.last_failure_at > agg.last_failure_at) {
              agg.last_failure_at = stepAgg.last_failure_at;
            }
          }
        }
      }
      // Percentages, and the needs_review flag.
      for (const pb_id of Object.keys(out)) {
        const agg = out[pb_id];
        agg.completion_rate = agg.total_runs ? Math.round((agg.completed / agg.total_runs) * 100) : 0;
        for (const s of agg.steps) {
          s.failure_rate = s.attempts ? Math.round((s.failures / s.attempts) * 100) : 0;
        }

        agg.needs_review = agg.steps.some(s => s.failures >= agg.review_after_failures);
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(Object.values(out)));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
  } else if (pathname === '/api/assignments' && req.method === 'GET') {
    // De-duplicated assignments feed for the dashboard. Read-only.
    //
    // Assignments are the PocketBase mirror of Todoist tasks. The collection
    // can hold several rows with the same todoist_id, and PocketBase's list
    // API has no DISTINCT, so this route pages through the collection
    // newest first and keeps the first (newest) row per todoist_id. Rows with
    // no todoist_id are kept individually. The browser then makes one request
    // instead of downloading every duplicate.
    try {
      // Every page is read, not just the newest few. Duplicates cluster among
      // recently synced tasks, so a partial scan would fill up with copies
      // and miss older tasks that have a single row. maxPages is only a
      // safety bound (300 pages x 500 rows = 150,000 rows).
      const maxPages = Math.max(1, Math.min(300, parseInt(parsedUrl.searchParams.get('maxPages') || '300', 10) || 300));
      const perPage = 500;
      const seen = new Set();
      const items = [];
      let pagesFetched = 0;
      for (let page = 1; page <= maxPages; page++) {
        let data;
        try {
          const r = await fetch(`${PB_URL}/api/collections/assignments/records?perPage=${perPage}&page=${page}&sort=-created`);
          if (!r.ok) break;
          data = await r.json();
        } catch { break; }
        pagesFetched++;
        const pageItems = (data && data.items) || [];
        for (const rec of pageItems) {
          const key = rec.todoist_id ? `t:${rec.todoist_id}` : `id:${rec.id}`;
          if (seen.has(key)) continue; // sorted by -created, so the first seen is the newest
          seen.add(key);
          items.push(rec);
        }
        if (pageItems.length < perPage) break; // reached the last page
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ items, deduped: true, unique: items.length, pagesFetched }));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
  } else if (pathname === '/api/xero' && req.method === 'GET') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(readXeroState()));
  } else if (pathname === '/api/publish' && req.method === 'POST') {
    // Start a publish of the hosted read-only copy of the dashboard
    // (see docs/HOSTED-DASHBOARD.md).
    //
    // This is the only route that runs a program, so it is deliberately
    // narrow: it takes no arguments from the caller and can only start one
    // fixed script in this repo, so there is nothing to inject a command
    // into. The script reads PocketBase and uploads a folder, both safe to
    // repeat.
    //
    // It only accepts requests from the loopback address. The server
    // listens on 127.0.0.1 by default, but if HOST is set to expose it on a
    // network, anyone there could otherwise press this button. Set
    // SPLATT_PUBLISH_ALLOW_LAN=1 to allow it from other machines on purpose.
    const remote = req.socket.remoteAddress || '';
    const local = remote === '127.0.0.1' || remote === '::1' || remote === '::ffff:127.0.0.1';
    if (!local && process.env.SPLATT_PUBLISH_ALLOW_LAN !== '1') {
      res.writeHead(403, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        error: 'Publishing can only be started from this machine itself.',
        detail: `This request came from ${remote}. Open the dashboard at localhost on the machine running the server, or set SPLATT_PUBLISH_ALLOW_LAN=1 before starting the server.`,
      }));
      return;
    }
    const running = publishRunning();
    if (running.running) {
      res.writeHead(409, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'A publish is already running.', pid: running.pid }));
      return;
    }
    const script = path.join(__dirname, '..', 'bin', 'publish-dashboard');
    if (!fs.existsSync(script)) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'bin/publish-dashboard is missing from this repo.' }));
      return;
    }
    // Run detached, with output discarded here, because the script writes
    // its own log. A publish takes tens of seconds, so the reply (202) comes
    // back at once and the page polls /api/publish/status; closing the tab
    // does not stop the run.
    try {
      const child = spawn('/bin/bash', [script, '--trigger', 'dashboard'], {
        cwd: path.join(__dirname, '..'),
        detached: true,
        stdio: 'ignore',
        env: { ...process.env, SPLATT_PUBLISH_TEE: '' },
      });
      child.unref();
      res.writeHead(202, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ started: true, pid: child.pid }));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
  } else if (pathname === '/api/publish/status' && req.method === 'GET') {
    // Whether a publish is running, the last 120 lines of the current (or
    // most recent) run's output, and whether this caller may start one. The
    // history of past publishes is in PocketBase (publish_log).
    const running = publishRunning();
    let lines = [], mtime = null;
    try {
      const f = path.join(LOGS_DIR, 'publish-dashboard.current');
      const st = fs.statSync(f);
      mtime = st.mtimeMs;
      lines = fs.readFileSync(f, 'utf-8').split('\n').slice(-120);
    } catch {}
    const remote = req.socket.remoteAddress || '';
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      ...running,
      can_publish: remote === '127.0.0.1' || remote === '::1' || remote === '::ffff:127.0.0.1'
        || process.env.SPLATT_PUBLISH_ALLOW_LAN === '1',
      mtime,
      lines,
    }));
  } else if (pathname === '/api/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      status: 'ok',
      // Which .env file was loaded at startup (null if none), so it is easy
      // to confirm the server is using the intended credentials.
      env_loaded_from: ENV_LOADED_FROM,
      ss_folders: SS_FOLDERS,
    }));
  } else {
    res.writeHead(404);
    res.end('Not found');
  }
});

server.listen(PORT, HOST, () => {
  console.log(`Project files server running on http://${HOST}:${PORT}`);
});
