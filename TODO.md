# TODO — suggested improvements not yet done

Every improvement that has been suggested but not implemented. When an item
is finished it moves to **COMPLETED.md**, with the version that delivered it
and what it achieved. IDs are permanent, so an item keeps its number when it
moves.

**Priority:** High = affects the accuracy or reliability of numbers you
already see · Medium = clear value · Low = nice to have.
**Effort:** S = an hour or so · M = an iteration · L = more than one.

Last updated: v3.8.13

---

## Recommended order

1. **T-32** Upgrading keeps your data, and an old copy says so
2. **T-01** Separate practice sessions from real rounds
3. **T-11** Faster report writer
4. **T-02** Built-in OCR accuracy check using debug shots
5. **T-17, T-19** Confidence figures, Round Stats split
6. **T-26** Match lie, club and wind speed by shape too

---

## Accuracy

### T-01 · Separate practice sessions from real rounds
**Priority:** High · **Effort:** M · **Origin:** review after v3.3.0

Distance-challenge and practice sessions are mixed into your real statistics:

- 25 of 39 "Off Tee" shots on the log tested came from under 100 yards. The tee baseline there is an estimate, because the source table starts at 100 yards.
- One Rock Bay session reads −35 vs par, because its "par 4s" are 25–150 yards.

Those figures then flow into the weekly trends alongside real rounds.

**To do**
- Recognise practice sessions from their yardages (for example, par 4s under 250 yd, or fixed 25/50/100/150 yd stations).
- Report them separately.
- Categorise their shots by distance rather than by tee.
- Keep them out of vs-par, vs-tour and weekly comparisons.

The same detection can flag partial wedge swings, so club gapping uses full swings only.

**Evidence (v3.6.1 review):** the reviewed round was GSPro's **Operation 36**
mode — nine 25-yd par 4s at level 1, 50-yd holes at level 2 — which is short-game
practice by design and should be recognised as such.

### T-02 · Built-in OCR accuracy check using debug shots
**Priority:** High · **Effort:** S–M · **Origin:** screenshots review, v3.4.0

Every Ctrl+Shift+F8 debug shot has known launch values: VLA 25.0, HLA 0.0, ball speed 100.0, spin axis 0.0, carry (raw) 133.6. You fired 27 in the last session. A manual check found 12 of 12 live captures exact.

**To do**
- After a debug shot, compare the next capture's launch fields with the known values automatically.
- Record the result in the capture-quality counters and show it in Round Stats as *verified* accuracy, alongside the blank and unreadable rates.
- Have the rebuild tool apply the same check from the "debug shot" lines in the log.

### T-04 · Verify the over-par stroke reading
**Priority:** Medium · **Effort:** S · **Origin:** found while testing v3.4.0

When strokes exceed par, GSPro shows the stroke number in a red cell, and that cell is read by OCR. On the original ticker test document, image 7 reads **17**. Earlier testing only confirmed that image 7 is routed to the red-cell path, not what number it shows.

**To do**
- Check the image, and fix the reading if 17 is wrong.
- Add real over-par frames to `test_fixtures/`.

### T-05 · Optional amateur strokes-gained baseline
**Priority:** Medium · **Effort:** M · **Origin:** review after v3.3.0

Strokes gained is measured only against PGA Tour players, so every category reads negative and progress is hard to see.

**To do**
- Find a published amateur baseline (scratch or handicap-based) with a usable data source.
- Offer it as a selectable comparison.

Verify the data source before building on it.

### T-09 · Multiplayer capture
**Priority:** Low (until needed) · **Effort:** L · **Origin:** first session (design only, not to be implemented yet)

All round state is per daemon, not per player, so a group round would mix players' shots.

**To do**
- Read the active player's name.
- Keep state per player.
- The simplest useful version records only your own shots and ignores the others.

---

## Speed

### T-11 · Faster report writer
**Priority:** Medium · **Effort:** S · **Origin:** review after v3.3.0

A prototype that writes the ODS XML directly took **0.31 s against 11.85 s** (39× faster) for 6,000 shots. Its output was verified to read back identically, including quotes, signs and special characters.

**To do:** replace the odfpy writer in `storage.ods_save`, keeping the typed cells and atomic writes. odfpy stays for reading.

**Since v3.8.3 (C-42)** the refresh runs in the background, so a slow writer no
longer delays captures — it only decides how long the report lags behind play:
≈17 s after a hole at a season's 6,000 shots. The direct-XML writer would make
that well under a second.

### T-12 · Make the raw OCR sheet optional in the report
**Priority:** Medium · **Effort:** S · **Origin:** review after v3.3.0

The database now holds the raw record, so Sheet1 in the spreadsheet is a duplicate. Dropping it roughly halves the report's size and write time.

**To do:** add a config option `include_raw_sheet`. Consider defaulting it to off once T-11 is done.

### T-14 · GPU screen capture (DXGI) and optional continuous capture
**Priority:** Low · **Effort:** M · **Origin:** OCR proposal (item 5)

The `dxcam` library captures on the GPU. It's faster than the current GDI capture and less likely to disturb a fullscreen game, and it would make continuous capture, with voting across several frames, practical.

**Deep dive (v3.8.10): continuous capture and voting would not improve accuracy.**
Voting helps only if frames of the same moment differ. Measured on the 4 Oct
screenshots — 14 captures with a later frame of the same moment, 1.9–17 s apart:
every field used for scoring (hole, par, yardage, remaining, lie, score, club,
course, wind speed, the whole launch panel) was pixel-identical in all 14 pairs,
so voting would return the same reading — and the same misread — every time.
Of the nine misreads found across the reviews, none had pixels that varied
between frames: all were systematic (reader, region or logic) and were fixed
at the source. The only fields that changed were the wind arrow (12/14 — it is
drawn relative to the camera, which turns after the ball stops; see T-39) and
the lie angles (2/14 — not yet drawn at capture; see T-38), and neither is
helped by voting. Speed gain: ~5–8% of a capture (v3.8.8 estimate).

**Recommendation:** keep Low; do it only if GDI capture is seen to disturb
GSPro in fullscreen. Do not build voting.


### T-15 · Keep the Tesseract engine in memory
**Priority:** Low · **Effort:** S–M · **Origin:** OCR proposal

`tesserocr` avoids starting a new process for every read, but it's awkward to install on Windows. T-03 delivers most of the same benefit.

---

## Reporting

**Since v3.8.9 (C-50):** one Tesseract run per group of settings removed most of
the start-up cost without `tesserocr`; this would remove the last two starts.

### T-17 · Confidence on every average
**Priority:** Medium · **Effort:** S · **Origin:** review after v3.3.0

Show ± a standard error on strokes gained per category and per club, and flag figures from too few shots — in the report and as error bars on the dashboard's category bars (C-35). 27 lob-wedge shots support a conclusion; 7 eight-iron shots barely do.

### T-19 · Split Round Stats into separate sheets
**Priority:** Medium · **Effort:** S · **Origin:** review after v3.3.0

Round Stats has grown to about 134 rows of stacked sections. Separate **Sessions**, **Trends** and **This Session** sheets would make it readable.

### T-20 · Strokes gained by hole for courses you replay
**Priority:** Low · **Effort:** S · **Origin:** review after v3.3.0

Show which holes cost you most on courses you play more than once.

---

## Maintenance

### T-21 · Validate the config at startup
**Priority:** Medium · **Effort:** S · **Origin:** first review (after v2.7.0)

Duplicate cells, conflicting hotkeys and misspelt keys currently fail silently.

**To do:** check them at launch and stop with a clear message.

### T-24 · Link each shot row to its screenshot
**Priority:** Low · **Effort:** S · **Origin:** screenshot feature, v3.5.0

Add the screenshot file name to SG_Shots (and the database), so a surprising row
in the report leads straight to the frame behind it. Needs a report-format step.

### T-25 · Keep the screenshots folder in check
**Priority:** Low · **Effort:** S · **Origin:** screenshot feature, v3.5.0

A long PNG session can use several hundred MB. An optional limit — keep the last
N days or N GB — would stop the folder growing unattended.

### T-26 · Match lie, club and wind speed by shape too
**Priority:** Low · **Effort:** S–M · **Origin:** character matching, v3.6.0

After character matching, most of a capture's remaining OCR time is free text:
course, lie, club and lie angles, plus wind speed (~70–100 ms each). Lie and
club come from small fixed lists, so whole words could be matched by shape the
same way; wind speed ("0 MPH") could join a font. That would leave Tesseract only
for the course name and lie angles.

---

## From the real-round review (added with v3.6.1)

**Since v3.8.4 (C-44)** course, hole, par and yardage are re-read only when their
pixels change, so the remaining per-capture OCR time is these free-text fields.
The same pixel test could also skip re-reading any of them whose region did not
change (a club or lie repeated from the last capture).

**Since v3.8.9 (C-50):** these fields share one Tesseract run; matching them by
shape would remove them from Tesseract altogether.

### T-32 · Upgrading keeps your data, and an old copy says so
**Priority:** High · **Effort:** S–M · **Origin:** 5 Oct review

Each package was unzipped into its own folder (`gspro_ocr_daemon_v3.8.0\…`), and
the shortcut kept launching v3.8.0 — so none of the fixes since were running.
Each folder also holds its own database, so starting the new copy would leave
the history behind.

**To do**
- At start-up, if a newer package sits in a sibling folder, say so in the
  banner ("v3.8.5 is in ..\gspro_ocr_daemon_v3.8.5 — you are running v3.8.0").
- On first launch of a new copy with no database, offer to bring over the
  database, learned characters, log and config from the previous version's
  folder.
- Or keep data outside the program folder (a `data_dir` setting), so a new
  version picks it up wherever it is unzipped.

### T-36 · Confirm which way GSPro's wind arrow points
**Priority:** Low · **Effort:** S · **Origin:** T-06 (v3.8.8)

The arrow's direction is now read correctly (25/25), but whether it points where
the wind blows *to* or comes *from* is not settled: GSPro's guide does not say,
and every frame reviewed was at 0 mph. The default is "to".

**To do:** with screenshot mode on, play one hole with a crosswind of 10 mph or
more and a straight shot. If the ball drifts the way the arrow points, "to" is
right; if against it, set `wind_arrow_points` to "from" (and change the default).
GSPro's cardinal wind direction, shown beside the arrow when wind is on, is a
second check against the hole's orientation.

### T-38 · Second look at fields GSPro draws late
**Priority:** Low · **Effort:** S · **Origin:** T-14 deep dive (v3.8.10)

GSPro draws the lie angles 2–3 s after the ball stops, later than the stroke
counter the watcher waits on, so 2 of 14 checked captures read a blank panel
(as "ee"). **To do:** (a) treat a field whose crop is blank (no text pixels) as
empty rather than letting Tesseract read noise; (b) if a field that should
have a value is empty, re-read just that field from a frame ~3 s later — the
watcher's camera-move triggers already grab such frames — and update the
capture. Lie angles are not used in scoring, so this is data quality only.

### T-39 · Read the wind arrow from the address view
**Priority:** Low (until wind is on) · **Effort:** S · **Origin:** T-14 deep dive (v3.8.10)

The wind arrow is drawn relative to the camera. At capture the camera often
still faces the shot just played; seconds later it turns to face the next
shot, and the arrow turns with it — by about 180° in 9 of 14 checked pairs. The
reading at capture is therefore relative to neither shot. **To do:** take the
wind direction from the last frame before the next shot (the address view the
pre-check already sees on camera moves) and record it with that next shot.
Pairs with T-36 (whether the arrow points to or from the wind); irrelevant at
0 mph, which is every round so far.

### T-40 · Check left/right on longer clubs
**Priority:** Low · **Effort:** S · **Origin:** T-31 (v3.8.13)

The side estimate was checked on screen only with 25–50 yd wedges (7 of 7). Its
size of miss also fits the five 118–151 yd 8-iron shots of 4 Oct, but no
longer shot's side has been seen on screen. **To do:** run `verify_sides.py` on
a session with mid and long irons and the driver (the 4 Oct 150-yd round's
screenshots in the v3.8.0 folder would do), and refit `SIDE_CURVE` if the firm
estimates miss.
