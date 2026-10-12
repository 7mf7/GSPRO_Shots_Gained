#!/usr/bin/env python3
"""
verify_sides.py — Check the left/right estimate against the screen (T-31, v3.8.13).

The dashboard's dispersion plot puts each shot left or right of the line from
its launch data (reports.estimate_side). This tool checks that estimate
against what GSPro showed. In the ball-camera view saved while a shot settles
(…_settle_skipped.png, …_settle_cooldown.png) the ball sits at the centre of
the screen and the camera looks along the ball's path, so a pin still ahead
and LEFT of centre means the ball finished RIGHT of the line, and vice versa.

    python verify_sides.py screenshots\\2026-10-08
    python verify_sides.py screenshots --report ocr_output.ods

Each settle frame is matched to its shot by the launch data on its panel (HLA
and spin axis) and the date. Frames where the camera already faces the pin
(the next address view) say nothing about the side and are skipped. Nothing is
written. Run it after switching hands, or once you have shots with longer
clubs, to see whether the estimate still holds.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import numpy as np                       # noqa: E402
from PIL import Image                    # noqa: E402

import ocr_reader                        # noqa: E402
import rebuild_from_log as rb            # noqa: E402
import rebuild_from_screenshots as rs    # noqa: E402
import reports                           # noqa: E402
import storage                           # noqa: E402

NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})-\d{3}_settle_\w+\.(png|jpe?g)$", re.I)
CENTRE_MARGIN = 25          # px at 1920 wide: a pin this close to centre gives no side


# ── what the screen shows ─────────────────────────────────────────────────────

def ball_camera(im: Image.Image) -> bool:
    """The ball-camera view: the ball's navy distance box sits just right of centre
    (about 12% of this patch; 0% in the address view)."""
    sx, sy = im.width / 1920, im.height / 1080
    a = np.asarray(im.convert("RGB")).astype(int)[int(700 * sy):int(1000 * sy), int(962 * sx):int(1080 * sx)]
    navy = (abs(a[..., 0] - 30) < 22) & (abs(a[..., 1] - 42) < 22) & (abs(a[..., 2] - 66) < 26)
    return a.size > 0 and float(navy.mean()) > 0.05


def flag_base_x(im: Image.Image) -> float | None:
    """x of the flagstick's base: the tallest thin light shape in the 3D view
    (it leans with the perspective, so this works on shapes, not columns)."""
    import cv2
    sx, sy = im.width / 1920, im.height / 1080
    y0, x0 = int(90 * sy), int(280 * sx)
    view = np.asarray(im.convert("RGB")).astype(int)[y0:int(1000 * sy), x0:int(1560 * sx)]
    light = ((view.min(axis=2) > 150) & (view.max(axis=2) - view.min(axis=2) < 60)).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(light, connectivity=8)
    best = None
    for k in range(1, n):
        _, _, w, h, area = st[k]
        if h >= 60 * sy and w <= 45 * sx and h >= 3 * w and area <= h * 12:
            if best is None or h > best[0]:
                ys, xs = np.nonzero(lab == k)
                best = (h, float(xs[ys >= ys.max() - 3].mean()) + x0)
    return None if best is None else best[1]


def side_seen(im: Image.Image) -> tuple[int | None, str]:
    """(+1 right / -1 left / 0 on the line, why) — None when the frame cannot say."""
    if not ball_camera(im):
        return None, "address view (camera faces the pin)"
    fx = flag_base_x(im)
    if fx is None:
        return None, "flagstick not found"
    centre, margin = im.width / 2, CENTRE_MARGIN * im.width / 1920
    if fx < centre - margin:
        return +1, "pin left of centre → ball right of the line"
    if fx > centre + margin:
        return -1, "pin right of centre → ball left of the line"
    return 0, "pin at the centre → on the line"


# ── matching frames to shots ──────────────────────────────────────────────────

def match_shot(shots: list[dict], date: str, when: datetime, hla: float, axis: float) -> dict | None:
    """The shot with this panel's HLA and spin axis, recorded at or soon after the frame."""
    cands = []
    for s in shots:
        if s.get("date") != date or s.get("hla") is None or s.get("axis") is None:
            continue
        if abs(s["hla"] - hla) > 0.05 or abs(s["axis"] - axis) > 0.05:
            continue
        try:
            t = datetime.strptime(f'{s["date"]} {s["time"]}', "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        gap = (t - when).total_seconds()
        if -2 <= gap <= 180:
            cands.append((gap, s))
    return min(cands, key=lambda c: c[0])[1] if cands else None


def load_shots(report: Path | None, database: Path | None) -> list[dict]:
    if database:
        import db
        d = db.Database(database)
        rows = d.sheet_rows("SG_Shots")
        d.conn.close()
    else:
        rows = storage.ods_read(report)["SG_Shots"]
    return reports._shots_from_rows(rows)[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", type=Path, nargs="+", help="screenshot folder(s), searched recursively")
    ap.add_argument("--report", type=Path, default=HERE / "ocr_output.ods", help="report with the shots (default beside this script)")
    ap.add_argument("--db", type=Path, default=None, help="read the shots from a database instead")
    ap.add_argument("--config", type=Path, default=None)
    a = ap.parse_args()
    logging.basicConfig(level=logging.ERROR, format="%(message)s")

    shots = load_shots(a.report, a.db)
    config = rb._load_config(a.config)
    frames = sorted(f for p in a.folders for f in (p.rglob("*") if p.is_dir() else [p]) if NAME.match(f.name))
    if not frames:
        sys.exit("No settle frames found (…_settle_skipped.png / …_settle_cooldown.png). "
                 "Turn on screenshot mode (Ctrl+Shift+F4) for a session first.")
    tally = {"firm": [0, 0], "weak": [0, 0]}
    skipped = 0
    with tempfile.TemporaryDirectory() as td:
        reader = rs.setup_reader(config, HERE / "digit_templates.json", Path(td))
        for f in frames:
            m = NAME.match(f.name)
            when = datetime.strptime(f"{m[1]} {m[2]}:{m[3]}:{m[4]}", "%Y-%m-%d %H:%M:%S")
            im = Image.open(f).convert("RGB")
            read = {c: ocr_reader.ocr_image(reader.regions[c], reader._crop(im, c), reader.lang)[0]
                    for c in ("M1", "R1") if c in reader.regions}
            try:
                hla, axis = float(read.get("M1", "")), float(read.get("R1", ""))
            except ValueError:
                skipped += 1
                continue
            seen, why = side_seen(im)
            s = match_shot(shots, m[1], when, hla, axis)
            if seen is None or s is None:
                skipped += 1
                print(f"  {f.name[:23]}  HLA {hla:+.1f} axis {axis:+.1f}: "
                      f"{why if seen is None else 'no matching shot in the report'}")
                continue
            kind = "firm" if s["side_firm"] else "weak"
            verdict = ""
            if seen != 0 and s["side"] != 0:
                ok = seen == s["side"]
                tally[kind][0 if ok else 1] += 1
                verdict = "✓" if ok else "✗"
            print(f"  {f.name[:23]}  {s['club']:<8} hole {s['hole']:>2}  predicted "
                  f"{s['side_pred']:+.1f} {s['bias_unit'] or 'yd'} ({kind}), "
                  f"measured offline {s['offline'] if s['offline'] is not None else '—'}  —  {why}  {verdict}")
    (fa, fd), (wa, wd) = tally["firm"], tally["weak"]
    print(f"\nFirm estimates (predicted miss ≥ {reports.SIDE_FIRM_YD} yd): {fa} right, {fd} wrong")
    print(f"Too close to call:                         {wa} right, {wd} wrong")
    print(f"Frames that could not say:                 {skipped}")


if __name__ == "__main__":
    main()
