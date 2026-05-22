# Phase 14C — Persistent Apps Script setup (one-time)

`sheets_cross_aggregate` calls into a **persistent** Apps Script project
deployed as an API executable. This is a one-time manual step that must
happen before stress test T3, T9, or any live cross-aggregate call.

Why persistent (not disposable per-call): Apps Script projects need a
**human deploy click** before they can be invoked via the Execution API.
A disposable `apps_script_oneshot` pattern would deadlock on the first
call to every new shard. The persistent project trades a one-time setup
ceremony for permanent zero-friction calls.

---

## Prerequisites

- `clasp` installed and logged in (`clasp login`)
- Google account = `egor.titt@gmail.com` (CLAUDE-TEST project owner)

```powershell
npm install -g @google/clasp
clasp login
```

---

## Step 1. Create the project locally

```powershell
cd "D:\Google work\apps_script_src\aggregator"
clasp create --type standalone --title "ChatAgentAggregator"
```

clasp will print:

```
Created new Google Apps Script: https://script.google.com/d/<SCRIPT_ID>/edit
Cloned 0 files.
```

**Copy the SCRIPT_ID** — you'll need it twice below.

## Step 2. Push the source

```powershell
clasp push --force
```

This uploads `Code.gs` and `appsscript.json`.

## Step 3. Deploy as API executable (manual — required)

This is the step that **cannot be automated** — Google requires a human
in the loop for the API-executable-deployment authorization.

1. Open `https://script.google.com/d/<SCRIPT_ID>/edit` in a browser
2. Click **Deploy → New deployment**
3. Click the gear icon → **API executable**
4. **Description:** `Phase 14 aggregator`
5. **Who has access:** `Only myself`
6. Click **Deploy**
7. Authorize when prompted (one-time OAuth consent for the new project's scopes)
8. Click **Done**

## Step 4. Save the script ID

Create `.data/phase14_config.json`:

```json
{
  "aggregator_script_id": "<SCRIPT_ID from step 1>"
}
```

Alternative: set environment variable `PHASE14_AGGREGATOR_SCRIPT_ID=<SCRIPT_ID>`
(takes precedence over the file).

## Step 5. Verify the deploy

```powershell
$env:LIVE_GOOGLE_TESTS = "1"
uv run python scripts/verify_phase14_setup.py
```

Expected output:

```
✓ config found: aggregator_script_id=1xYz...
✓ clasp run ping → {ok: true, version: phase14, runtime: V8, server_time: ...}
✓ ready: sheets_cross_aggregate will work
```

If you see `Error: function not found: ping`, you didn't push the source
(step 2). Run `clasp push --force` from `apps_script_src/aggregator/`.

If you see `Error: API executable not enabled`, the deploy step (step 3)
didn't take. Retry the deploy ceremony.

---

## Future updates to the aggregator

When `Code.gs` changes (e.g. supporting a new `op`):

```powershell
cd "D:\Google work\apps_script_src\aggregator"
clasp push --force
```

The deployment auto-updates — no need to re-deploy unless you change the
`appsscript.json` manifest (e.g. adding new scopes).
