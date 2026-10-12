# OCR Daemon — Windows Setup

Background service that listens for a keyboard shortcut, captures
configurable screen regions, runs Tesseract OCR on each, and writes
the results directly into cells of an `.ods` spreadsheet.

---

## Files

| File | Purpose |
|------|---------|
| `ocr_daemon.py` | Main daemon — capture pipeline, hotkeys, GSPro connection, reports, command line |
| `screen_capture.py` | Grabbing GSPro's screen: frames, regions, which monitor it is on |
| `ocr_reader.py` | Reading the HUD regions: detectors, character matching, Tesseract |
| `rebuild_from_screenshots.py` | Re-reads saved screenshots with the current code and rebuilds the report |
| `verify_sides.py` | Checks the dispersion plot's left/right estimate against saved screenshots |
| `config_defaults.py` | The default settings; missing ones in config.json are taken from here |
| `region_selector.py` | Interactive GUI: draw screen regions with mouse |
| `config.json` | All settings: hotkeys, ODS path, regions, cell mappings |
| `run_console.bat` | Launch with a visible console (good for testing) |
| `run_background.bat` | Launch hidden — no window, log goes to file |
| `requirements.txt` | Python dependencies |

---

## Step 1 — Install Tesseract

Download the Windows installer from:
**https://github.com/UB-Mannheim/tesseract/wiki**

Run the installer. The default path is:
```
C:\Program Files\Tesseract-OCR\tesseract.exe
```
That path is already pre-filled in `config.json`. If you installed
to a different folder, update `tesseract_cmd` in `config.json`.

> Optional: tick extra language packs during install if you need
> languages other than English.

---

## Step 2 — Install Python packages

Open a Command Prompt or PowerShell in this folder and run:

```bat
pip install -r requirements.txt
```

---

## Step 3 — Define your capture regions

```bat
python region_selector.py
```

A dark semi-transparent overlay covers the whole screen.

| Action | Effect |
|--------|--------|
| **Left-drag** | Draw a capture region |
| Fill in dialogs | Name · target cell (e.g. `B3`) · sheet · PSM mode · preprocessing |
| **Right-click** | Undo the last region |
| **ESC** | Confirm all regions and save to `config.json` |

---

## Step 4 — Verify (dry run)

Before going live, make sure each region captures the right area
and OCR produces sensible output:

```bat
python ocr_daemon.py --test
```

This prints results to the console and saves a `debug_<Name>.png`
for every region so you can inspect the pre-processed image.
Nothing is written to the ODS file in this mode.

---

## Step 5 — Run the daemon

**With a console window** (you can watch the live log):
```bat
run_console.bat
```

**Completely hidden** (log goes to `ocr_daemon.log`):
```bat
run_background.bat
```

Now press **Ctrl+Shift+F9** any time to trigger OCR.
Results appear instantly in `ocr_output.ods`.

Press **Ctrl+Shift+F10** to stop the daemon
(or kill `pythonw.exe` in Task Manager when running hidden).

---

## Run once (no daemon — useful for scripting)

```bat
python ocr_daemon.py --once
```

Runs the pipeline exactly once and exits. You can call this from a
batch script or Windows Task Scheduler.

---

## config.json reference

```jsonc
{
  "hotkey":         "<ctrl>+<shift>+F9",   // trigger OCR pipeline
  "exit_hotkey":    "<ctrl>+<shift>+F10",  // graceful shutdown
  "ods_file":       "ocr_output.ods",      // path to the target spreadsheet
  "tesseract_cmd":  "C:\\Program Files\\Tesseract-OCR\\tesseract.exe",
  "tesseract_lang": "eng",                // ISO 639-2; "eng+deu" for multiple

  "regions": [
    {
      "name":             "MyField",       // label (logs + debug PNG filename)
      "x": 120, "y": 240,                 // top-left corner in pixels
      "width": 280, "height": 40,         // size of capture area in pixels
      "sheet":            "Sheet1",        // ODS sheet/tab name
      "cell":             "A1",            // target cell (Excel-style)
      "tesseract_config": "--psm 7",       // Tesseract options
      "preprocess":       "auto"           // null | "scale" | "threshold" | "auto"
    }
  ]
}
```

### Hotkey key names

Combine keys with `+`:  `<ctrl>+<shift>+F9`

| Key | Syntax |
|-----|--------|
| Ctrl | `<ctrl>` |
| Shift | `<shift>` |
| Alt | `<alt>` |
| Windows key | `<cmd>` |
| F-keys | `<F9>`, `<F12>` … |
| Letters | `a`, `z` … |

### Tesseract PSM quick reference

| PSM | Best for |
|-----|----------|
| `6` | Uniform block of text (default) |
| `7` | Single text line — labels, invoice fields |
| `8` | Single word |
| `11` | Sparse / scattered text |
| `13` | Raw line (skips layout analysis) |

To restrict characters (e.g. numbers only):
```
"--psm 7 -c tessedit_char_whitelist=0123456789.,"
```

### Preprocessing modes

| Mode | When to use |
|------|-------------|
| `null` | Clean, high-contrast screen text |
| `"scale"` | Small or low-DPI text |
| `"threshold"` | Document scans, high-contrast images |
| `"auto"` | Best all-round starting point |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `TesseractNotFoundError` | Check `tesseract_cmd` path in `config.json` |
| Garbage OCR output | Run `--test`, inspect `debug_<Name>.png`; try `"preprocess": "auto"` |
| Hotkey not detected | Try running as Administrator (right-click → Run as administrator) |
| Wrong cells written | Recheck `x/y/width/height` with `--test`; adjust coordinates |
| ODS file locked | Close the file in LibreOffice / Excel before triggering OCR |

---

## GSPro drops out of fullscreen when the daemon starts

If GSPro switches from fullscreen to windowed when the daemon runs, every
capture region shifts and the OCR breaks. There are three separate causes and
the daemon now addresses all of them, but one setting on your side makes the
problem impossible.

### Do this first: set GSPro to Borderless Windowed

In GSPro's display settings choose **Borderless Windowed** rather than
**Fullscreen** (exclusive). Capture works identically in both modes, but a
borderless window is an ordinary composited window — it has no exclusive
display mode to lose, so nothing can knock it out of one. Frame rate impact on
a modern GPU is negligible.

This is the only fix that is guaranteed, because exclusive fullscreen is
designed to yield the display whenever another process takes focus.

### What the daemon now does automatically

**One capture context per thread.** `_grab_region` previously built a new
`mss.mss()` for every region — 22 Windows device-context create/destroy cycles
per capture, plus several per second once auto-capture polls. That churn
against the desktop DC is a known trigger for the Desktop Window Manager
dropping an exclusive-fullscreen Direct3D app to windowed. The context is now
created once per thread and reused, which is also measurably faster.

**No console flashes from Tesseract.** `pytesseract` spawns `tesseract.exe`
once per region. Without `CREATE_NO_WINDOW` each spawn briefly shows a console,
and a new foreground window steals focus — dropping the game instantly. The
daemon now forces that flag on all child processes.

**DPI awareness declared at startup.** On a scaled display (125%, 150%) a
non-DPI-aware process gets virtualised coordinates, which both misplaces
regions and can provoke a mode change. The daemon now declares per-monitor v2
awareness before any capture happens.

**Launchers no longer steal focus.** `run_background.vbs` uses `pythonw.exe`
and shows no success dialog (a message box is a focus-stealing window like any
other). `run_console.bat` does open a console by necessity — see below.

### Remaining thing to watch

`run_console.bat` opens a visible console, and that takes focus when it appears.
If GSPro is already in exclusive fullscreen, this alone will drop it. Either
start the daemon **before** launching GSPro, use `run_background.vbs`, or use
Borderless Windowed.



---

## Console follows GSPro

The log console can track the GSPro window so it can be watched while playing.
Toggle with **Ctrl+Shift+F5**, or set the mode in `config.json`.

| Mode | Behaviour |
|---|---|
| `opposite` | Console centred on the monitor GSPro is NOT on. Default — it can never cover the game. Falls back to `beside` on a single monitor. |
| `beside` | Docked immediately left or right of the GSPro window, whichever side has room. |
| `corner` | Overlays the top-right of the GSPro window. Handy on one monitor, but it sits over the game — only sensible in Borderless Windowed. |
| `off` | Disabled. |

```json
"console_follow": {
  "mode": "opposite",
  "poll_seconds": 2.0,
  "margin_px": 8,
  "title_patterns": ["gspro", "gs pro"]
}
```

### Why this does not knock GSPro out of fullscreen

Moving a window is exactly the operation that steals focus, and an
exclusive-fullscreen Direct3D application drops to windowed mode the instant it
loses focus. Every reposition therefore passes `SWP_NOACTIVATE | SWP_NOZORDER`,
and the module never calls `SetForegroundWindow`, `BringWindowToTop`, or
`ShowWindow`. The console is moved without ever being activated or raised.

Repositioning only happens when GSPro's rectangle actually changes, so a
stationary window costs one `GetWindowRect` every two seconds.

If GSPro still leaves fullscreen, use Borderless Windowed — it has no exclusive
mode to lose. `corner` mode in particular assumes borderless.

Running under `run_background.vbs` there is no console, so the follower detects
that and does nothing. Use `run_console.bat` to see the log.


---

## OCR: single-frame capture, parallel reads, per-field profiles

**One frame per capture.** All regions are cropped from a single screen grab, so
every field in a capture comes from the same instant. Previously 22 separate
grabs spread one capture over about two seconds, and a HUD update partway
through could mix values from two different moments.

**Parallel OCR.** Regions are read on a thread pool (`ocr_workers`, default 6).
Each Tesseract call is its own process, so threads give real parallelism. The
log prints a timing line on every capture — `timing: grab N ms, OCR N ms` — so
the speed-up can be measured on your machine.

**Per-field profiles** (`ocr_vocab.py`). Every field is read in single-line mode
(`--psm 7`) instead of block mode, and numeric fields get a character whitelist,
which removes the border artefacts (`-2.]`, `4]`, `-|`). P1 (total length) has no
whitelist because it needs the `'` and `"` of feet-and-inches, which cannot be
passed to Tesseract safely. Override per cell in `config.json` under
`ocr_profiles`, or set `"use_profile": false` on a region to use its own
`tesseract_config`.

Whitelists need **Tesseract 4.1 or newer** (5.x recommended). Older builds ignore
them harmlessly; the round score has a separate repair step either way.

**Vocabulary snapping.** Clubs and lies snap to known lists; courses snap to a
list learned from valid captures and stored in `known_courses.json`. A snap only
happens when the match is clear — an ambiguous read is left alone rather than
forced to a plausible but possibly wrong value. Raw OCR stays in Sheet1; the
snapped value feeds strokes gained and the SG sheets.

The course region (A1) is too narrow and truncates "The Hills Course" to
"The Hills Cours". Snapping cannot recover text that was never captured — widen
the region with `region_selector.py` to fix it.


---

## Crash safety and backups (v2.8.0)

Every file the daemon keeps is now written **atomically**: the new version is
written to a temporary file and swapped into place, so at every instant a file
is either the complete old version or the complete new one. Ending
`pythonw.exe` in Task Manager, a crash, or a power cut can no longer leave a
half-written file.

### The backups/ folder

| File | What it is | Kept |
|---|---|---|
| `<file>.bak1`, `.bak2` … | The previous versions, rotated on every write | 2 (report), 3 (state), 5 (history) |
| `<file>.session-start-<time>` | A copy taken each time the daemon starts | last 5 (report), 10 (history) |
| `<file>.pre-upgrade-v<N>-<time>` | The original, taken before a report upgrade | **forever** |
| `<file>.corrupt-<time>` | A damaged file that was replaced | **until you delete it** |

### If a file is damaged

The damaged file is moved into `backups/` (never deleted) and the newest backup
that reads is restored automatically. A warning naming every preserved damaged
file is logged at each startup until you delete it.

Only if no backup is readable:
- **Report** — the write is skipped rather than allowed to erase the other
  sheets. The capture is still in the log; `rebuild_from_log.py` can recover it.
- **Round history** — new sessions go to a fresh file; the original stays in
  `backups/`.
- **State** — the round restarts from defaults, but row positions are re-read
  from the report, so no existing rows are overwritten.

A file you **deleted** is treated as new and is never restored from backup —
delete or rename `ocr_output.ods` to start a fresh report.

### Row positions come from the report

At startup the next-free-row positions are taken from the report itself, not
just the state file. A stale or lost state file, `--reset`, or a deleted report
can no longer send new rows over existing data or leave blank gaps.

### Report upgrades

The report carries a format version in a small `_meta` sheet. When a newer
daemon finds an older-format report it upgrades it at launch, keeping a
permanent pre-upgrade copy, so new rows always line up with the columns. The
upgrade re-lays rows by column name, converts legacy yard distances on the green
to feet, and never touches Sheet1 (the raw OCR audit trail).

```
python ocr_daemon.py --upgrade-ods                  # upgrade the configured report
python ocr_daemon.py --upgrade-ods path\other.ods   # upgrade any report
```

Set `"auto_upgrade_ods": false` in `config.json` to upgrade manually instead.

### Tests

```
python test_strokes_gained.py     # strokes-gained maths (193 tests)
python test_storage.py            # crash safety, upgrades, typed cells, log archiving (24)
```

`test_storage.py` includes a regression test for the original history-erasure
bug.


---

## One shared scoring engine (v2.9.0)

Everything that decides what a capture means now lives in shared modules that
the live daemon **and** `rebuild_from_log.py` both run:

| Module | Role |
|---|---|
| `scoring.py` | Progression checks, session boundaries, shot scoring, gimmies, incomplete-hole discard |
| `parsing.py` | Raw OCR text → checked fields |
| `screen_capture.py` | Screen grabs, region crops, the monitor offset, DPI awareness |
| `ocr_reader.py` | One region → text; the pre-check read; the end-of-round screen |
| `reports.py` | The SG_Summary and Round Stats sheets, built from data |
| `version.py` | Version and changelog |

The engine writes no files and reads no clock directly: output goes to the
report (live) or memory (rebuild, tests), and a replay runs each capture at its
logged time. A rebuilt report is therefore exactly what the current daemon would
have produced live — verified byte-for-byte against your log — and uses the
same layout, so it can replace a live report directly.

Log lines now carry the date (`2026-10-03 14:05:22`), so any log replays exactly
across days. Older date-less logs still work: the date is taken from the log,
or pass `--date YYYY-MM-DD`.

### Fixed: shot and hole rows overwritten between sessions

Since v2.2, when a session ended mid-run (course change or a 90-minute idle gap),
the new session wrote its SG_Shots and SG_Holes rows over the previous session's
from the top, and the end-of-run cleanup then cut the sheets to the new session's
length. On the log tested, 91 of 119 shot rows and 9 of 39 hole rows were lost
this way. Your session totals in Round Stats were unaffected; the per-shot and
per-hole sheets were not.

**To recover a damaged report**, rebuild it from your log, which holds every
capture:

```
python rebuild_from_log.py ocr_daemon.log -o ocr_output_rebuilt.ods --history-out round_history_rebuilt.json
```

Check the rebuilt file, then with the daemon stopped, rename your current
`ocr_output.ods` and rename the rebuilt one to take its place. Only replace
`round_history.json` too if your log covers **all** your play — a log started
fresh partway through would rebuild only part of the history. Backups of
everything are kept in `backups/` either way.

### Tests

```
python test_strokes_gained.py     # strokes-gained maths (193)
python test_storage.py            # crash safety, upgrades, typed cells, log archiving (23)
python test_engine.py             # holes, recovery, lost sign, end of round, round check, lie penalty (75)
python test_dashboard.py          # the HTML dashboard (12)
python test_report_worker.py      # background report refresh (10)
python test_field_cache.py        # re-reading fields only when they change (12)
python test_rebuild_screenshots.py # re-reading saved screenshots (7)
python test_shutdown.py           # exiting while a capture is running (7)
python test_coverage.py           # coverage beside strokes-gained figures (11)
python test_sides.py              # left/right in the dispersion plot (10)
python test_db.py                 # database, transactions, recovery, migration (15)
python test_capture.py            # capture, character matching, wind arrow, batched Tesseract, LAST SHOT panel (49)
```


---

## How holes are counted (v3.0.0)

Every hole now has an explicit lifecycle:

1. **In play** — from its first capture.
2. **Over** — the ball is holed, or comes to rest inside the gimmie circle.
3. **Finalised, exactly once** — when the next hole starts, or when the session
   or run ends. Only then is the stroke count verified against GSPro's round
   score and the hole written to SG_Holes.

After a hole is over, the next capture starts a new hole **even if the hole
number is the same**. Distance-challenge stations repeat a hole number for
every attempt; identifying holes by number alone carried one attempt's strokes
into the next.

### Fixed in this version

| Problem | Effect | Fix |
|---|---|---|
| Holed-out holes finalised twice | Counted twice: an extra row and its strokes | Finalised once, at close |
| Repeated hole number continued the last attempt | Second attempt's score included the first's | A finished hole always ends; the next capture is fresh |
| New course starting on the same hole number | Captures rejected as replays | A course change is always progression |
| GSPro round reset (hole 9 → 1, score back to E) | Read as strokes: one 25 yd hole scored 14 | Round score not used across a reset |
| Implausible round-score jump | Phantom strokes from a misread | Capped at `max_reconcile_strokes` (default 5) |
| Unfinished-hole check used the hole number | A repeat attempt could escape the discard | Explicit "finalised" flag |
| Hole holed on the last capture of a run | Could be discarded as incomplete | Holes that are over are finalised at exit |

On the log tested: 39 holes / 130 strokes / worst hole 14 became **33 holes /
90 strokes / worst hole 5**, with every stroke backed by a shot row.

When the round score cannot be used, only certain strokes are added: none for a
holed ball, the conceded putt for a ball in the gimmie zone.

**Your existing reports contain the double counts and phantom strokes.** Rebuild
them from your log as described in the v2.9.0 section.

### Strokes the capture missed

When the round score shows more strokes than were captured, the difference is
added as "uncaptured stroke" rows. Those strokes are real — GSPro counted them —
but their strokes-gained values are estimates, scored as putts from the last
known position. On the log tested, 39 of 90 strokes were added this way. The
fewer shots the capture misses, the more trustworthy the per-shot statistics.


---

## Numbers you can chart, logs you never lose, and capture quality (v3.1.0)

### Report format 3: real numbers, dates and times

Every sheet except Sheet1 now stores numbers as numbers and dates and times as
real date/time cells, so SG, distances and scores can be summed, sorted and
charted directly, including by date for week-over-week trends. Sheet1 stays as
text: it is the raw OCR audit trail, and re-typing it would change what it records.

Your report upgrades automatically on first launch (format 1 or 2 → 3 in one
pass), with a permanent copy of the original in `backups/`. Displayed values are
unchanged; LibreOffice may show `+0.512` as `0.512`, since a number cell has no
stored plus sign.

### Log archiving

When `ocr_daemon.log` reaches `log_max_mb` (default 5 MB) it moves into a
`logs/` folder with a timestamp and a fresh log begins. **Nothing is deleted** by
default (`log_keep_archives: 0`), because the log is what `rebuild_from_log.py`
replays. To rebuild from everything:

```
python rebuild_from_log.py ocr_daemon.log --with-archives -o rebuilt.ods
```

### Region check

At launch the daemon reads hole and par once and logs whether it succeeded. A
failure is normal if GSPro is on a menu; if you are on a hole, the regions may
have moved after a GSPro update, a window-mode change or display scaling. The
daemon also warns after 8 unreadable captures in a row. To see exactly what each
region captures:

```
python ocr_daemon.py --check-regions
```

This saves every region's image to `region_check/` and prints its OCR text.

### Capture quality

Round Stats gains two sections:

- **CAPTURE QUALITY**, one row per session: captures, accepted, menu screens,
  replays skipped, and how each stroke is known: **captured**, **conceded** (a
  gimmie, which is certain) or **estimated** (inferred from the round score, with
  an approximate strokes-gained value). Coverage is the share captured or certain.
- **FIELD READ HEALTH** for the current session: how often each region reads
  blank or unreadable on genuine golf screens. Launch-monitor fields are not
  counted on putts, where GSPro leaves them blank.

Because quality is counted by the shared engine, rebuilding an old log produces
these statistics for sessions recorded before this version.


---

## The database is the master record (v3.2.0)

Everything the daemon captures now lives in **`gspro_stats.db`**, an SQLite
database beside the daemon (with `-wal` and `-shm` companion files while it
runs). The ODS report is **generated from it**.

### What this changes

- **One transaction per capture.** A capture's raw row, shot rows, hole rows
  and the daemon state commit together or not at all. A crash mid-capture
  leaves no trace; the capture is still in the log for `rebuild_from_log.py`.
- **The spreadsheet is written far less.** It used to be rewritten in full for
  every raw row, shot, hole and summary update — roughly 270 times on the log
  tested. It is now regenerated when a hole finishes, at exit, and on demand:
  34 times on the same log. Set `"report_refresh": "exit"` to write it only at
  exit.
- **Keeping the report open in LibreOffice is safe.** A refresh that finds the
  file open is skipped with a log message and happens next time. Nothing is
  lost, because the data is in the database.

To regenerate the report at any time:

```
python ocr_daemon.py --export-report
```

### First launch on v3.2

Your existing data is imported automatically, in one transaction: the state
file, the round history, and the rows of `ocr_output.ods` (brought up to the
current column layout). Afterwards `ocr_state.json` and `round_history.json`
are renamed `*.migrated`, with permanent copies in `backups/`, so they cannot be
imported twice. If anything fails, the database is left untouched and the next
launch tries again.

### Backups and recovery

At every launch the database is snapshotted into `backups/` using SQLite's
backup API, which is safe while the database is in use (the last 5 are kept).
A database that fails to open or fails its integrity check is moved into
`backups/` — never deleted — and the newest good snapshot is restored.

Don't copy `gspro_stats.db` by hand while the daemon is running: recent changes
may still be in the `-wal` file. Use the snapshots in `backups/`, or stop the
daemon first.

To recover from the log into a database you can adopt:

```
python rebuild_from_log.py ocr_daemon.log --with-archives --db-out recovered.db
```

With the daemon stopped, rename `gspro_stats.db` (and its `-wal`/`-shm` files)
and rename `recovered.db` to take its place.

**To start completely fresh**, stop the daemon and rename both
`gspro_stats.db` (with `-wal`/`-shm`) and `ocr_output.ods`. Renaming only the
database would re-import the report's rows on the next launch.

### Querying your data

Sessions have typed columns, so trends need no spreadsheet at all, e.g. with the
`sqlite3` command-line tool:

```sql
SELECT week, SUM(holes), ROUND(SUM(sg_total) / SUM(holes), 3) AS sg_per_hole
FROM sessions GROUP BY week ORDER BY week;
```

Report rows are stored exactly as they appear in the report, as JSON lists of
cell texts; SQLite's `json_extract` reads individual columns.


---

## Clubs: gapping and strokes gained by club (v3.3.0)

### Fixed: shots were credited to the wrong club

After a shot settles, GSPro's HUD has already switched to the club for the
**next** shot. Versions before 3.3 credited each shot to the club read at that
moment, so every shot carried the next shot's club. On the log tested, all 26
shots that finished on the green were labelled "Putter", and the eight 8-iron
shots never appeared under their own name.

Each shot is now credited to the club shown **before** it; a hole's set-up
capture supplies the tee shot's club. SG_Shots gains a **Club** column (report
format 4); `Region9` keeps the raw club read after the shot. Per-club figures
from earlier versions, including SHORT / LONG BY CLUB, were attributed this way
and should not be relied on — rebuild from your log for corrected figures.

### Upgrading existing data

Old rows are re-credited automatically: row N−1's displayed club is exactly the
club for shot N. A hole's **first** shot has no predecessor in SG_Shots and is
left blank; a rebuild from the log recovers it, because the log holds the hole's
set-up capture. Rows stored in the database are upgraded the same way at launch,
before any new row is written.

### The Clubs sheet

- **CLUB GAPPING** — for full-swing shots (putts excluded): median carry, the
  middle 50% (p25–p75 and IQR), spread, shortest and longest, median total
  length and ball speed, and the gap to the next club down. Under 5 yd between
  neighbouring clubs is flagged as an overlap, over 20 yd as a large gap. Clubs
  with fewer than 3 shots are listed but not judged. Medians and quartiles are
  used because a single misread or chunked shot barely moves them.
- **The same for the last 30 days of play**, when your data spans longer.
- **STROKES GAINED BY CLUB** — total and per shot, split by category, with each
  club's mean short/long bias (feet for the putter).

Estimated strokes carry no club and are left out; the sheet says how many.

**Gapping assumes full swings.** Practice and distance-challenge stations
(25/50/100 yd) make wedges hit partial shots, so their "gaps" there describe the
stations, not your bag. Gapping is most meaningful from full-swing play.


---

## Verified on real GSPro frames (v3.4.0)

Seven screenshots from a real session were run through the actual capture
code and scored against what is visibly on screen: **65 of 91 fields correct
before, 91 of 91 after.** Launch-monitor fields in live captures also matched
the debug shot's known values exactly (12 of 12 captures).

### Fixed

| Problem | Effect | Fix |
|---|---|---|
| Stroke ticker read strokes 1–2 one too high | Auto-capture saw "stroke unchanged" and **skipped real shots**; progression checks misjudged | Reads which cell is highlighted, wherever the cells sit in the region |
| A skipped screen started auto-capture's 4 s cooldown | The stroke counter's real update a moment later was discarded | The cooldown starts only after an actual capture |
| Daemon capturing the wrong monitor | Every capture unreadable until toggled by hand | **Follows GSPro to its monitor automatically** (`auto_window_offset`) |
| Elevation: sign from colour, only inches read | `▼7ft 9` recorded as `+9` | Sign from the triangle's shape, full feet-and-inches read |
| E1 sat over the wind **speed** text but ran an arrow detector | A meaningless direction every time | E1 reads wind speed; direction is "calm" at 0 mph |
| Club and course regions too narrow | `L Weda`, `Central Par` | Club 80 px wide, course 175 px |
| `---` (no shot data yet) counted as a misread | Launch-field health looked far worse than it is | Counted as "no data" |
| "Deep" lie not in the vocabulary | Read correctly but counted unreadable | Recognised (scored on the heavy-rough table) |

The wind arrow (T1) still reads direction poorly; wind is not used in scoring,
and the direction is recorded as "calm" whenever the wind is 0 mph.

### Strokes gained categories

- **Unattributed** — strokes the capture missed but GSPro's round score proves
  happened. Their start positions are unknown, so they are no longer filed under
  Putting. Totals are unchanged; on the log tested, Putting went from −19.5 to
  −0.3 for one session, with −14.8 now shown as Unattributed.
- **Par-3 tee shots count as Approach**, per the PGA Tour definition.

Report format 5 applies both to existing rows and renames "Wind Dir 1 (E1)" to
"Wind Speed (E1)", clearing its old meaningless values. Session totals already
in your history were summed under the old rules — rebuild from the log to
recompute them.

### Seeing why auto-capture fires or skips

Set `"auto_capture_verbose": true` for one session to log every watcher
decision. `test_fixtures/` holds the real-frame crops used by `test_capture.py`.


---

## Screenshots for reviewing a session (v3.5.0)

| Keys | Action |
|---|---|
| **Ctrl+Shift+F12** | Save a timestamped screenshot of the GSPro monitor now |
| **Ctrl+Shift+F4** | Toggle **every-capture** mode on/off |

Start with every-capture mode already on with `python ocr_daemon.py --screenshots`
(or `"screenshot_every_capture": true` in `config.json`).

In every-capture mode:

- **Each capture saves the exact frame the OCR read** — the screenshot is the
  capture, not a second grab a moment later. Auto-capture fires when the screen
  settles after a shot, which is the screen *before* your next shot, so a session
  played this way gets a screenshot before every shot with no key presses.
- **File names carry the result:** `…_auto_h2_s2_accepted.png`,
  `…_auto_h1_s1_no_progression.png`, `…_invalid.png`.
- **Screens auto-capture skipped are saved too:** `…_settle_skipped.png` and
  `…_settle_cooldown.png` — the moments where missed shots hide.
- **The log names every file** in its capture block, so frames and log entries
  line up one to one.

Files go to `screenshots/<date>/`. Saving happens on a background thread, so
captures never wait for the disk; if the disk ever falls far behind, a frame is
dropped with a log message rather than delaying play. A PNG is about 2–4 MB, so a
long session can use several hundred MB — set `"screenshot_format": "jpg"` for
about a tenth of the size (PNG keeps the exact pixels, which matters if frames
are re-read later). Delete old date folders when you are done with them.

Hotkeys can be changed in `config.json` (`screenshot_hotkey`,
`screenshot_toggle_hotkey`) if they clash with anything else on your PC.

---

## Character matching for numeric fields (v3.6.0)

GSPro draws its HUD in a few fixed fonts, so the numeric fields — launch data,
remaining distance, elevation, hole, par, yardage and the round score — are now
read by matching each character's shape against stored examples: about **1 ms
per field instead of about 85 ms** with Tesseract. On the real frames tested, a
capture's OCR time fell from about 1.5 s to about 0.6 s (one field at a time;
the daemon reads several at once), with every field still read correctly.

**It never reads worse than before.** If any character is not recognised
confidently, the field is read by Tesseract exactly as in earlier versions.

**It switches on as it learns.** The starter set (`digit_templates.seed.json`)
comes from real frames, which did not contain every digit: the launch panel
lacks a 7 and the HUD text a 3. A font is matched only once **all ten digits**
are on file — in testing, a font missing its 9 read `398' 4"` as `308' 4"`, a
wrong value that passes every format check, and this rule prevents exactly that.
Missing characters are learned from Tesseract reads during play (a learned
character needs two agreeing sightings) and saved in `digit_templates.json`,
which upgrades never overwrite. The launch log shows each font's status, e.g.
`panel font: learning; still needs 7`.

**It is cross-checked.** Every 25th matched read of a font is also read by
Tesseract; if they disagree three times (and more than 10% of checks), matching
is switched off for that font for the session. The exit log summarises how many
reads were matched, fell back, or were learned.

Set `"digit_templates": false` in `config.json` to turn it off entirely.

---

## v3.6.1 — fixes from a real round's screenshots and log

A full Operation 36 round (Central Park South, nine 25-yd par 4s, 26 strokes)
was reviewed frame by frame against the log. v3.6.0 recorded 6 holes and 18
strokes, one of them wrong; replaying the same log through v3.6.1 records every
hole it can account for correctly, and with the pre-check fix holes 1–8 (23
strokes) match GSPro's card exactly.

- **New holes skipped** — the auto-capture pre-check treated an unreadable hole
  number as "unchanged", and a new hole resets the stroke counter *down*, which
  it did not look for; holes 8 and 9 were never captured. Now anything it cannot
  read, or any change in the stroke counter, runs the full capture.
- **Holes with no captured shot vanished** — a tee shot that stops in the gimmie
  circle sends GSPro straight to the next hole. Such a hole is now closed from
  the round score (E → −2 on a par 4 = 2 strokes).
- **No more impossible scores** — a hole not seen finished, whose strokes the
  round score cannot supply (a new round started, or the score was unreadable),
  was recorded with only its captured strokes: a 1 on a par 4. It is now left
  out, with a warning in the log. This also affected the last hole of rounds.
- **Remaining distance cut off** — values like `25ft 11` ran past the K1 region
  and read as `25ft 1`. K1 is now 78 px wide (was 70). *If you keep your own
  config.json, change K1's width to 78.*
- **Spin axis `-4.5` read as `-45`** — the launch fields always show one decimal
  place, so a dropped point is restored; the character reader's minus-sign rule
  was also fixed.
- **Character matching works from the first launch** — the starter set now comes
  from 37 verified frames and covers both main fonts (only the round-score font
  still learns). Leave-one-out on all 37 frames: 474 of 518 fields read
  correctly, 44 handed to Tesseract, none wrong.

---

## Final strokes recovered from the next tee frame (v3.7.0)

When a hole ends on a holed stroke, or a ball that stops in the gimmie circle,
GSPro moves straight to the next hole, so the final stroke is never captured.
Until now it was inferred from the round score and filed as **Unattributed**.

But the next hole's first frame still shows that stroke on the shot panel —
ball speed, launch angles, total length — and where it started is the last
captured position. Combined with the stroke count from the round score:

- **one stroke missing** → the final stroke was holed;
- **two missing** → it stopped in the gimmie circle (about |start − total
  length| from the hole) and the putt was conceded;

so it is now recorded as a real shot, with its category, club and launch data,
noted `recovered: holed` or `recovered: stopped in gimmie circle` in SG_Shots.

It is used only when the evidence agrees — the frame shows the next hole
untouched (on the tee, stroke 1, full yardage), the shot panel changed since the
last capture, and the shot's length fits — otherwise the old behaviour applies.
A hole's score and strokes-gained total are **unchanged** either way (they come
from the round score); only how its strokes are classified changes.

On the reviewed Operation 36 round (holes 1–8): Unattributed strokes 13 → 0,
coverage 10 of 23 strokes → 23 of 23, hole scores and strokes-gained totals
identical to v3.6.1.

If the log says *GSPro conceded a putt from about 3.3 ft, but gimmie_zone_ft is
1.5*, set `gimmie_zone_ft` in config.json to GSPro's gimmie distance.
`"recover_final_strokes": false` turns recovery off.

---

## Dashboard (v3.8.0)

`gspro_dashboard.html` is written next to the report every time the report is
refreshed — after each hole and at exit — and **also when the spreadsheet is open
in LibreOffice** (which blocks the report, not the dashboard). Open it in any
browser; it needs no internet and no other files.

- **Week by week** — strokes gained per hole for each category and in total,
  as trend lines (from the second week of play).
- **By category** — this session against the last 30 days, per hole.
- **Club gapping** — every club's carry: middle half of shots, median, shortest
  and longest, the gap to the next club, overlaps and large gaps flagged.
- **Dispersion by club** — every shot's distance offline against short/long of
  pin high (putts in feet). Left and right cannot be told from distances alone,
  so offline is shown as a distance from the line.

Hover over any point or bar for its exact value. The figures come from the same
calculations as the spreadsheet (weekly totals, gapping), so the two agree.

    python ocr_daemon.py --dashboard            # write it now and open it
    python rebuild_from_log.py LOG --dashboard dash.html

Settings: `"dashboard": false` turns it off; `"dashboard_file"` sets its name.
Practice sessions such as Operation 36 are not yet separated from real rounds,
so they appear in the trends and gapping (TODO T-01).

---

## v3.8.1 — holes dropped because the round score lost its minus sign

In a 36-hole Operation 36 session, v3.8.0 recorded only 19 holes. The shots were
all captured; whole holes were then left out because the round score did not add
up — GSPro's **−6 was read as "6"** (taken as +6), −7 as "−1", −8 as nothing.

- **The round score is now read by character matching.** It is drawn in the
  launch panel's typeface, so it uses the panel's digits; all 93 verified
  round-score frames read correctly (−6, −7, −8, −9, −10, −12 included).
- **A number without a sign is no longer taken as over par.** GSPro always shows
  "+" over par. When the sign is lost, the engine keeps the value that gives a
  possible hole result — and records nothing if more than one would.
- **A score unreadable on the tee frame** is filled in from any later capture on
  the hole (the score cannot change mid-hole).
- **Cross-checks are fair to character matching.** A Tesseract read that is not
  even a valid value ("Hole" with no number) no longer counts as a disagreement.
- **No silent losses.** A hole with no captured shot that cannot be accounted
  for is now reported in the log.

Replaying the session from its own saved frames: every round now records holes
1–8, with totals matching GSPro exactly (22, 20, 25 and 29 strokes) — 32 holes
instead of 19. Hole 9 of each round is still missing: it ends on the
end-of-round screen (TODO T-28).

---

## The last hole of a round (v3.8.2)

A round ends on its last hole's final shot, so no next hole shows how that hole
went — v3.8.1 left it out. GSPro then shows its end-of-round screen with the
round's total (Operation 36: "24 SCORE"), and the daemon now reads it:

- a cheap pixel check on every capture spots the screen (its white header band
  above the dark score box) — tested on 150 screenshots, it fired on the 5 end
  screens and nothing else — and only then reads the total and the "SCORE"
  label under it;
- the last hole's strokes are the total minus the strokes before it (the pars of
  the round's earlier holes plus the round score at the last hole's tee). It is
  used only when every earlier hole of the round was seen and one result fits;
- when the next round starts, its first tee frame still shows the final shot,
  so the last hole's final stroke is recovered as well (v3.7.0). If you exit
  instead, the hole is still closed from the total.

Replaying the reviewed session with the end screens: **36 of 36 holes, 107
strokes — exactly GSPro's four end screens (24, 22, 28, 33).**

The end screen's areas are under `"end_screen"` in config.json, in the same
coordinates as the regions. An existing config.json now picks up settings added
in later versions automatically; nothing you set is changed.

---

## Background report refresh (v3.8.3)

The report and dashboard used to be rewritten on the capture thread after every
hole, so the next capture waited for them — 14.7 s at a season's 6,000 shots.
They are now written by a background worker:

- a capture only *asks* for a refresh (0.2 ms) and carries on;
- refreshes asked for within `report_debounce_sec` (2 s) of each other are
  combined; one asked for during a refresh gets exactly one follow-up, so the
  report always catches up;
- the report reads the database from a snapshot that does not hold up captures
  (WAL mode) — measured on a 6,000-shot database, captures written during a
  refresh took 12 ms typically, 0.18 s at most;
- the start-up refresh also runs in the background, so capturing starts at once;
- at exit, a refresh in progress (or asked for) finishes first, then the final
  report is written as before.

`"report_background": false` in config.json restores the old synchronous refresh.
The refresh itself still takes as long as before (≈15 s at 6,000 shots) — the
report just lags by that much. A faster writer (TODO T-11) shortens it.

---

## Unchanged fields are not read again (v3.8.4)

The course name, hole, par and yardage change at most once per hole, but every
capture read them again — the course name with Tesseract. Now each of these
fields keeps its last reading and the pixels it came from, and is read again
only when those pixels change. The test is strict: no pixel may change by more
than 24 of 255 (a changed character moves pixels by ~200), so a new hole, course
or yardage is always read fresh.

On the reviewed 36-hole session: the course name was read once instead of 70
times; the hole number was re-read at every new hole and nowhere else; no
reading differed from a fresh one; OCR time per capture fell from 580 to 470 ms
(one region at a time). Readings reused this way are marked `(unchanged)` in the
log. Settings: `"ocr_cache": false` turns it off; `"ocr_cache_cells"` lists the
fields (default A1, B1, C1, D1).

---

## Each round checked as it ends (v3.8.6)

When GSPro's end-of-round screen appears, the console says how the round was
recorded — at the moment you are looking at the screen:

    ━━━  Round complete — 9 of 9 holes recorded, 24 strokes, matching GSPro's 24  ✓  ━━━

or, when something was lost, which holes and why:

    ━━━  Round complete — 8 of 9 holes recorded, 22 strokes (GSPro's total: 24)  ━━━
           not recorded: hole 4 — round score unreadable

A round that ends without the end screen (a new round started, or you exit) is
reported too ("Round ended …", "Round so far …"). If the round score cannot be
read on three captures in a row, a warning appears straight away — on 5 Oct,
that is the problem that cost most holes, and it went unnoticed for a day.

---

## Re-reading saved screenshots (v3.8.7)

With screenshot mode on (Ctrl+Shift+F4), every capture's frame is saved. If a
sitting was recorded badly — by an older version, or before a fix —
`rebuild_from_screenshots.py` replays those frames through the current
pre-check, reader and engine (the same code the daemon runs) and rebuilds the
report. Each round is checked as it goes, against GSPro's end screen.

To put a recovered sitting into your whole history, give your log too: its
captures outside the screenshots' time are kept, those inside are replaced.
Close the daemon first, then, in the program folder:

    python rebuild_from_screenshots.py screenshots --log ocr_daemon.log --with-archives ^
        -o ocr_output_new.ods --db-out gspro_stats_new.db --log-out ocr_daemon_corrected.log

Check the round lines it prints, then adopt the result: rename `gspro_stats.db`
to keep it as a backup, and rename `gspro_stats_new.db` to `gspro_stats.db`.
Keep `ocr_daemon_corrected.log`: rebuilding from it later reproduces this result
(a rebuild from the original log would bring the old misreads back). Nothing in
your folders is changed by the tool itself; it writes only the files you name.

On the 4 Oct screenshots (130 frames, two sittings) it records 45 holes, and
all five rounds report "9 of 9 holes recorded … matching GSPro's total ✓".

---

## Wind direction (v3.8.8)

The wind arrow (T1) is read from its shape: GSPro's arrow is a dart, and the tip
is the point farthest from the notch between its two barbs. The reading gives
the arrow's direction on screen (N = up, along your target line) and what the
wind does to the ball, e.g. `NNW (helping, slightly right-to-left)` or
`E (left-to-right)`. At 0 mph it is recorded as "calm", as before.

GSPro's guide says the arrow is relative to your orientation but not whether it
points where the wind blows *to* or comes *from*. The default is "to" (the usual
convention for wind arrows); every frame so far was at 0 mph, so it is not yet
confirmed. If, on a windy hole, the ball drifts *against* the arrow, set
`"wind_arrow_points": "from"` in config.json. Wind is not used in scoring yet.

---

## Faster OCR: one Tesseract run per capture (v3.8.9)

Most of a Tesseract read is Tesseract starting up (~75 ms). The fields still
read by Tesseract — lie angles, wind speed, club and both lies — are now read
together: each field's prepared image becomes a page of one multi-page image,
read in one run per group of identical settings (two groups today). Measured on
30 real frames: 2.5 Tesseract starts per capture instead of 5.6, and OCR time
per capture from 467 to about 270 ms, with every reading unchanged. If a batch
fails, its fields are read one at a time. `"ocr_batch": false` turns it off.

The same review found that a cross-check disagreement between character
matching and Tesseract replaced the matched read with Tesseract's. Every
disagreement checked on screen was Tesseract misreading — a round score of −7
read as −1, a carry of 27.4 as 21.4 — so the matched read is now kept (a run of
disagreements still switches matching off, as before).

---

## Lie penalty: GSPro's LAST SHOT panel (v3.8.10)

After a shot from a penalising lie, GSPro shows how much ball speed and spin
the lie let you keep — for example 80% speed and 53% spin from deep rough. The
daemon now reads both and records them with that shot, in two new SG_Shots
columns at the end, "Speed Kept %" and "Spin Kept %" (no existing column moves).
Round Stats gains a "LIE PENALTY" table: for each lie, how many shots, the
average speed and spin kept, and the average strokes gained.

The panel stays on screen through the putts that follow and onto the next tee,
so it is credited only to a shot that started off the green. It is read only
when it is showing (a quick colour check), so captures without it cost nothing
extra. The panel's position comes from the `last_shot_panel` setting, filled in
automatically — no change to your config.json is needed. Reports and databases
are upgraded to format 6 automatically; earlier shots are blank.

---

## Exiting during a capture (v3.8.11)

Pressing the exit hotkey (Ctrl+Shift+F10) while auto-capture is capturing a
shot used to let Python shut down underneath the capture: the shot was lost,
and the console could show `cannot schedule new futures after interpreter
shutdown` followed by `Fatal Python error … daemon threads`. Now exit happens
in order: hotkeys and new captures stop first, a capture already under way is
finished and recorded (you may see "Finishing the capture in progress…"), and
only then are the round closed and the reports written. A shot whose ball is
still settling when you press exit is not captured — wait for its capture line.

---

## Coverage beside every strokes-gained figure (v3.8.12)

Strokes inferred from the round score count in SG Total (as Unattributed) but
in none of the four categories. So every strokes-gained figure now says how
much of the play behind it was captured: a "Capture coverage" row under each
session's total, a Coverage column in the session log (and career total), by
course, and week by week (in SG_Summary beside the Sample column), "Captured"
(e.g. "1 of 3") for each hole, and coverage in the lie and distance-band
headings, the Clubs sheet and the dashboard's category legend and weekly chart.
Under 80% it is marked "(low)" — the category figures then leave out a fifth or
more of the play. Holes lost entirely are not part of coverage; they are simply
missing from the figures (see the Holes count and the round check).

---

## Left and right in the dispersion plot (v3.8.13)

How far offline a shot finished is measured from the screen; which side it was
on cannot be told from distances. It is now **estimated** from the launch data:
predicted miss = length × sin(HLA) + 0.28 × length × sin(spin axis), positive =
right of the target line as you face it. On 75 full shots (13–110 yd) the size
of that prediction matches the measured offline distance closely (r = 0.93,
about ±1.3 yd), and in GSPro's ball-camera view — where the ball is centred and
the pin's position shows the side — the predicted side was right on every shot
checked whose predicted miss was 0.5 yd or more (7 of 7).

The plot draws those firm estimates filled, and shots too close to the line to
call hollow, as are putts (their side is only the start line — the green's
slope is not known). Each panel's caption counts left and right.

To check the estimate yourself — after switching hands, or with longer clubs —
turn on screenshot mode for a session, then:

    python verify_sides.py screenshots\2026-10-08

---

## Improvement tracking

- **`TODO.md`** — every suggested improvement not yet done, with priority,
  effort, the evidence behind it, and a recommended order.
- **`COMPLETED.md`** — suggestions that have been delivered, with the version
  and what each achieved.

When an item is finished it moves from TODO.md to COMPLETED.md and keeps its ID.
