#!/usr/bin/env python3
"""version.py — Version and changelog, shared by the daemon and the tools."""

# ── Build identity ─────────────────────────────────────────────────────────────
# Bump VERSION on any behavioural change. BUILD is stamped at edit time. Both are
# logged at startup and written into every ODS sheet header, so a spreadsheet can
# always be traced back to the code that produced it.
VERSION = "3.8.13"
BUILD   = "2026.10.08.1757"
VERSION_STR = f"v{VERSION} build {BUILD}"

CHANGELOG = [
    ("3.8.13", "T-31: dispersion plot shows left and right — the side estimated from HLA and "
               "spin axis (reports.estimate_side; size of miss r = 0.93 against the measured "
               "offline, side 7/7 against the screen when firm), uncertain points hollow; "
               "verify_sides.py re-checks the estimate on saved screenshots"),
    ("3.8.12", "T-18: capture coverage beside every strokes-gained figure — SG_Summary (current "
               "round, week by week), Round Stats (this session, session log, career total, by "
               "course, week by week, hole by hole, lie and distance-band tables), Clubs and the "
               "dashboard (category legend, each week); under 80% marked low"),
    ("3.8.11", "Exit no longer crashes or loses a capture in progress: no capture can start once "
               "exit begins, a capture already running is finished, and only then are the exit "
               "reports written (the 8 Oct 'cannot schedule new futures after interpreter "
               "shutdown' / 'Fatal Python error'). Wind arrow works with OpenCV 5 as well as 4"),
    ("3.8.10", "T-08: GSPro's LAST SHOT panel read — ball speed and spin kept after a shot "
               "from a penalising lie, recorded with that shot (SG_Shots 'Speed Kept %', "
               "'Spin Kept %'; report format 6) and summarised by lie in Round Stats"),
    ("3.8.9", "T-37: fields read by Tesseract share one run per group of settings — "
              "2.5 Tesseract starts per capture instead of 5.6, OCR 467 → ~270 ms per capture, "
              "readings unchanged. A cross-check disagreement now keeps the matched read: "
              "Tesseract's 7→1 misreads had replaced correct readings (a round score of -7 read as -1)"),
    ("3.8.8", "T-06: wind arrow direction read from the arrow's notch (25/25 real arrows, "
              "was 0/25) and labelled by its effect — helping, into, left-to-right — under a "
              "wind_arrow_points setting (to/from), pending one windy frame to confirm"),
    ("3.8.7", "T-23: rebuild_from_screenshots.py re-reads saved screenshots with the current "
              "pre-check, reader and engine (shared with the daemon: ocr_reader.read_frame) "
              "and can merge them into the full history from the log; the 4 Oct screenshots "
              "give 45 holes, all five rounds matching GSPro. Cross-check no longer counts "
              "Tesseract reads GSPro could never display (it had switched the round score "
              "back to Tesseract)"),
    ("3.8.6", "T-35: every GSPro round is checked as it ends — holes recorded, strokes, "
              "GSPro's end-of-round total, each lost hole with its reason — and a live "
              "warning when the round score keeps failing"),
    ("3.8.5", "T-22: screen capture and OCR split out of ocr_daemon.py into "
              "screen_capture.py and ocr_reader.py (daemon 1,998 → 1,506 lines); no change "
              "in behaviour — identical log content on 27 real frames"),
    ("3.8.4", "T-13: course, hole, par and yardage re-read only when their pixels change "
              "(field_cache.py) — the course name's Tesseract read now happens once per "
              "round; OCR time per capture −19% on a real session, no reading changed"),
    ("3.8.3", "T-10: report and dashboard refreshed in the background (report_worker.py) — "
              "a capture never waits for them; close-together refreshes combined; the "
              "report reads a WAL snapshot without the writer's lock; start-up refresh in "
              "the background; exit finishes pending refreshes first"),
    ("3.8.2", "T-28: GSPro's end-of-round score read from its end screen (pixel check, then "
              "total + 'SCORE' label) and used to close the round's last hole, with final-"
              "stroke recovery from the next round's tee; config.json picks up newer settings "
              "from the defaults"),
    ("3.8.1", "Dropped holes: the round score's minus sign was lost ('-6' read as '6' = +6), "
              "so holes were discarded. Score now read with the launch panel's digits (93/93 "
              "real frames); a bare number is no longer +N, the sign is restored when only "
              "one value fits; a score unreadable on the tee is filled from later captures; "
              "invalid Tesseract reads no longer count against character matching"),
    ("3.8.0", "T-16: HTML dashboard (dashboard.py) — weekly strokes-gained trends, "
              "category bars, club gapping, per-club dispersion; self-contained inline SVG; "
              "rewritten with every report refresh, --dashboard to open, rebuild --dashboard"),
    ("3.7.0", "T-27: each hole's final stroke recovered from the next hole's tee frame "
              "(launch data + known start; holed or stopped in the gimmie circle, from the "
              "round score) instead of Unattributed; coverage counts recovered strokes"),
    ("3.6.1", "From a real 9-hole round's screenshots and log: pre-check no longer skips "
              "new holes it cannot read (holes 8-9 lost); holes with no captured shot "
              "reconciled (hole 1 lost); unfinished holes no longer recorded short (false "
              "albatross); K1 region widened; minus sign and dropped decimals fixed; "
              "starter set from 37 verified frames"),
    ("3.6.0", "Numeric HUD fields read by matching GSPro's fixed fonts (T-03): starter "
              "set from real frames, missing characters learned during play, a font "
              "used only once all ten digits are known, periodic Tesseract cross-check"),
    ("3.5.0", "Timestamped full-screen screenshots: hotkey (Ctrl+Shift+F12) and "
              "every-capture mode (Ctrl+Shift+F4 / --screenshots) saving each capture's "
              "frame named by its result, plus screens auto-capture skipped; manual "
              "captures release their screen-capture context"),
    ("3.4.0", "Stroke ticker and elevation readers rebuilt and verified on real frames; "
              "auto-capture no longer starts its cooldown on a skip; automatic monitor "
              "selection; Unattributed category for missed strokes; par-3 tee shots are "
              "Approach; E1 reads wind speed; wider club/course regions; '---' counted as "
              "no data; report format 5"),
    ("3.3.0", "FIXED: shots were credited to the club shown after them (the next "
              "shot's club). New Club column (report format 4, with upgrade of ODS and "
              "database rows); Clubs sheet with gapping and strokes gained by club; "
              "one shared builder for every generated sheet"),
    ("3.2.0", "SQLite master record (gspro_stats.db): one transaction per capture; "
              "report regenerated per hole/at exit/--export-report; first-launch "
              "migration of state, history and report rows; rebuild --db-out"),
    ("3.1.0", "Report format 3: numbers, dates and times stored as typed cells; "
              "log archiving that never deletes replay data; startup region check "
              "and --check-regions; capture quality and field read health per session"),
    ("3.0.0", "Hole lifecycle: each hole finalised exactly once; repeated hole "
              "numbers and course switches start fresh holes; round-score "
              "reconciliation skipped across GSPro round resets and capped; "
              "holes finished on the last capture are kept at exit"),
    ("2.9.0", "Scoring engine shared with rebuild_from_log.py (scoring/parsing/reports); "
              "injectable clock; dated log lines. FIXED: SG_Shots/SG_Holes rows "
              "overwritten at each mid-run session boundary (since v2.2); fresh "
              "rounds sharing statistics containers"),
    ("2.8.0", "Crash-safe storage: atomic writes, rotating backups, damaged-file "
              "recovery; row positions re-derived from the report; versioned "
              "report upgrades (--upgrade-ods); report format 2"),
    ("2.7.0", "Single-frame capture + parallel OCR; per-field Tesseract profiles "
              "and whitelists; club/lie/course snapping; round-score repair"),
    ("2.6.0", "Green distances reported and evaluated in feet throughout; green "
              "baseline stored natively in feet; putting short/long now evaluated; "
              "gimmie radius configured in feet"),
    ("2.5.0", "Console window follows GSPro across monitors without taking focus"),
    ("2.4.0", "Short/long + offline geometry per shot and per club; "
              "parse_distance handles feet-inches (was 3x wrong on short shots)"),
    ("2.3.0", "Gimmie-zone shots scored on actual remaining distance; session "
              "tracking; weekly stats always rebuilt on exit; version stamping"),
    ("2.2.0", "Per-session/per-course boundaries; session + week trend reporting"),
    ("2.1.0", "Distance-band statistics derived from baseline table knots"),
    ("2.0.0", "Per-hole normalisation; incomplete-hole discard; round history"),
]
