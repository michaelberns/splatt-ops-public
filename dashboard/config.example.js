// Dashboard configuration template.
//
// Copy this file to config.local.js in the same folder and fill in your own
// values. config.local.js is listed in .gitignore, so your token and
// addresses never get committed. index.html loads config.local.js before
// anything else and reads these values from window.SPLATT_CONFIG. If the
// file is missing, the page says so on screen instead of half loading.
//
// The hosted read-only copy does not use this file: tools/snapshot_build.py
// writes its own config.local.js there, which adds SNAPSHOT_URL and switches
// on dashboard/snapshot_shim.js (see docs/HOSTED-DASHBOARD.md).

window.SPLATT_CONFIG = {
  // PB_URL: where PocketBase is listening. Host and port, no trailing slash.
  // In this edition bin/start binds PocketBase to 127.0.0.1:8090, so the
  // dashboard works on the machine that runs the database.
  PB_URL: "http://localhost:8090",

  // FILES_URL: where server/project-files-server.js is listening. Host and
  // port only; the dashboard adds the /api/... part itself. That server
  // provides the project files and folder browser, document uploads, the
  // de-duplicated task list, the Xero snapshot, the log tails, the playbook
  // runs and the Publish action, so those pages are empty without it.
  // Optional: if left out, the dashboard uses http://localhost:8092. Change it
  // if the server runs on another port (it reads the PORT environment
  // variable) or on another machine.
  FILES_URL: "http://localhost:8092",

  // TODOIST_TOKEN: a Todoist personal API token, from Todoist Settings >
  // Integrations > Developer. Treat it like a password: anyone holding it
  // can read and change every task in the account. Normal task edits go
  // through PocketBase and the sync daemon, which uses its own token from
  // .env; this one is only used for direct calls from the page.
  TODOIST_TOKEN: "put-your-todoist-token-here",
};
