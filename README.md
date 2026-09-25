# LiveScheduleSheets

Reads a Google Sheet crew/schedule tab and automatically creates recording
events in **Telestream Live Schedule Pro (LSP)**. Ships with a **web UI** so an
engineer can configure the mapping, timings, and toggles without touching YAML.
Runs as a single Docker container (built for Portainer).

Every `poll_interval` (default 15 min) it:

1. Reads every **visible** sport tab (Football, Fall/Winter/Spring Olympic,
   Basketball, Special Events) — each transposed, one event per column, row
   headers down column A. **Hidden tabs are skipped automatically** (so the
   `COUNT` tab and the old `*RELAYOUT` composites are ignored with no config).
2. For each event column, reads the **event name**, **start time**, and the
   **`CONTROL ROOM`** letter (`A`–`E`), mapping that room to an LSP channel.
3. Creates an LSP recording event starting `lead_in_minutes` before the sheet's
   start time, ending after a `safety_cap_hours` cap (an engineer normally stops
   it manually in LSP first).
4. Skips events that already exist (checked against LSP and a local state file),
   so it is safe to run repeatedly.

There is **no composite tab** — the schedule lives across the per-sport tabs and
the service assembles it. Tabs with no `CONTROL ROOM` row (e.g. **Football**) get
their channel from a per-tab **default control room** set in the UI, or they can
be toggled off entirely.

---

## The web UI

Once deployed, open **`http://10.10.251.95:8080`** (the container's own IP —
see [Deploy in Portainer](#deploy-in-portainer)). From there an engineer can:

- Set the **LSP server URL and login**, and **Test connection**.
- **Paste the Google Sheet link.** The sheet is read right away (no Save
  needed): its title and visible tabs are shown, and the tab the link points at
  (`#gid=…`) opens in the row-mapping preview. If Google can't open it, the UI
  names the service-account email to share the sheet with.
- Edit the **control room → channel mapping** (`A`–`E`; channel names
  auto-complete from the live channel list after a successful connection test).
- Manage **Sheet tabs** — every visible tab is listed with an on/off toggle and
  a **default control room** selector (used for tabs without a `CONTROL ROOM`
  row, e.g. Football), plus a **Rows…** button that opens it in the preview.
- **Check and choose rows** in *Sheet preview & row mapping*. Each tab is shown as
  a grid, with the detected Event / Date / Start / Room rows highlighted and a
  **Parses as** line above every column showing the resulting start and room (or
  why that column won't schedule). To correct a row, use its dropdown (Auto,
  No row, or any labelled row), or click a row in the grid and choose what it is
  for. Rows detected by guesswork, rather than an exact label, are flagged
  **please confirm**.
- Adjust **lead-in**, **safety-cap hours**, the active window, and the event
  name prefix.
- Toggle **Dry run** and the **sync interval**.
- **Preview events** — see exactly what the next sync would create/skip, with no
  changes made. Each row has an inline **room selector** and an **Ignore**
  checkbox for fixing individual events.
- Stage **Manual overrides** — per-event fixes (matched by tab + date + event
  name): assign/correct the control room, override the start time, or ignore an
  event. They persist in the overrides list until removed.
- **Run now**, and see the **last-run status**.
- **Clean up test events** — the *Events created by this tool* card lists every
  event this tool created and can **delete just those** from LSP in one click.
  Events already in LSP (or created by hand) are never touched.

Everything is saved to `config.yaml` on the `/data` volume; the background loop
picks up changes automatically. (Optional: protect the UI with HTTP Basic auth
by setting `UI_USER` / `UI_PASSWORD`.)

---

## How events are read

Each sport tab is **transposed**: labels live in column A, and **each event is a
column**. For each tab the service works out which row holds each field, in this
order (a row used by an earlier step is never reused):

1. **Your pick** from the row-mapping preview (stored as `tab_overrides.<tab>.rows`).
2. **Exact label**: column-A text matches one of the labels below (editable
   under *Advanced* in the UI).
3. **Header keyword**: e.g. `KICKOFF` or `Tip` for the start time, `Ctrl Room` for
   the room, `Event Name` / `Event Date`. Headers containing `CALL`, `CHECK`,
   `DOORS`, `END`, `MEAL` or `CREW` are never taken as the start time.
4. **Cell contents**: dates only (a row that is mostly `9/5`-style dates), and
   control rooms only (a row that is mostly `A`–`E`). The start **time** is never
   guessed from contents, because `CREW CALL`, `AUDIO CHECK` and `DOORS` rows hold
   times too.

A pick is stored with the row number *and* its column-A label, so it survives
rows being inserted or deleted above it. It follows the label to the nearest
row with that label within 15 rows. If the label is gone, it keeps the row
number and is flagged in the UI and the log.

The default labels:

| Field        | Default label(s)                    | Required | Notes |
|--------------|-------------------------------------|----------|-------|
| Event name   | `EVENT`                             | yes      | Used as the LSP event name |
| Control room | `CONTROL ROOM`, `PCR`               | for a channel | Letters `A`–`E` (also accepts `PCR A`, `Control Room B`). The **first** matching row wins — a repeated block lower down (e.g. a scoreboard feed) is ignored. |
| Date + Time  | `DATE` + `GAME START`/`GAME TIME`/`START TIME` | one of | These tabs keep date and time in separate rows |
| Start        | *(combined)* `datetime`             | one of   | Optional: a single cell with full date + time including year |

A column becomes a recording once it has an **event name** and a **valid start**
(`DATE` + a game time). Blank cells or `TBD`/`TBA` are skipped. If its
`CONTROL ROOM` is blank the event still shows in Preview flagged **no room**, so
an engineer can assign one via the inline selector or a manual override.

**Control rooms are letters `A`–`E` only.** A stray value that isn't a letter
(e.g. an old `CR3`) is left unassigned rather than guessed — fix it in the sheet
or with a manual override.

**Which tabs are read:** all **visible** tabs by default (hidden tabs skipped).
Leave `sheet.tabs` empty to auto-discover, or list specific tabs as an
allow-list. Turn individual tabs off in the UI (**Sheet tabs** card).

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
5. Deploy, then open `http://10.10.251.95:8080` and finish configuration in the UI.

**Networking:** the container joins the existing **Companion** `ipvlan`
network (parent `eth0`, subnet `10.10.251.0/24`) as an external network, at a
fixed IP of **10.10.251.95** — no host port mapping. Docker allows only one
ipvlan network per parent interface, so this stack can't create its own. Set the
`COMPANION_NETWORK` stack env var to that network's full Docker name (see
`docker network ls`) if it isn't `companion_companion_net`. With ipvlan, the
Docker host itself usually can't reach the container's IP; use another machine
on the LAN.

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
  Events this tool creates are tagged `created_by_tool`; events found already in
  LSP are tagged `existed` (and are never deleted by the cleanup below).
- LSP is queried per channel each pass and is the source of truth, so duplicates
  are avoided even if the state file is lost. Editing an event's **name or start
  time** in the sheet creates a *new* LSP event; delete the old one in LSP if
  needed.

### Deleting tool-created events (testing)

The UI's *Events created by this tool* card (and `POST /api/delete-created`)
removes **only** the events this tool created — read from the `created_by_tool`
entries in `state.json` and deleted via `DELETE /api/v1/RemoveEvent`. After a
delete they are forgotten from state, so a later pass will re-create them. This
lets you iterate during testing without wiping hand-made events in LSP.

## Configuration reference

The UI covers everything; `config.example.yaml` documents every field inline
(it seeds the live config on first run). Highlights: `scheduling.lead_in_minutes`,
`scheduling.safety_cap_hours`, `scheduling.horizon_days` / `past_grace_minutes`,
`runtime.poll_interval_seconds`, `runtime.dry_run`. Multi-tab settings:
`sheet.tabs` (empty = auto-discover visible tabs), `tab_overrides`
(enable/disable a tab, `default_control_room`, pinned `rows`), and `event_overrides` (per-event
`control_room` / `start` / `ignore`, matched by tab + date + event name).

## Keeping docs current

**Documentation is part of every change — keep it up to date as you go, in the
same commit as the code.** When behavior, config, the sheet layout, or the UI
changes, update, in the same commit:

- this **README** (flow, the "How events are read" table, the UI list),
- **`config.example.yaml`** (every field is documented inline; it also seeds the
  live config), and
- any code docstrings/comments the change touches.

Treat a change as unfinished until the docs match it.

## Project layout

```
app/
  webui.py       # Flask web UI + JSON API (default container entrypoint)
  web/index.html # the configuration page (single file, no build step)
  manager.py     # background sync loop + operations the UI calls
  main.py        # headless runner (RUN_ONCE / cron)
  config.py      # config model, parse/validate, load/save
  settings_store.py # live config on /data, seeded from the example
  sheets.py      # Google fetch + pure parse_grid() / locate_rows() / inspect_grid()
  lsp_client.py  # Live Schedule Pro API client (auth, channels, events)
  sync.py        # plan() (read-only) + run_once() (creates)
  state.py       # local created-event cache
  models.py      # ScheduledEvent
config.example.yaml   # seed / documented defaults
Dockerfile
docker-compose.yml
swagger.json     # LSP API spec (reference)
```
