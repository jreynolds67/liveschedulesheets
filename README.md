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
2. For each event column, reads the **event name**, **date**, **start time**,
   and the **PCR** (`CONTROL ROOM` / `PCR` row: `A`–`E` or `PCR A`–`PCR E`),
   mapping that PCR to LSP channels. Hidden columns are read like any other.
3. Creates an LSP recording event starting `lead_in_minutes` before the sheet's
   start time, ending after a `safety_cap_hours` cap (an engineer normally stops
   it manually in LSP first).
4. Keeps events it already created in step with the sheet: a changed game
   time, date, name or PCR **updates** the LSP event (or moves it to the new
   PCR's channels). Once someone changes an event **by hand in LSP**, it is
   **locked** and the tool leaves it alone. See
   [How changes are tracked](#how-changes-are-tracked). It's safe to run
   repeatedly: nothing is duplicated.
5. Sets each event's **Event Name** variable to the event name (e.g.
   `VB STANFORD PITT`) instead of its default (e.g. `Event XX`). See
   [The Event Name variable](#the-event-name-variable).

There is **no composite tab** — the schedule lives across the per-sport tabs and
the service assembles it. Events on a tab with no PCR row (e.g. **Football**)
show in Preview as **no PCR** until one is assigned per event, or the tab can be
left out entirely.

---

## The web UI

Once deployed, open **`http://10.10.251.95`** (the container's own IP —
see [Deploy in Portainer](#deploy-in-portainer)). From there an engineer can:

- Set the **LSP server URL and login**, and **Test connection**.
- **Paste the Google Sheet link.** The sheet is read right away (no Save
  needed): its title and visible tabs are shown, and the tab the link points at
  (`#gid=…`) opens in the row-mapping preview. If Google can't open it, the UI
  names the service-account email to share the sheet with.
- Edit the **PCR → channel mapping**. Each PCR (`A`–`E`) records on
  **every** LSP channel whose name contains its match text as a whole word
  (default `PCR A`, …), so a `PCR A` event is booked on `01 - PCR A PGM
  (x264)`, `01 - PCR A PGM (ProRes422)`, `02 - PCR A CLEAN (x264)`, etc.
  Channels are looked up live every pass, so added or renamed channels are
  picked up automatically; the UI lists which channels each PCR matches.
  Below the mapping, set the **Event name variable** (default `Event Name`).
  **Check channels** lists each PCR channel as **found** or **not found**,
  according to whether its events carry that variable. The *Scheduled in
  Live Schedule Pro* list shows each event's value, in amber when it isn't
  the event name.
- Pick tabs and **check and choose rows** in *Sheet tabs & row mapping*. Every
  visible tab has a button; the selected one has an **Include this tab**
  toggle and is shown as a grid, with the detected Event / Date / Start / PCR
  rows highlighted and a **Parses as** line above every column showing the
  resulting PCR and start (or
  why that column won't schedule). To correct a row, use its dropdown (Auto,
  No row, or any labelled row), or click a row in the grid and choose what it is
  for. Rows detected by guesswork, rather than an exact label, are flagged
  **please confirm**.
- Adjust **lead-in**, **record length** (hours), the active window, and the
  event name prefix. Under **Record length by sport**, add a sport code (`MSOC`,
  `VB`, `WSOC`, …) with its own length; it applies to events whose name contains
  that code as a whole word (ignoring case), and the first matching row wins.
  Other events use the default length. Changing a length (or the lead-in) also
  updates events already created in LSP that haven't started and aren't locked.
  Preview shows each event's start and end. (In `config.yaml` these are still
  `safety_cap_hours` / `sport_safety_caps`.)
- Toggle **Dry run** and set the **sync interval** (60 s or more): how often
  the tool reads the sheet and pushes changes to LSP (one sync does both). Dry
  run is **on** until you switch it off in the UI (the switch saves immediately,
  on its own, and asks for confirmation before going live). While it's on, a
  *DRY RUN* badge shows in the header, and passes and **Run now** only report
  what they *would* create. The only things that change LSP while it's on are
  the explicit one-off actions: Preview's **SEND TO LSP** and the delete
  buttons. It's stored as `runtime.live` (default `false`) and only the switch
  (`POST /api/dry-run` `{dry_run: true|false}`) changes it: Save, Preview and the
  other buttons never do, and every open page shows the saved state, so a page
  left open with an old setting can't flip it back. A `DRY_RUN` env var, if
  set, overrides the toggle.
- **Preview events** — see exactly what the next sync would do, with no changes
  made: **create**, **update** (the sheet changed), **exists** (up to date),
  **locked** (changed by hand in LSP), **not in sheet** (tracked, but gone from
  the sheet), out of window, or no channel. Each row has an inline **PCR
  selector** and an **Ignore** checkbox for fixing individual events; locked
  rows have an **UNLOCK** button.
- **Send one event to LSP** — rows that would be created or updated have a
  **SEND TO LSP** button that applies just that event now, on all its channels,
  so you can test one at a time. It works **even with dry run on** (the
  scheduled sync stays dry), is recorded in `state.json` like any sync (so a
  later pass won't repeat it), and can be removed with the cleanup button.
  Also available as `POST /api/send-event` with
  `{source_tab, event_date, event_name, occurrence}` from a preview row.
- **Event fixes** — the PCR you pick or the **Ignore** you tick on a Preview
  row is saved as a per-event fix (matched by tab + date + event name) and
  applied on top of the sheet on every sync. The *Event fixes* card lists them;
  remove one to go back to what the sheet says. Fixes for games more than a
  day in the past are dropped on the next Save, as are tab settings for tabs
  the sheet no longer has. (Stored as `event_overrides`.)
- **Run now**, and see the **last-run status** (the *Status* card at the top).
- **See what's scheduled in LSP** — the *Scheduled in Live Schedule Pro* card
  lists, live from LSP, the upcoming and in-progress events on the mapped PCR
  channels (optionally the last 24 h / 7 days too). Events this tool created are
  tagged, with a filter to show only those, and it warns about tool-created
  events in the shown time range that have disappeared from LSP.
- **Delete an event** — each row of that card has a **DELETE** button that
  removes the event from LSP on all its channels (any event, with a
  confirmation) and stops tracking it. If it's still in the sheet the next sync
  creates it again, so tick **Ignore** on it in Preview to keep it out.
- **Clean up test events** — the same card can **delete just the upcoming
  events this tool created** from LSP in one click. Events already in LSP (or
  created by hand), events that have started (in progress or past), and locked
  events are never touched.

Everything is saved to `config.yaml` on the `/data` volume; the background loop
picks up changes automatically. If the settings were saved from another
browser window since this page loaded them, a save is refused (the page offers
to reload them) instead of overwriting the other window's changes: the page
sends the `_version` it loaded with `POST /api/config`, and gets `409` if it's
out of date. Scripts that send no `_version` just save.

**Protect the UI with a password**: set `UI_USER` / `UI_PASSWORD` (HTTP Basic
auth). Without one, anyone who can reach the page can change settings and
delete LSP events; the Status card and the log warn about it. Changes (POSTs)
coming from another site's web page are refused either way, so a page
elsewhere on the LAN can't drive the API through someone's browser; scripts and
tools such as curl or Companion, which send no `Origin` header, can still call
it.

---

## How events are read

Each sport tab is **transposed**: labels live in column A, and **each event is a
column**. For each tab the service works out which row holds each field, in this
order (a row used by an earlier step is never reused):

1. **Your pick** from the row-mapping preview (stored as `tab_overrides.<tab>.rows`).
2. **Exact label**: column-A text matches one of the labels below (editable
   under *Advanced* in the UI).
3. **Header keyword**: e.g. `KICKOFF` or `Tip` for the start time, `Ctrl Room` for
   the PCR, `Event Name` / `Event Date`. Headers containing `CALL`, `CHECK`,
   `DOORS`, `END`, `MEAL` or `CREW` are never taken as the start time.
4. **Cell contents**: dates only (a row that is mostly `9/5`-style dates), and
   PCRs only (a row that is mostly `A`–`E` / `PCR A`–`PCR E`). The start **time** is never
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
| PCR          | `CONTROL ROOM`, `PCR`               | for a channel | `A`–`E` or `PCR A`–`PCR E` (also accepts `Control Room B`). The **first** matching row wins — a repeated block lower down (e.g. a scoreboard feed) is ignored. |
| Date         | `DATE`                              | yes      | Must include the year |
| Start time   | `GAME START`, `GAME TIME`, `START TIME` | yes   | Date and time are always separate rows |

A column becomes a recording once it has an **event name** and a **valid start**
(`DATE` + a game time). **Dates must include the year** (e.g. `9/12/2026`) —
a date without one is skipped and flagged “Date has no year” in the sheet
preview. **Times need AM/PM** unless they're unambiguous 24-hour times
(`17:30`, `0:30`, or with seconds like `9:00:00`, as Sheets formats time
cells): a bare `7:00` could be morning or evening, so it's skipped and flagged
“Start time has no AM/PM” instead of guessed. In a cell with notes, the first
time is used, taking a later AM/PM if it has none (`6:30 & 9:00 PM` → 6:30
PM); `NOON` counts as PM. Blank cells or `TBD`/`TBA` are skipped. If its
PCR is blank the event still shows in Preview flagged **no PCR**, so
an engineer can assign one via the inline selector (saved as an event fix).

Same-named games on the same day (doubleheaders) are numbered in column order
(the `occurrence` in event fixes), counting every column with that name and
date, even one whose time is still TBD, so game 2 keeps its number and its
fixes when game 1's time is filled in.

**PCRs are letters `A`–`E` only** (bare or as `PCR A`). A stray value that isn't a letter
(e.g. an old `CR3`) is left unassigned rather than guessed — fix it in the sheet
or with the PCR selector in Preview.

**Which tabs are read:** all **visible** tabs by default (hidden tabs skipped).
Leave `sheet.tabs` empty to auto-discover, or list specific tabs as an
allow-list. Turn individual tabs off in the UI (**Include this tab** in the
*Sheet tabs & row mapping* card).

---

## The Event Name variable

Each PCR channel's Vantage workflow has a text variable named **Event Name**
(`lsp.event_name_variable`). The channel stores it with no value and a default
(e.g. `Event XX`). When an event is created, LSP copies the channel's
variables into it, so a new event starts out holding that default. The tool
replaces it with the event name from the sheet (the `EVENT` cell, plus any
event name prefix), the same on every channel. Vantage adds the channel,
date and time itself.

After creating an event, the tool reads it back from LSP. It uses the id in
LSP's `AddEvent` reply, or, if the reply has none, finds the new event on its
channel by name and start. It then sets the variable in the event's workflow
variables (`Customization.Conditions`, value in `ConditionValue.Text`; an
empty value means the default) with one `PATCH /api/v1/PatchEvent/{id}`, and
reads the event back (`GetFilteredEvents`) to check LSP kept it. An event
updated from the sheet gets the same. An event the tool created earlier that
still holds the default shows in Preview as **update** (`Will update Event
Name`) and is fixed on the next sync. Each booking is tried once per name
(`variable_sent` in `state.json`), so a value LSP won't keep isn't re-sent
every pass; the log says `LSP didn't keep …` if that happens.

This happens only when a sync is live; with dry run on, Preview's **SEND TO
LSP** does it for that one event. **Check channels** reads each PCR
channel's newest events. It shows **not found** if they don't carry the
variable, listing the variables they do have. Events are scheduled either
way. Leave the setting blank to turn this off.

Also available as `GET /api/event-name-channels`. To see exactly what LSP
holds for an event, `GET /api/lsp-event/<event id>` returns it as LSP's API
does (read-only).

---

## One-time setup

### 1. Google service account (to read the sheet)

1. In the [Google Cloud Console](https://console.cloud.google.com/), pick/create
   a project and **enable the Google Sheets API**.
2. **APIs & Services → Credentials → Create credentials → Service account.**
3. Open it → **Keys → Add key → JSON**. Download the file.
4. In the web UI's **Google Sheet** card, click **Upload key (.json)** and pick
   the file. It's checked, saved on the `lss_state` volume (owner-only
   permissions) and used straight away; the card then shows the service
   account's email. *(Alternative: put it on the Docker host as
   `/opt/liveschedulesheets/service-account.json`, or in the folder named by
   the `GOOGLE_KEY_DIR` stack env var. An uploaded key takes precedence.)*
5. Copy the service account's email (`…@….iam.gserviceaccount.com`) and **share
   the Google Sheet with that email as a Viewer**.

### 2. Everything else — in the web UI

LSP URL/login, PCR mapping, timings, and toggles are all set in the UI after the
container is running. You can optionally pre-seed the LSP login with the
`LSP_USERNAME` / `LSP_PASSWORD` env vars instead.

The LSP base URL is the server's web address including its port (e.g.
`http://10.10.71.32:6500` — the same place its `/swagger` page is served). The
login is **optional**: the client first tries API calls with no login (LSP's
*Basic* auth provider can allow that), then a token from `/api/v1/auth/login`,
then HTTP Basic auth with the username & password. **Test connection** reports
which one worked. It only falls back to HTTP Basic when the login endpoint
actually refuses the login; if the endpoint is down or erroring (e.g. LSP
restarting), the next request simply tries again.

The password is only ever sent to the server it was entered for. Changing the
LSP URL without typing the password again clears it (the UI says so), and an
`LSP_PASSWORD` env var stops being used too, so nobody who can reach the UI
can point it at their own server to collect the login. Replacing the example
config's placeholder URL the first time is exempt.

---

## Deploy in Portainer

**Option A — Git repository stack (recommended):**

1. Push this project to a Git repo Portainer can reach.
2. Portainer → **Stacks → Add stack → Repository**, pointing at `docker-compose.yml`.
3. Under **Environment variables**, optionally set `LSP_USERNAME` / `LSP_PASSWORD`
   and `UI_USER` / `UI_PASSWORD`.
4. Deploy, then upload the Google key from the UI (**Google Sheet → Upload
   key**). Until then the sheet features report the key is missing, but the LSP
   side (Test connection, channels, the *Scheduled in Live Schedule Pro* card,
   cleanup) still works. To mount it from the host instead, put it at
   `/opt/liveschedulesheets/service-account.json` or set `GOOGLE_KEY_DIR`; a
   relative path like `./secrets` doesn't work for Git stacks, because
   Portainer checks out the repo in its own data folder. **Never commit a real
   key to a shared repo.**
5. Deploy, then open `http://10.10.251.95` and finish configuration in the UI.

**Health, logs and shutdown:** the image has a `HEALTHCHECK` (`GET /healthz`,
no login needed) that marks the container unhealthy if the sync loop has died
or a pass has been stuck for over half an hour past its interval; Portainer
shows it. (Plain Docker doesn't restart unhealthy containers by itself.) The
stack caps the container log at 5 × 10 MB. On stop or redeploy, the app
finishes the LSP write in progress, saves `state.json` and exits (it waits up
to 45 s; the stack's `stop_grace_period` is 60 s).

**Networking:** the container joins the existing **Companion** `ipvlan`
network (parent `eth0`, subnet `10.10.251.0/24`) as an external network, at a
fixed IP of **10.10.251.95** — no host port mapping. The stack sets
`WEB_PORT=80` so the UI URL needs no port (the image default is 8080). Docker allows only one
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

The `lss_state` named volume persists the live `config.yaml`, the tracking
`state.json`, and an uploaded Google key across restarts and redeploys.

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

Turn on **Dry run** in the UI (or `-e DRY_RUN=true`, which overrides the UI) to
log what would be created without the sync touching LSP. Preview's **Send to
LSP** button and the cleanup button still act on LSP, so you can test one
event at a time and clean up afterwards. Headless single pass (cron/testing):

```bash
docker run --rm -e RUN_ONCE=true -e DRY_RUN=true ... liveschedulesheets \
  python -m app.main
```

### Unit tests

The sheet parser, the sync logic (against an in-memory LSP) and the manager
have a pytest suite in `tests/`; no Google key or LSP server is needed:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

### Updating dependencies

`requirements.in` lists the direct dependencies; `requirements.txt` pins them
*and everything they pull in*, so every Portainer rebuild installs exactly
the same packages. To upgrade, edit the versions in `requirements.in`, then
regenerate `requirements.txt` for the image's platform (Linux, Python 3.12):

```bash
pip install --dry-run --ignore-installed --report report.json \
  --python-version 3.12 --platform manylinux_2_17_x86_64 --only-binary=:all: \
  -r requirements.in
python -c "import json; [print(f\"{i['metadata']['name']}=={i['metadata']['version']}\") for i in sorted(json.load(open('report.json'))['install'], key=lambda i: i['metadata']['name'].lower())]"
```

Paste that list under the header comment in `requirements.txt`, and run the
tests.

---

## How changes are tracked

At the start of every pass (and every preview) the tool fetches LSP's current
channel list, resolves each PCR's channels, and reads the events on them. Each
sheet event it schedules is tracked in `state.json` (on the `lss_state` volume)
as a **record**: where it came from (tab, column, name, date), and one
**booking** per LSP channel with a snapshot of the LSP event (name, start, end,
channel) as LSP reported it right after the tool last wrote it.

**Following the sheet.** Each pass matches sheet events to records:

1. same tab + date + event name (+ which same-named game that day): a changed
   **start time** or **PCR**;
2. otherwise the same tab + name, when only one unmatched event and one
   unmatched record share it: a changed **date**;
3. otherwise the same tab + column + date, likewise: a changed **name**.

Steps 2–3 only match events that haven't started, so a finished game never
swallows a later rematch. For a matched event that hasn't started, the tool
updates the LSP event's name/start/end in place (`PatchEvent`), and when the PCR
changes it removes the bookings on the old PCR's channels and creates them on
the new ones. A new channel that matches the PCR gets a booking on the next
pass. Lead-in and safety-cap changes are applied the same way. Once an event
has started, sheet changes are no longer applied to it (and it's no longer
checked for hand edits, since LSP may change a recording's end itself when it's
stopped).

**Suspicious changes are held.** If the sheet moves a tracked event outside
the active window (into the past, or beyond `horizon_days`, usually a typo in
the year), LSP is left as it was and Preview shows it as **out of window** with
the date the sheet now gives. Fixing the sheet brings it back in step.

**Channels are only removed when the PCR changes.** When a tracked event's PCR
changes, its bookings on the old PCR's channels are removed (retried next pass
if a removal fails). A booking whose channel merely stops matching the same
PCR (renamed in LSP, or the channel match edited) is left in LSP.

**Locking.** Before changing anything, the tool compares each booking with its
snapshot and with the values it last sent. If the name, start or end match
neither, or the event was deleted or moved to another channel, **someone changed
it in LSP**, and the whole sheet event is
**locked**: the tool never updates, re-creates or deletes it again, and the
cleanup button skips it. Locks are recorded even in dry run. Preview shows
locked events with the reason and an **Unlock** button (`POST /api/unlock`
`{record_id}`); unlocking takes LSP's current values as the new baseline and
forgets bookings deleted in LSP, so the next pass puts the sheet's values back
and re-creates those bookings.

**Gone from the sheet.** A tracked event that no longer appears in the sheet
(column removed, date made TBD, tab turned off, ignored) shows in Preview as
**not in sheet** and is **left in LSP**. Nothing is deleted automatically,
because a misread tab would otherwise wipe real bookings. Delete it in LSP if
it's cancelled.

**New events.** A sheet event with no record is created on each of its PCR's
channels, unless LSP already has an event with the same name and start minute
there (made by hand). That one is recorded but never modified or deleted.

Matching the values last sent covers LSP returning the old values on the
re-read right after an update. If LSP gives an event a new id when it's updated,
the tool finds it by those values on the same channel and follows the new id.

**Old records are pruned.** LSP deletes events some days after they end (its
*Event cleanup threshold*, read each pass from `GET /api/v1/settings/general`
→ `EventCleanupThresholdInDays`). After each pass the tool forgets records
whose events ended longer ago than that, so `state.json` doesn't grow
forever. If the setting can't be read (the login needs `lsp-config-read`), it
keeps records for 365 days.

`state.json` is saved after every change the tool makes in LSP, not just at the
end of a pass, so a restart mid-pass can't leave events in LSP that the tool
has no record of. If `state.json` isn't valid JSON when the tool starts, it's
renamed to `state.json.corrupt-<time>` (kept for inspection) and the Status
card says so; one that can't be read at all stops the sync with an error
rather than being overwritten. If `state.json` is lost, the tool falls back to
matching LSP events by channel + name + start minute, so unchanged events
aren't duplicated (they are then treated as made by hand and no longer follow
the sheet). Events edited or deleted in LSP would be created again, though, so
keep the volume.

**Errors show in the Status card.** If the Google Sheet can't be opened (unshared,
key revoked, Google down), the pass fails with that error instead of reading as
an empty sheet; a single tab that can't be read is skipped (its events show as
**not in sheet**, which deletes nothing) and counted as an error. A request from
the UI that would have to wait more than a few seconds for a running sync pass
(Preview, the channel lists) says so instead of hanging; Run now and the other
actions wait up to two minutes.

### Deleting tool-created events (testing)

The *Delete tool-created events* button on the UI's *Scheduled in Live Schedule
Pro* card (and `POST /api/delete-created`)
removes **only** the events this tool created that haven't started — read from
the tool-created bookings in `state.json` and deleted via
`DELETE /api/v1/RemoveEvent`. Events in progress or past, and locked ones, are
skipped (the reply counts them as `skipped_started` / `skipped_locked`). After a
delete they are forgotten from state, so a later pass will re-create them. This
lets you iterate during testing without wiping hand-made events in LSP. It works
with dry run on too, to clean up events sent one at a time from Preview.

A row's **DELETE** button calls `POST /api/delete-events` with
`{event_ids: [...]}` (the event's id on each channel); it works on any event and
also forgets it from `state.json`.

The card's event list comes from `GET /api/scheduled?past_days=N`, which calls
`GetEvents` for each mapped PCR channel and marks events whose id matches a
tool-created entry in `state.json`.

## Configuration reference

The UI covers everything; `config.example.yaml` documents every field inline
(it seeds the live config on first run). Highlights: `lsp.event_name_variable`, `scheduling.lead_in_minutes`,
`scheduling.safety_cap_hours`, `scheduling.horizon_days` / `past_grace_minutes`,
`runtime.poll_interval_seconds`, `runtime.live`. Multi-tab settings:
`sheet.tabs` (empty = auto-discover visible tabs), `tab_overrides`
(enable/disable a tab, pinned `rows`), and `event_overrides` (per-event
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
  runtime.py     # logging + local time zone (TZ) shared by both entry points
  config.py      # config model, parse/validate, load/save
  settings_store.py # live config on /data, seeded from the example
  sheets.py      # Google fetch + pure parse_grid() / locate_rows() / inspect_grid()
  lsp_client.py  # Live Schedule Pro API client (auth, channels, events)
  sync.py        # plan() (read-only) + run_once() (creates / updates / locks)
  state.py       # tracked events, their LSP bookings and snapshots
  models.py      # ScheduledEvent
tests/           # pytest suite (pure parsing + sync against a fake LSP)
requirements.in  # direct dependencies; requirements.txt is the full pinned set
config.example.yaml   # seed / documented defaults
Dockerfile
docker-compose.yml
docs/lsp-swagger.json  # LSP API spec (reference)
```
