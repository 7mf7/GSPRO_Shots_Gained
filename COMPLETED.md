# COMPLETED — suggested improvements that have been delivered

Items move here from **TODO.md** when they are finished, keeping their ID,
with the version that delivered them and what they achieved. Newest first.

Suggestions completed before this file existed (v2.7.0–v3.4.0) are recorded
below under C- numbers. For changes that came from your direct requests
rather than suggestions, see the changelog (`python ocr_daemon.py --version`).

---

## v3.8.13

### C-57 · Left and right in the dispersion plot (was T-31)
**Delivered in:** v3.8.13 · **Origin:** dashboard, v3.8.0 · **TODO item:** T-31

`reports.estimate_side`: predicted lateral = L·sin(HLA) + 0.28·L·sin(spin
axis). The factor and its sign were fitted, not assumed: on 75 full shots the
predicted size of miss matches the geometry's measured offline distance with
r = 0.93 (HLA alone 0.80), and a positive axis bends the same way as a positive
HLA. The absolute side came from GSPro's ball-camera settle frames — the ball
centred, the camera along its path, the pin's offset giving the side — read
automatically (ball-distance box present; flagstick base by shape, since it
leans): firm estimates (≥ 0.5 yd) 7 of 7 right, weaker ones 4 of 6. The plot is
now signed, firm points filled, uncertain points and putts hollow, with left /
right counts and an "estimated" note. `verify_sides.py` repeats the check on
any screenshots folder; the 12 screen-checked shots are a test fixture.

---

## v3.8.12

### C-56 · Capture coverage beside every strokes-gained figure (was T-18)
**Delivered in:** v3.8.12 · **Origin:** review after v3.3.0 · **TODO item:** T-18

One calculation (`reports.coverage_counts` / `coverage_label`), shared by the
report and the dashboard: strokes captured, recovered or conceded ÷ all strokes,
never rounded up to 100%, "(low)" under 80%. Shown in SG_Summary (current round;
week by week beside Sample), Round Stats (this session; session log and career
total; by course; week by week; hole by hole as "Captured"; lie and distance-band
headings), Clubs, and the dashboard (category legend; each week, amber when low,
and in its tooltip). Each hole now records how many of its strokes were
estimated. Sessions from before v3.1 show "—" and are left out of aggregates,
with a note. On the merged history: 100% / 100% / 97% by sitting, 98% career —
matching the Capture Quality counts and the Clubs sheet exactly.

---

## v3.8.11

### C-55 · Wind arrow works with OpenCV 5
**Delivered in:** v3.8.11 · **Origin:** found while delivering C-54

OpenCV 5 returns convexity defects as (N, 4) rather than OpenCV 4's (N, 1, 4);
the notch lookup (C-49) indexed the old shape, so under OpenCV 5 every wind
arrow read as blank. The defects are now reshaped to (N, 4), which reads either
version; all 25 verified arrows pass under OpenCV 5.0.

### C-54 · Exit waits for a capture in progress
**Delivered in:** v3.8.11 · **Origin:** crash on exit, 8 Oct

At exit the watcher was told to stop but not waited for, and nothing waited for
a manual capture: a capture started just before or as the exit hotkey was
pressed ran on while the exit reports were written and Python shut down. It
then failed starting its parallel OCR ("cannot schedule new futures after
interpreter shutdown"), so the shot was lost, and logging that during Python's
final teardown aborted it ("Fatal Python error … stdout … daemon threads"). The
capture and the exit reports could also both write the round state. Now
`_finish_run` sets a shutting-down flag (run_pipeline refuses new captures),
stops the hotkeys, stops auto-capture and waits for its capture
(`ShotWatcher.stop(wait=True)`; a stop during the pre-check starts no capture),
waits for any manual capture by taking the pipeline lock, and holds it while the
exit reports are written. Reproduced end to end: v3.8.10 prints the same error
after "Daemon stopped"; v3.8.11 records the capture, then writes the reports,
with no error (test_shutdown.py, 7 tests).

---

## v3.8.10


### C-53 · The two remaining misreads of the reviewed round are gone (was T-30)
**Resolved by:** earlier fixes, verified in v3.8.10 · **Origin:** real-round review, v3.6.1 · **TODO item:** T-30

The hole number at 13:04:47 — thought to be the panel background showing
through — has pixels byte-identical to the correctly read 13:04:45 frame; it
was a reader limitation, and the current reader reads both as "Hole8". The
total length at 13:02:11 now reads 13'9" (truth 13' 9"). No change was needed.

### C-52 · GSPro's LAST SHOT panel recorded (was T-08)
**Delivered in:** v3.8.10 · **Origin:** observed in screenshots, v3.4.0 · **TODO item:** T-08

Two regions, X1 (speed kept) and Y1 (spin kept), from a new `last_shot_panel`
setting that older configs pick up from the defaults. They are read only when
the panel is showing — its crops are ~80% navy, every other frame's 0% — and
then by Tesseract (digits and %) inside the shared batch (C-50). The values go
to the shot just played, but only if it started off the green: the panel
lingers through putts and onto the next tee (seen at 09:22:14). SG_Shots gains
"Speed Kept %" and "Spin Kept %" at the end (format 6, migrated automatically);
Round Stats gains a by-lie LIE PENALTY table, shown only when there is data.
Replaying the 09:12–09:22 screenshots: the panel read in exactly its two frames,
80/53 credited to the shot from deep rough only, the table filled. Daemon log on
27 real frames unchanged apart from the two (empty) new lines; 4 Oct replay
unchanged; no measurable time added.

---

## v3.8.9

### C-51 · A cross-check disagreement keeps the matched read
**Delivered in:** v3.8.9 · **Origin:** found while delivering T-37

Every 25th matched read is also read by Tesseract; when the two disagreed, the
matched read was replaced by Tesseract's. Batching changed which reads were
cross-checked and exposed it: all four affected readings, checked on screen,
were Tesseract's 7→1 misread replacing a correct value — a round score of −7
recorded as −1 (the 5 Oct failure, by another route), a carry of 27.4 as 21.4,
24.7 as 24.1, 23.7 as 23.1. The matched read is now kept; disagreements still
count toward switching matching off for a font (C-48). Readings also no longer
depend on which read happens to be cross-checked.

### C-50 · One Tesseract run per capture (was T-37)
**Delivered in:** v3.8.9 · **Origin:** OCR speed review (v3.8.8) · **TODO item:** T-37

~75 ms of each ~70–100 ms Tesseract read was start-up. `ocr_reader.read_frame`
now reads everything that needs no Tesseract first, collecting the rest as jobs
(`ocr_image(…, defer_tesseract=True)`), then reads each group of identical
settings in one run of a multi-page TIFF, split on the page separator
(`run_tesseract_jobs`); a failed batch or wrong page count falls back to single
runs. On 30 real frames: 5.6 → 2.5 Tesseract starts per capture, OCR 467 →
~270 ms per capture; readings identical apart from the four C-51 corrections;
the daemon's log on 27 real frames identical; the 4 Oct screenshot replay gives
the same 45 holes and round checks. `"ocr_batch": false` restores single runs.

---

## v3.8.8

### C-49 · Wind arrow direction read correctly (was T-06)
**Delivered in:** v3.8.8 · **Origin:** screenshots review, v3.4.0 · **TODO item:** T-06

GSPro's wind arrow is a dart: one tip and two equally sharp barbs, so taking the
sharpest corner as the tip picked a barb — the direction was wrong on all 25
distinct arrows in the saved screenshots (the "roughly reversed" of the review).
The tip is now the outline point farthest from the notch (the deepest dent),
with the point farthest from the centre as the fallback. Right on 25/25, checked
by eye and by an independent isosceles-apex test; the 25 arrows are now test
fixtures. Labels describe the effect (helping, into, left-to-right …) under a
`wind_arrow_points` setting, since GSPro does not document whether the arrow
points to or from the wind (T-36). On 27 real frames only the wind lines changed.

---

## v3.8.7

### C-48 · Cross-check ignores reads GSPro could never display
**Delivered in:** v3.8.7 · **Origin:** found while delivering T-23

Re-reading the 4 Oct screenshots switched character matching off for the panel
font after 3 "disagreements" — all of them Tesseract dropping a decimal or digit
("48" for 4.8, "71" for 7.7, "4" for 0.2, "ft0" for 0ft 0). Since the round score
uses the panel font's characters (C-36), that silently sent the round score back
to Tesseract. The cross-check now tests Tesseract's read against how GSPro
displays each field (AUDIT_PATTERNS: one decimal for VLA, HLA, ball speed and
spin axis; yards or feet-and-inches for distances); learning is unchanged. A
format-valid disagreement ("271.4" vs "27.4") still counts.

### C-47 · Re-read saved screenshots with the current code (was T-23)
**Delivered in:** v3.8.7 · **Origin:** screenshot feature, v3.5.0 · **TODO item:** T-23

`rebuild_from_screenshots.py` replays saved frames in time order through the
current pre-check, reader and engine, and writes a report, database and
dashboard like rebuild_from_log; with `--log` it merges them into the full
history, replacing the log's captures for the sittings the screenshots cover,
and `--log-out` writes a corrected log that reproduces the result. To make the
replay read exactly as the daemon does, the frame reading and the pre-check's
decision moved into `ocr_reader` (`read_frame`, `precheck_says_capture`) and the
default settings into `config_defaults.py`, shared by daemon and tools — the
daemon's log on 27 real frames and rebuild_from_log's output on both real logs
are unchanged. On the 4 Oct screenshots (130 frames): 45 holes, 133 shots, all
five rounds "9 of 9 holes recorded … matching GSPro's total ✓", with no manual
steps; the corrected log rebuilds to the same.

---

## v3.8.6

### C-46 · Each round is checked as it ends (was T-35)
**Delivered in:** v3.8.6 · **Origin:** 5 Oct review · **TODO item:** T-35

When the end-of-round screen appears the console reports the round: holes
recorded, strokes, and whether they match GSPro's total — or which holes were
lost and why (round score unreadable, did not add up, round ended without the
end screen, session ended). Rounds that end without the end screen are reported
too, and three captures in a row without a readable round score raise a live
warning. On the 4 Oct session (as v3.8.5 reads it) all four rounds report
"9 of 9 holes recorded … matching GSPro's total ✓". On 5 Oct's log as v3.8.0
read it, every lost hole would have been named during play, with its reason,
and the warning would have fired seconds after the round score began to fail.

---

## v3.8.5

### C-45 · Screen capture and OCR split out of the daemon (was T-22)
**Delivered in:** v3.8.5 · **Origin:** first review (after v2.7.0) · **TODO item:** T-22

`screen_capture.py` (frames, regions, the monitor offset, DPI awareness) and
`ocr_reader.py` (one region → text, the pre-check read, the end-of-round screen,
Tesseract's console windows) now hold the code and the state they own — the
window offset, the OCR profiles and the character-matching store — and the
daemon sets that state there. `ocr_daemon.py`: 1,998 → 1,506 lines. A pure
refactor, checked two ways: every test passes with its swap-ins pointed at the
owning module, and 27 real frames through the pre-check and capture pipeline
give log content identical to v3.8.4, frame by frame (only the order of lines
written by parallel threads varies — as it does between two runs of v3.8.4).

---

## v3.8.4

### C-44 · Unchanged fields are not read again (was T-13)
**Delivered in:** v3.8.4 · **Origin:** OCR proposal · **TODO item:** T-13

`field_cache.py`: course, hole, par and yardage keep their last reading and its
pixels, and are read again only when a pixel changes by more than 24 of 255.
Measured first on the reviewed session: these regions were pixel-identical
between every two captures of the same hole, and the hole number changed at
every new hole. Replaying its 70 captures: no reading differed from a fresh
read; the course name was read once instead of 70 times (hole 35/70, par 69/70,
yardage 66/70 reused); OCR time per capture 580 → 470 ms. Reused readings are
logged "(unchanged)", which rebuild_from_log reads as before.

---

## v3.8.3

### C-43 · The report reads a snapshot that never holds up captures
**Delivered in:** v3.8.3 · **Origin:** found while delivering T-10

Run in the background, the refresh still read the database under the writer's
lock — at a season's size, one capture was held up 2.1 s. `Database.reading()`
reads a consistent WAL snapshot on its own connection instead: the longest
capture during a refresh fell to 0.18 s (typical 12 ms).

### C-42 · Report refreshed in the background (was T-10)
**Delivered in:** v3.8.3 · **Origin:** review after v3.3.0 · **TODO item:** T-10

`report_worker.py`: a capture asks for a refresh and carries on (0.2 ms, against
a 14.7 s wait after every hole on a 6,000-shot database). Close-together requests
are combined, one arriving mid-refresh gets exactly one follow-up, a steady
stream cannot postpone the report (10 s cap), a failure does not stop the worker,
and exit finishes pending refreshes before the final report. The start-up
refresh also runs in the background. `"report_background": false` restores the
synchronous refresh.

---

## v3.8.2

### C-41 · An existing config.json picks up newer settings
**Delivered in:** v3.8.2 · **Origin:** found while delivering T-28

config.json was used exactly as found, so a setting added in a later version
worked only where the code happened to supply its own fallback — the end-screen
reader had none and would have stayed off for anyone keeping their config.
Missing settings are now filled in from the defaults; nothing set is changed.

### C-40 · The last hole of a round closes from GSPro's end-of-round score (was T-28)
**Delivered in:** v3.8.2 · **Origin:** real-round review · **TODO item:** T-28

The end screen is spotted by a pixel check (150 screenshots: the 5 end screens
and nothing else), its total read with the "SCORE" label checked (5/5 correct:
26, 24, 22, 28, 33). The last hole's strokes = total − (pars of the earlier
holes + the round score at its tee), used only when every earlier hole was seen
and one result fits; the final stroke is then recovered from the next round's
tee frame. On the reviewed 36-hole session: 36/36 holes and 107 strokes, matching
all four end screens — v3.8.0 had recorded 19 holes. The rebuild tool's outcome
tally, which had no place for the new end-of-round outcome, was fixed with it.

---

## v3.8.1 — holes dropped by a lost minus sign

The session: four rounds of Operation 36 (25, 50, 100, 150 yd), 36 holes.
v3.8.0 recorded 19; replayed from the session's own frames, v3.8.1 records 32 —
holes 1–8 of every round, totals matching GSPro exactly.

### C-39 · Lost holes are reported, not silent
**Delivered in:** v3.8.1 · **Origin:** session review

Two holes with no captured shot vanished without a word in the log; such holes
are now reported.

### C-38 · Cross-checks no longer penalise character matching for Tesseract's errors
**Delivered in:** v3.8.1 · **Origin:** session review

"Hole8" vs Tesseract's "Hole" counted as a disagreement; one more and matching
for the HUD font would have been switched off. Reads that are not valid values
for the field no longer count.

### C-37 · A lost minus sign is restored; an unreadable score is filled in
**Delivered in:** v3.8.1 · **Origin:** session review

A bare "6" was taken as +6. It is now "sign unknown": the value that gives a
possible hole result is used (and carried to the next hole); if more than one
would, nothing is guessed. A score unreadable on the tee frame is taken from a
later capture on the same hole. Alone, this recovered 4 of the 17 lost holes
from the log as recorded.

### C-36 · Round score read by character matching (was T-29)
**Delivered in:** v3.8.1 · **TODO item:** T-29

The score font lacked 0, 1, 6 and 8 and was left to Tesseract, which lost the
minus sign on −6, −9 and −1 and misread −7 and −8 — 26 of 76 captures. The score
uses the launch panel's typeface, so it now borrows the panel's digits: 93/93
verified round-score frames read correctly, and all 26 misreads are corrected.

---

## v3.8.0

### C-35 · Visual dashboard (was T-16)
**Delivered in:** v3.8.0 · **Origin:** review after v3.3.0 · **TODO item:** T-16

`gspro_dashboard.html`: weekly strokes-gained trend lines, category bars (this
session against 30 days), club gapping (median, middle half, extremes, gap to
the next club, overlap/large-gap flags) and per-club dispersion (offline against
short/long). One self-contained file of inline SVG — no internet, no scripts —
with hover values and light/dark modes. Built from the same inputs and
calculations as the spreadsheet: the gapping arithmetic was split out of the
Clubs sheet into `reports.gapping_stats` (Clubs sheet byte-identical before and
after) and tests confirm the weekly and gapping figures match the report.
Rewritten with every report refresh, even when the ODS is locked;
`--dashboard` opens it, `rebuild_from_log --dashboard` builds it from a log.
Checked visually on a six-week sample and on your Operation 36 round.

---

## v3.7.0

### C-34 · Each hole's final stroke recovered from the next tee frame (was T-27)
**Delivered in:** v3.7.0 · **Origin:** real-round review · **TODO item:** T-27

The next hole's first frame shows the final stroke's launch data and the last
capture gives its start; the round score says whether it was holed (one stroke
missing) or stopped in the gimmie circle and was conceded (two). Recorded as a
real shot with category, club and launch data; used only when the frame is an
untouched tee, the panel shows a new shot and the length fits, otherwise the old
behaviour applies. On the reviewed round (holes 1–8): Unattributed strokes
13 → 0, Putting 3 → 15 strokes, coverage 10/23 → 23/23; hole scores and
strokes-gained totals identical to v3.6.1 to the third decimal (the conceded
putt takes the value that keeps the hole total exact, so per-stroke rounding
cannot drift it). Every recovery matched the screenshots — e.g. hole 2: a
20 ft 4 in putt that rolled 21 ft 2 in at 7.2 mph and stopped 10 in past.

---

## v3.6.1 — from a real round's screenshots and log

The round: Operation 36, Central Park South, 26 strokes. v3.6.0 recorded 6 holes
(18 strokes), one wrong; with these fixes holes 1–8 match GSPro's card exactly.

### C-33 · Elevation's leading zero read by shape (was T-07)
**Delivered in:** v3.6.1 · **TODO item:** T-07

The HUD font is now complete in the starter set, so character matching reads
the "0" in `0ft 9` from the first launch; the old heuristic is only a fallback.

### C-32 · Starter set from 37 verified frames
**Delivered in:** v3.6.1 · **Origin:** your screenshots

Launch-panel and HUD fonts complete at first launch, including the course
panel's 8 and 9, which the player-panel examples did not match. Leave-one-out:
474/518 correct, 44 handed to Tesseract, 0 wrong. Transcriptions cross-checked
against Tesseract: all 34 disagreements were Tesseract's errors.

### C-31 · Minus sign and dropped decimal points
**Delivered in:** v3.6.1 · **Origin:** real-round review

Spin axis `-4.5` was stored as `-45`: Tesseract dropped the point, and the
character reader took the panel's low minus sign for a dot. The minus is now
recognised by its width, and one-decimal launch fields get a dropped point back.

### C-30 · Remaining-distance region widened
**Delivered in:** v3.6.1 · **Origin:** real-round review

3 of the round's 16 captures cut off the end of the distance (`25ft 11` →
`25ft 1`, `8ft 9` → `8ft`, `15ft 11` → `15ft 1`). K1: 70 → 78 px; 30/30 frames now
correct.

### C-29 · Unfinished holes no longer recorded short
**Delivered in:** v3.6.1 · **Origin:** real-round review

Hole 7 was recorded as a 1 on a par 4 (−3) because it was closed when the next
GSPro round began, with no round score to reconcile; the next round's hole 1
was recorded the same way after a score misread. Such holes are now left out
with a warning.

### C-28 · Holes with no captured shot are reconciled
**Delivered in:** v3.6.1 · **Origin:** real-round review

Hole 1 vanished: its only shot stopped in the gimmie circle and GSPro moved
straight on. Now closed from the round score (E → −2 = 2 strokes).

### C-27 · Pre-check no longer skips new holes
**Delivered in:** v3.6.1 · **Origin:** real-round review

Holes 8 and 9 were never captured: every screen was skipped as "hole ? stroke 1
unchanged". An unreadable hole number counted as unchanged, and only an
*advancing* stroke counter counted as progress, while a new hole resets it to 1.

---

## v3.6.0

### C-26 · Read HUD digits by template matching (was T-03)
**Delivered in:** v3.6.0 · **Origin:** OCR proposal (item 4) · **TODO item:** T-03

Numeric fields are read by matching GSPro's fixed fonts: **0.7 ms per field
against ~80 ms** for Tesseract. On seven real frames, with the starter set, every
field was read correctly (98/98); in leave-one-out testing (frame never seen)
94/98 were correct and 3 safely fell back — the one error was a digit missing
from the set, which the "all ten digits" rule now prevents. Per-capture OCR time
on real frames: 1,548 ms → 612 ms once the panel and HUD fonts are complete
(one field at a time), with all 91 checked fields still correct. Learning was
verified on real data (a removed 9 relearned from Tesseract reads). Found and
fixed during the work: identical sightings of a learned character were being
discarded as duplicates, which would have stopped any character from ever being
confirmed.

---

## v3.5.0

### C-25 · Manual captures release their screen-capture context
**Delivered in:** v3.5.0 · **Origin:** found while adding screenshots

Each Ctrl+Shift+F9 capture runs in its own short-lived thread, and nothing
released that thread's capture context afterwards (only auto-capture did). It
is now released explicitly — the same resource pressure once linked to GSPro
dropping out of fullscreen.

### C-24 · Timestamped screenshots for session review
**Delivered in:** v3.5.0 · **Origin:** your request

Ctrl+Shift+F12 saves the GSPro screen; every-capture mode (Ctrl+Shift+F4 or
`--screenshots`) saves each capture's own frame named by its result, plus the
screens auto-capture skipped, with every file named in the log. Rehearsed on
seven real frames with real OCR: seven 1920×1080 screenshots, correctly named
from `auto_h1_s1_accepted` to `auto_h2_s2_accepted`, including one
`no_progression` frame.

---

## v3.4.0

### C-23 · Stroke ticker reads the highlighted cell wherever it sits
**Delivered in:** v3.4.0 · **Origin:** screenshots review

The ticker read strokes 1 and 2 one too high, which made auto-capture skip real shots. On real frames it went from **2/7 to 7/7** correct, and still passes the original test document.

### C-22 · Auto-capture cooldown starts only after a real capture
**Delivered in:** v3.4.0 · **Origin:** review after v3.3.0

A skipped screen no longer starts the cooldown that swallowed the stroke counter's real update. A test reproduces the missed shot: it fails on v3.3 and passes on v3.4.

### C-21 · Automatic monitor selection
**Delivered in:** v3.4.0 · **Origin:** screenshots review

The daemon follows GSPro to whichever monitor it's on, and never matches its own console window. In the session before this fix, 32 of 44 captures were unreadable because the daemon was on the wrong screen.

### C-20 · Elevation reader rebuilt
**Delivered in:** v3.4.0 · **Origin:** screenshots review

The sign now comes from the triangle's shape and the full feet-and-inches text is read. Correct on **0/7 before, 7/7 after**.

### C-19 · E1 reads wind speed; club and course regions widened
**Delivered in:** v3.4.0 · **Origin:** screenshots review

E1 had sat over the wind speed text while running an arrow detector. The club region was widened from 60 to 80 px and the course region from 123 to 175 px. Course and wind speed went from **0/7 to 7/7**.

### C-18 · "Unattributed" category for strokes the capture missed
**Delivered in:** v3.4.0 · **Origin:** review after v3.3.0

On the log tested, 89% of putting strokes gained came from estimated strokes. One session's Putting figure went from −19.5 to −0.3, with −14.8 now shown as Unattributed. Session totals are identical.

### C-17 · Par-3 tee shots count as Approach
**Delivered in:** v3.4.0 · **Origin:** review after v3.3.0

This follows the PGA Tour definition. Existing rows are relabelled by the format-5 upgrade.

### C-16 · Full-shot launch-data "blanks" diagnosed
**Delivered in:** v3.4.0 · **Origin:** OCR proposal (item 3)

Launch fields read 100% correctly on real frames, and 12 of 12 live captures were exact. The blanks were most likely GSPro's `---` placeholder, which is now counted as "no data" rather than a misread.

---

## v3.3.0

### C-15 · Club gapping and strokes gained by club
**Delivered in:** v3.3.0 · **Origin:** first review (item 6)

A Clubs sheet with median carry, middle 50%, spread, gaps between clubs, and strokes gained per club. Building it uncovered that every shot had been credited to the *next* shot's club, which was also fixed.

---

## v3.2.0

### C-14 · SQLite as the master record
**Delivered in:** v3.2.0 · **Origin:** first review (item 5)

One transaction per capture. The spreadsheet is generated from the database: 34 writes during play instead of roughly 270. First-launch migration and `rebuild --db-out` were included.

---

## v3.1.0

### C-13 · Numbers stored as numbers (report format 3)
**Delivered in:** v3.1.0 · **Origin:** first review (item 4)

Real number, date and time cells, so the report can be charted directly.

### C-12 · Log rotation that keeps replay data
**Delivered in:** v3.1.0 · **Origin:** first review (item 4)

The log archives into `logs/` at 5 MB, and nothing is deleted by default.

### C-11 · Startup region check and `--check-regions`
**Delivered in:** v3.1.0 · **Origin:** first review (item 4)

Hole and par are read once at launch. A warning appears after 8 unreadable captures in a row, and `--check-regions` saves what every region sees.

### C-10 · Capture quality tracked per session
**Delivered in:** v3.1.0 · **Origin:** first review (operational)

Coverage (captured, conceded and estimated strokes) and field read health appear in Round Stats.

---

## v3.0.0

### C-09 · Engine tests for sessions, gimmies and the unfinished-hole discard
**Delivered in:** v3.0.0 · **Origin:** first review (item 3)

Writing the scenario tests found four counting bugs: holes double-counted, repeated hole numbers, round resets read as strokes, and course switches rejected. On the log tested, 130 strokes / 39 holes became 90 / 33, with every stroke backed by a row.

---

## v2.9.0

### C-08 · One shared scoring engine; rebuild tool uses it
**Delivered in:** v2.9.0 · **Origin:** first review (item 2)

The live daemon and the rebuild tool produce byte-identical reports. The rebuild gained the progression check it was missing. The work also found shot and hole rows being overwritten at every session change since v2.2: 91 of 119 shot rows had been lost.

---

## v2.8.0

### C-07 · Atomic writes, backups and damaged-file recovery
**Delivered in:** v2.8.0 · **Origin:** first review (item 1)

This closed three ways an interrupted write could erase the history, the spreadsheet rows, or every sheet but one.

### C-06 · Versioned report upgrades
**Delivered in:** v2.8.0 · **Origin:** your request for an upgradeable report

`--upgrade-ods`, applied automatically at launch with a permanent copy of the original kept.

### C-05 · Dead code removed
**Delivered in:** v2.8.0 · **Origin:** first review (operational)

`write_cell`, the `hole_score` alias, and odfpy imports left over after storage moved out.

---

## v2.7.0

### C-04 · One screen grab per capture
**Delivered in:** v2.7.0 · **Origin:** OCR proposal (item 1)

Every field now comes from the same instant: 1 grab instead of 22.

### C-03 · Parallel OCR
**Delivered in:** v2.7.0 · **Origin:** OCR proposal (item 1)

About 5× faster in simulation (1,711 ms → 320 ms).

### C-02 · Per-field OCR modes and character whitelists
**Delivered in:** v2.7.0 · **Origin:** OCR proposal (item 2)

Removed the border artefacts (`-2.]`, `4]`). Round scores went from 91/107 parseable to 107/107.

### C-01 · Club, lie and course snapping
**Delivered in:** v2.7.0 · **Origin:** OCR proposal (item 2)

Clubs recognised went from 49/120 to 119/120.
