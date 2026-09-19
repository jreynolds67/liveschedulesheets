# LiveScheduleSheets

Reads a Google Sheet crew/schedule tab and automatically creates recording
events in **Telestream Live Schedule Pro (LSP)**. Ships with a **web UI** so an
engineer can configure the mapping, timings, and toggles without touching YAML.
Runs as a single Docker container (built for Portainer).

Every `poll_interval` (default 15 min) it:

1. Reads your **composite** tab (one event per column, row headers down column A).
2. For each column with an **event name**, a **PCR** letter, and a **start time**,
   maps the PCR (A/B/C/D/E/V) to an LSP channel.
3. Creates an LSP recording event starting `lead_in_minutes` before the sheet's
   start time, ending after a `safety_cap_hours` cap (an engineer normally stops
   it manually in LSP first).
4. Skips events that already exist (checked against LSP and a local state file),
   so it is safe to run repeatedly.

Football is intentionally not included — only the composite tab is read.

---

## The web UI

Once deployed, open **`http://<docker-host>:8080`**. From there an engineer can:

- Set the **LSP server URL and login**, and **Test connection**.
- Edit the **PCR → channel mapping** (channel names auto-complete from the live
  channel list after a successful connection test).
- Adjust **lead-in**, **safety-cap hours**, the active window, and the event
  name prefix.
- Toggle **Dry run** and the **sync interval**.
- **Preview events** — see exactly what the next sync would create/skip, with no
  changes made.
- **Run now**, and see the **last-run status**.

Everything is saved to `config.yaml` on the `/data` volume; the background loop
picks up changes automatically. (Optional: protect the UI with HTTP Basic auth
by setting `UI_USER` / `UI_PASSWORD`.)

---

## How events are read

The sheet is **transposed**: labels live in column A, and **each event is a
column**. The service finds these label rows (editable under *Advanced* in the UI):

| Field        | Default label(s)              | Required | Notes |
|--------------|-------------------------------|----------|-------|
| Event name   | `EVENT`                       | yes      | Used as the LSP event name |
| PCR / room   | `PCR`, `CONTROL ROOM`         | yes      | `A`/`B`/`C`/`D`/`E`/`V` (also accepts `PCR A`, `Control Room B`) |
| Start        | `START`, `GAME START`         | one of   | **Recommended:** one cell with full date + time **including year** |
| Date + Time  | `DATE` + `GAME TIME`/`START TIME` | one of | Fallback if kept in separate rows |

A column becomes a recording only once it has **event name + PCR + a valid
start**. Blank cells or `TBD`/`TBA` are skipped — so an event is scheduled the
moment an engineer assigns it a PCR.

**Recommended composite-tab layout:** a `START` row with an explicit date-time
including the year, e.g. `2026-09-12 15:30` or `9/12/2026 3:30 PM`. That removes
all year guessing. (Bare `9/12` dates fall back to academic-year inference.)

---

## One-time setup

### 1. Google service account (to read the sheet)

1. In the [Google Cloud Console](https://console.cloud.google.com/), pick/create
   a project and **enable the Google Sheets API**.
2. **APIs & Services → Credentials → Create credentials → Service account.**
3. Open it → **Keys → Add key → JSON**. Download the file.
4. Save it as `secrets/service-account.json` in this project (or place it on the
   Docker host and mount it — see compose).
5. Copy the service account's email (`…@….iam.gserviceaccount.com`) and **share
   the Google Sheet with that email as a Viewer**.

### 2. Everything else — in the web UI

LSP URL/login, PCR mapping, timings, and toggles are all set in the UI after the
container is running. You can optionally pre-seed the LSP login with the
`LSP_USERNAME` / `LSP_PASSWORD` env vars instead.

---

## Deploy in Portainer

**Option A — Git repository stack (recommended):**

1. Push this project to a Git repo Portainer can reach.
2. Portainer → **Stacks → Add stack → Repository**, pointing at `docker-compose.yml`.
3. Under **Environment variables**, optionally set `LSP_USERNAME` / `LSP_PASSWORD`
   and `UI_USER` / `UI_PASSWORD`.
4. Ensure the Google key is available at the mounted path
   (`secrets/service-account.json` in the checkout, or an absolute host path you
   set in the compose volume). **Never commit a real key to a shared repo.**
5. Deploy, then open `http://<host>:8080` and finish configuration in the UI.

**Option B — Build & push an image:**

```bash
docker build -t your-registry/liveschedulesheets:latest .
docker push your-registry/liveschedulesheets:latest
```

Then set `image:` in `docker-compose.yml` and deploy the stack.

The `lss_state` named volume persists **both** the live `config.yaml` and the
de-dup `state.json` across restarts.

---

## Test locally

```bash
docker build -t liveschedulesheets .
docker run --rm -p 8080:8080 \
  -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/service-account.json \
  -v "$PWD/secrets/service-account.json:/secrets/service-account.json:ro" \
  -v lss_state:/data \
  liveschedulesheets
# open http://localhost:8080, configure, hit "Preview events" (creates nothing)
```

Turn on **Dry run** in the UI (or `-e DRY_RUN=true`) to log what would be created
without touching LSP. Headless single pass (cron/testing):

```bash
docker run --rm -e RUN_ONCE=true -e DRY_RUN=true ... liveschedulesheets \
  python -m app.main
```

---

## How de-duplication works

Each event is matched by **channel + name + start minute (UTC)**:
- The local `state.json` (on the `lss_state` volume) records what was created.
- LSP is queried per channel each pass and is the source of truth, so duplicates
  are avoided even if the state file is lost. Editing an event's **name or start
  time** in the sheet creates a *new* LSP event; delete the old one in LSP if
  needed.

## Configuration reference

The UI covers everything; `config.example.yaml` documents every field inline
(it seeds the live config on first run). Highlights: `scheduling.lead_in_minutes`,
`scheduling.safety_cap_hours`, `scheduling.horizon_days` / `past_grace_minutes`,
`runtime.poll_interval_seconds`, `runtime.dry_run`.

## Project layout

```
app/
  webui.py       # Flask web UI + JSON API (default container entrypoint)
  web/index.html # the configuration page (single file, no build step)
  manager.py     # background sync loop + operations the UI calls
  main.py        # headless runner (RUN_ONCE / cron)
  config.py      # config model, parse/validate, load/save
  settings_store.py # live config on /data, seeded from the example
  sheets.py      # Google Sheets read + transposed-column parsing
  lsp_client.py  # Live Schedule Pro API client (auth, channels, events)
  sync.py        # plan() (read-only) + run_once() (creates)
  state.py       # local created-event cache
  models.py      # ScheduledEvent
config.example.yaml   # seed / documented defaults
Dockerfile
docker-compose.yml
swagger.json     # LSP API spec (reference)
```
