# The hosted read-only dashboard

The dashboard normally runs on the same machine as the database. The
hosted dashboard is a read-only copy of it, published as a static website
on Cloudflare Pages, so it can be opened from a phone or another computer
without exposing PocketBase to the internet. There is no backend: nothing
on the hosted page can change anything.

## How it works

The local dashboard reads three live sources: PocketBase on :8090, the
files server on :8092, and (for a few actions) the Todoist API. None of
those exist on a static website, so the hosted copy is built from a frozen
snapshot of them.

1. **Build.** `tools/snapshot_build.py` reads every PocketBase collection
   the dashboard uses and every files-server response the dashboard asks
   for, and writes them into one file, `dist/snapshot.json`. It copies the
   same `index.html` and engine scripts beside it and writes a hosted
   `config.local.js` that sets `SNAPSHOT_URL` and an empty Todoist token.
2. **Serve reads from the snapshot.** `dashboard/snapshot_shim.js` wraps
   the browser's `fetch`. When `SNAPSHOT_URL` is set, it answers every
   read from `snapshot.json` (it contains a small filter, sort and paging
   engine that imitates the PocketBase API), refuses every write with a
   message on screen, and blocks all Todoist calls. When `SNAPSHOT_URL`
   is not set, which is always the case locally, it does nothing.
3. **Publish.** `bin/publish-dashboard` runs the build and uploads `dist/`
   with Cloudflare's `wrangler` tool.

Because the shim is what changes behaviour, there is only one `index.html`
and the hosted copy cannot drift from the local one.

## Safety checks in the publish script

- It refuses to upload if a Todoist token appears anywhere in the bundle.
- Only one publish can run at a time (a pid lock in `state/publish.lock`).
- Every run is recorded as a row in the `publish_log` collection: when it
  ran, what started it, whether it worked, how many records went up and
  the tail of its output. The dashboard's Publish page shows these rows.
  Recording is best effort: if PocketBase is down the upload still happens
  and the script says the row was skipped.
- The Publish page (and the `POST /api/publish` route behind it) only
  exists on the local dashboard, and the route only accepts requests from
  this machine.

## First-time setup

1. Create a free Cloudflare account. No domain is needed; the site gets a
   `*.pages.dev` address.
2. Copy your **Account ID** from the Cloudflare dashboard, and create an
   **API token** with one permission: Account, Cloudflare Pages, Edit.
3. Add them to `.env`:

   ```
   CLOUDFLARE_API_TOKEN=...
   CLOUDFLARE_ACCOUNT_ID=...
   CF_PAGES_PROJECT=splatt-ops-demo      # optional, this is the default
   ```

4. Install the Node tools and create the Pages project once:

   ```bash
   npm install
   npx wrangler pages project create splatt-ops-demo --production-branch=main
   ```

5. **Lock it down before the first publish** (next section).
6. Publish:

   ```bash
   bin/publish-dashboard               # build and upload
   bin/publish-dashboard --build-only  # build dist/ only, to inspect it first
   ```

## Locking it down

The snapshot contains every client, contact, quote and financial record.
A `pages.dev` address is public by default, and an address that is hard to
guess is not access control. Put it behind Cloudflare Access:

1. In the Cloudflare dashboard open **Zero Trust** (the free plan covers
   up to 50 users).
2. **Access, Applications, Add an application, Self-hosted.** Add two
   hostnames so preview deployments are covered too:
   `<project>.pages.dev` and `*.<project>.pages.dev`.
3. Keep **One-time PIN** as the login method (it emails a code).
4. Add a policy that allows a list of email addresses.

Everyone not on that list then sees a login page instead of the data.

## Keeping it current

The snapshot is only as fresh as the last publish. To publish on a
schedule, add a cron entry on the machine that runs PocketBase, for
example every hour from 7am to 6pm on weekdays:

```
15 7-18 * * 1-5 /path/to/splatt-ops/bin/publish-dashboard --trigger cron
```

The script writes its own log to `logs/publish-dashboard.log`.

## Adding a dashboard page later

If a new page reads a PocketBase collection that is not listed in
`COLLECTIONS` in `tools/snapshot_build.py`, that page will be empty on the
hosted copy; add the collection there. The same goes for a new files-server
endpoint and `FIXED_ENDPOINTS`. If a new engine script is added to
`index.html`, add it to `STATIC_FILES`; `tests/test_publish.py` fails if a
local `<script src>` is missing from that list. `node tests/test_snapshot_shim.js`
covers the shim's query engine and write blocking.
