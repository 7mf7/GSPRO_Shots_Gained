#!/usr/bin/env python3
"""
arrow_detector.py — OpenCV contour-based direction detection for GSPro.

GSPro renders wind direction as a ROTATABLE CURSOR-STYLE ARROW (like a GPS
navigation indicator) that can point to any of 8 compass directions.
Tesseract cannot read graphical symbols; pixel-mass halving fails for rotated
shapes.  This module uses contour analysis instead:

  1. Isolate the arrow foreground (color-based for the blue GSPro arrow,
     with a grayscale Otsu fallback for other colours).
  2. Find the largest contour and approximate it to its polygon vertices.
  3. Identify the TIP vertex — the one with the SMALLEST interior angle
     (sharpest point).  Farthest-from-centroid is used as backup.
  4. Compute atan2(centroid → tip) and map to an 8-point compass.

Height-delta regions use the same contour approach on the left strip (arrow)
and Tesseract restricted to digits on the right strip (number).

Public API
----------
detect_wind_direction(pil_img, debug_path=None)  → "N (helping)" / "E (left-to-right)" / …
detect_height_delta(pil_img, debug_path=None)    → "+15" / "-8" / …
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False
    log.warning("OpenCV not installed — arrow detection disabled.  "
                "Install with:  pip install opencv-python")

try:
    import pytesseract as _tsr
    _TSR = True
except ImportError:
    _TSR = False

# ── constants ──────────────────────────────────────────────────────────────────

# Minimum foreground pixels (after upscale) before declaring the region blank.
_MIN_PX = 80

# Upscale so the shortest image side is at least this many pixels.
# Small regions (e.g. 43×28) need to be enlarged for reliable contour detection.
_MIN_SIDE = 120

# GSPro blue wind arrow: HSV hue range (OpenCV uses 0-180).
_BLUE_LO  = np.array([ 90,  60,  60])
_BLUE_HI  = np.array([140, 255, 255])

# GSPro green height-delta arrow (positive / uphill ▲): HSV hue range.
# Covers yellow-green → green → teal to catch any shade of 'game green'.
_GREEN_LO = np.array([ 40,  60,  60])
_GREEN_HI = np.array([ 90, 255, 255])

# GSPro red/orange height-delta arrow (negative / downhill ▼).
# Red wraps in OpenCV HSV — two bands needed.
_RED_LO1  = np.array([  0, 100,  80])
_RED_HI1  = np.array([ 12, 255, 255])
_RED_LO2  = np.array([158, 100,  80])
_RED_HI2  = np.array([180, 255, 255])

# What the wind does to the ball, for each direction the wind TRAVELS on screen
# (up = away from the player, along the target line) — T-06, v3.8.8.
_EFFECT: dict[str, str] = {
    "N":   "helping",            "NNE": "helping, slightly left-to-right",
    "NE":  "helping, left-to-right", "ENE": "left-to-right, slightly helping",
    "E":   "left-to-right",      "ESE": "left-to-right, slightly into",
    "SE":  "into, left-to-right", "SSE": "into, slightly left-to-right",
    "S":   "into",               "SSW": "into, slightly right-to-left",
    "SW":  "into, right-to-left", "WSW": "right-to-left, slightly into",
    "W":   "right-to-left",      "WNW": "right-to-left, slightly helping",
    "NW":  "helping, right-to-left", "NNW": "helping, slightly right-to-left",
}

# Which way GSPro's arrow points: "to" = where the wind blows (the usual
# convention for wind arrows), "from" = where it comes from. GSPro's guide says
# only that the arrow is relative to the player's orientation, and every frame
# reviewed so far was at 0 mph — set from config "wind_arrow_points".
WIND_ARROW_POINTS = "to"


def wind_label(arrow: str) -> str:
    """'N' (arrow pointing up) → 'N (helping)' under "to", 'N (into)' under "from"."""
    if arrow not in _EFFECT:
        return arrow
    travel = arrow
    if WIND_ARROW_POINTS == "from":
        travel = _COMPASS_16[(_COMPASS_16.index(arrow) + 8) % 16]
    return f"{arrow} ({_EFFECT[travel]})"


# 16 directions, 22.5° each, starting from E (0°) going counter-clockwise.
_COMPASS_16 = [
    "E", "ENE", "NE", "NNE",
    "N", "NNW", "NW", "WNW",
    "W", "WSW", "SW", "SSW",
    "S", "SSE", "SE", "ESE",
]

# ── helpers ────────────────────────────────────────────────────────────────────

def _pil_to_bgr(img: Image.Image) -> "np.ndarray":
    return cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)


def _upscale(bgr: "np.ndarray") -> "np.ndarray":
    """Upscale small images so contour detection is reliable."""
    h, w = bgr.shape[:2]
    short = min(h, w)
    if short >= _MIN_SIDE:
        return bgr
    scale = int(np.ceil(_MIN_SIDE / short))
    return cv2.resize(bgr, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)


def _isolate_foreground(bgr: "np.ndarray") -> "np.ndarray":
    """
    Return a binary mask (255 = arrow, 0 = background).

    Strategy
    --------
    1. Try HSV colour isolation for the GSPro blue arrow.
    2. If too few blue pixels are found, fall back to Otsu grayscale threshold.
    3. Apply morphological close to fill small gaps.
    """
    hsv   = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    blue  = cv2.inRange(hsv, _BLUE_LO,  _BLUE_HI)
    green = cv2.inRange(hsv, _GREEN_LO, _GREEN_HI)
    colour = cv2.bitwise_or(blue, green)

    # Use colour mask if it covers at least 5 % of the image
    threshold = bgr.shape[0] * bgr.shape[1] * 0.05
    if np.count_nonzero(colour) > threshold:
        mask = colour
    else:
        # Grayscale Otsu fallback (handles white/yellow/other arrow colours)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if np.mean(mask) > 127:          # light background → invert
            mask = cv2.bitwise_not(mask)

    # Close small gaps inside the arrow body
    k = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
    return mask


def _tip_and_back(cnt: "np.ndarray", cx: float, cy: float) -> tuple[tuple, tuple]:
    """
    The tip and the back of GSPro's wind arrow (T-06, v3.8.8).

    The arrow is a dart (➤): one tip, two equally sharp barbs at the back and a
    notch between them, so "the sharpest corner" picks a barb about as often as
    the tip — the arrow was read roughly reversed in 7 of 7 real frames. The
    notch is unambiguous: it is the deepest dent in the outline, and the tip is
    the outline point farthest from it. Without a clear notch (a blurred or
    dimmed arrow) the tip is the point farthest from the centre.
    """
    pts = cnt.reshape(-1, 2).astype(float)
    size = max(1.0, float(np.sqrt(cv2.contourArea(cnt))))
    try:
        hull = cv2.convexHull(cnt, returnPoints=False)
        defects = cv2.convexityDefects(cnt, hull) if hull is not None and len(hull) > 3 else None
    except cv2.error:
        defects = None
    if defects is not None and len(defects):
        # (N, 1, 4) in OpenCV 4, (N, 4) in OpenCV 5 — accept either (v3.8.11)
        _, _, f, depth = max(defects.reshape(-1, 4), key=lambda d: d[3])
        if depth / 256.0 >= 0.08 * size:                     # a real notch
            back = pts[f]
            d = np.hypot(pts[:, 0] - back[0], pts[:, 1] - back[1])
            return tuple(pts[d.argmax()]), tuple(back)
    d = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
    return tuple(pts[d.argmax()]), (cx, cy)


def _angle_to_compass(dx: float, dy_screen: float) -> str:
    """
    Convert a centroid→tip displacement to a 16-point compass string.

    Coordinate system
    -----------------
    dx        : positive = right  (East)
    dy_screen : positive = down in image  (South in screen coords)

    We negate dy_screen so that 'up in the image' maps to North,
    then use atan2 which returns 0° = East, 90° = North.

    The 360° circle is divided into 16 sectors of 22.5° each:
    E, ENE, NE, NNE, N, NNW, NW, WNW, W, WSW, SW, SSW, S, SSE, SE, ESE
    """
    angle  = np.degrees(np.arctan2(-dy_screen, dx))   # 0=E, 90=N, 180=W, -90=S
    norm   = (angle + 360) % 360                       # normalise to 0–360
    sector = int((norm + 11.25) / 22.5) % 16          # 22.5° bins
    return _COMPASS_16[sector]


def _detect_direction(mask: "np.ndarray") -> str:
    """
    Full direction detection from a binary foreground mask.
    Returns a raw compass string or "unknown".
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return "unknown"

    cnt = max(contours, key=cv2.contourArea)
    if cv2.contourArea(cnt) < _MIN_PX:
        return "unknown"

    M = cv2.moments(cnt)
    if M["m00"] == 0:
        return "unknown"
    cx = M["m10"] / M["m00"]
    cy = M["m01"] / M["m00"]

    (tx, ty), (bx, by) = _tip_and_back(cnt, cx, cy)
    direction = _angle_to_compass(tx - bx, ty - by)

    log.debug(
        "arrow_detector: centroid=(%.1f,%.1f) tip=(%.1f,%.1f) → %s",
        cx, cy, tx, ty, direction,
    )
    return direction


def _save_debug(
    bgr_up: "np.ndarray",
    mask: "np.ndarray",
    direction: str,
    path: Optional[Path],
) -> None:
    if path is None or not _CV2:
        return
    try:
        vis = bgr_up.copy()
        # Overlay mask as semi-transparent green
        green = np.zeros_like(vis)
        green[mask > 0] = (0, 200, 0)
        vis = cv2.addWeighted(vis, 0.7, green, 0.3, 0)
        # Burn in detected label
        cv2.putText(vis, direction, (4, vis.shape[0] - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)
        cv2.imwrite(str(path), vis)
    except Exception as exc:
        log.debug("arrow_detector: debug save failed — %s", exc)


# ── public API ─────────────────────────────────────────────────────────────────

def detect_wind_direction(
    img: Image.Image,
    debug_path: Optional[Path] = None,
) -> str:
    """
    Detect the compass direction of the GSPro wind arrow.

    Returns a human-readable label such as "NW (Head+Left)".
    Returns "" on failure or if OpenCV is unavailable.
    """
    if not _CV2:
        return ""
    try:
        bgr   = _pil_to_bgr(img)
        bgr   = _upscale(bgr)
        mask  = _isolate_foreground(bgr)

        direction = _detect_direction(mask)
        _save_debug(bgr, mask, direction, debug_path)

        if direction == "unknown":
            log.warning("arrow_detector: wind direction undetected (blank or ambiguous)")
            return ""

        label = wind_label(direction)
        log.info("arrow_detector: wind → %s", label)
        return label

    except Exception as exc:
        log.error("arrow_detector: wind detection failed — %s", exc)
        return ""


def _colour_sign(arrow_bgr: "np.ndarray") -> str:
    """
    Determine elevation sign from pixel colour.

    Green pixels  → "+"  (uphill ▲)
    Red pixels    → "−"  (downhill ▼)
    Ambiguous     → ""   (caller should fall back to contour direction)

    The two-triangle layout in GSPro shows both colours; the active
    (saturated) one dominates, so pixel counts give a reliable answer.
    """
    hsv   = cv2.cvtColor(arrow_bgr, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, _GREEN_LO, _GREEN_HI)
    red   = cv2.bitwise_or(
        cv2.inRange(hsv, _RED_LO1, _RED_HI1),
        cv2.inRange(hsv, _RED_LO2, _RED_HI2),
    )
    g_px = int(np.count_nonzero(green))
    r_px = int(np.count_nonzero(red))

    log.debug("arrow_detector: colour_sign — green=%d  red=%d", g_px, r_px)

    # Require a minimum count to avoid noise confusion
    if g_px < 20 and r_px < 20:
        return ""                   # neither colour dominant → use contour
    if g_px >= r_px:
        return "+"
    return "-"


# Optional character matcher (digit_reader.TemplateStore), set by the daemon.
DIGIT_READER = None


def _ticker_rank(bgr: "np.ndarray", hole_par: int | None = None) -> int | None:
    """
    The stroke number from the ticker, found by WHICH cell is highlighted.

    The highlighted cell is a filled white box; the others are thin white
    digits. Every white blob is found, the filled one is the blob with by far
    the largest area, and the stroke is its rank. This works wherever the cells
    sit inside the region — the earlier method assumed the cells spanned the
    whole region, and a region with empty space on the left read strokes 1 and
    2 one too high (verified on real GSPro frames), which made auto-capture
    see "stroke unchanged" and skip real shots.

    Sky behind the panel is excluded by colour (it is bluish, not white), and a
    blob touching the right edge is ignored (the "FOR PAR" banner beyond the
    ticker). If stray blobs appear on the LEFT (more blobs than par), the rank
    is counted from the right, where the ticker cells always are.
    """
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    # V >= 130: the thin, anti-aliased digits are greyer than the filled box;
    # a stricter threshold lost the "1" and shifted every rank after it.
    white = cv2.inRange(hsv, np.array([0, 0, 130]), np.array([180, 70, 255]))
    H, W = white.shape
    n, _, stats, _ = cv2.connectedComponentsWithStats(white, connectivity=8)
    blobs = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if h < 0.30 * H or area < 8:
            continue                                   # specks, underlines
        if x + w >= W - 1:
            continue                                   # runs off the right edge
        blobs.append((x, area))
    if len(blobs) < 2:
        return None
    hi = max(blobs, key=lambda b: b[1])
    if hi[1] < 2.5 * max(b[1] for b in blobs if b is not hi):
        return None                                    # no clearly filled cell
    blobs.sort()
    rank = blobs.index(hi) + 1
    if hole_par and len(blobs) > hole_par:
        rank = hole_par - (len(blobs) - rank)          # count from the right
    return rank if rank >= 1 else None


def detect_stroke_ticker(
    img: Image.Image,
    hole_par: int = 5,
    debug_path: Optional[Path] = None,
) -> int | None:
    """
    Detect the current stroke count from the GSPro hole score ticker (V1 region).

    The ticker displays equally-spaced numbered cells (1, 2, … par) with the
    current stroke highlighted.  When the player goes over par a red cell is
    appended at the right showing the actual stroke number (which may jump by
    2 or 3 if a penalty was taken).

    Two-path detection
    ------------------
    PATH A — Over-par (red cell present):
        Isolate the red region with HSV masking and OCR it.  The number
        inside the red cell is the exact stroke count regardless of how
        large the jump was.

    PATH B — At/under par (no red cell):
        The highlighted cell has a bright background; others are dark.
        Use the brightness centroid's x-position relative to image width
        to identify which of the (hole_par) cells is highlighted.
        stroke = floor(centroid_x / cell_width) + 1

    Parameters
    ----------
    img       : PIL Image of the V1 capture region
    hole_par  : Par for the current hole (number of at-par cells shown)
    debug_path: Optional path to save an annotated debug image

    Returns
    -------
    Stroke count as int, or None on detection failure.
    """
    if not _CV2:
        return None
    try:
        bgr = _pil_to_bgr(img)
        bgr = _upscale(bgr)
        h, w = bgr.shape[:2]

        # ── PATH A: Over-par — detect solid red column in rightmost zone ────────
        # The over-par red cell ALWAYS appears at the rightmost position.
        # We detect it by checking whether any column in the right 35% of the
        # image has > 50% of its rows coloured red.  This rejects scattered
        # border pixels that happen to have a red hue but are only 1-2 rows tall.
        right_start = int(w * 0.65)
        right_bgr   = bgr[:, right_start:]
        right_hsv   = cv2.cvtColor(right_bgr, cv2.COLOR_BGR2HSV)
        red_mask = cv2.bitwise_or(
            cv2.inRange(right_hsv, _RED_LO1, _RED_HI1),
            cv2.inRange(right_hsv, _RED_LO2, _RED_HI2),
        )
        # For each column in the right zone: what fraction of the height is red?
        col_red_frac = red_mask.mean(axis=0) / 255.0    # shape (right_w,)
        is_red_cell  = (col_red_frac > 0.20).any()      # ≥ 20% of height red

        if is_red_cell:
            x_cols = np.where(red_mask.any(axis=0))[0]
            x0 = max(0, int(x_cols[0]) - 2)
            x1 = min(right_bgr.shape[1], int(x_cols[-1]) + 2)
            red_crop = right_bgr[:, x0:x1]
            rh, rw = red_crop.shape[:2]
            red_pil = Image.fromarray(
                cv2.cvtColor(red_crop, cv2.COLOR_BGR2RGB)
            )
            red_pil = red_pil.resize((rw * 4, rh * 4), Image.LANCZOS)

            if debug_path is not None:
                debug_vis = bgr.copy()
                debug_vis[bgr.shape[0]//2:, right_start:][red_mask > 0] = (0, 80, 255)
                cv2.imwrite(str(debug_path), debug_vis)

            if _TSR:
                raw = _tsr.image_to_string(
                    red_pil,
                    config="--psm 10 -c tessedit_char_whitelist=0123456789",
                ).strip()
                m = re.search(r"\d+", raw)
                if m:
                    n = int(m.group())
                    # Sanity: must be over par and plausible (≤ par + 15)
                    if hole_par < n <= hole_par + 15:
                        log.info("arrow_detector: stroke ticker → %d (over-par red cell)", n)
                        return n
            log.warning("arrow_detector: red cell found but OCR failed")
            return None

        # ── PATH B: At/under par — rank of the highlighted cell ──────────────
        ranked = _ticker_rank(bgr, hole_par)
        if ranked is not None:
            log.info("arrow_detector: stroke ticker → %d (highlighted cell rank)", ranked)
            return ranked
        # Fallback when no cell is clearly highlighted: brightness centroid.
        # Exclude the rightmost 15% which can contain a bright UI border that
        # skews the centroid into the adjacent cell.
        inner_w    = int(w * 0.85)
        profile    = bgr[:, :inner_w].mean(axis=(0, 2)).astype(float)
        BRIGHT     = 110
        bright_cols = np.where(profile > BRIGHT)[0]
        if len(bright_cols) == 0:
            log.warning("arrow_detector: no bright cell found in ticker")
            return None

        centroid_x = float(bright_cols.mean())
        # Use full image width / par for cell width (the par cells fill the image)
        cell_w     = w / hole_par
        stroke     = int(centroid_x / cell_w) + 1
        stroke     = max(1, min(stroke, hole_par))

        if debug_path is not None:
            vis = bgr.copy()
            cv2.putText(vis, str(stroke), (4, h - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 1)
            cv2.imwrite(str(debug_path), vis)

        log.info(
            "arrow_detector: stroke ticker → %d (centroid_x=%.1f  cell_w=%.1f  par=%d)",
            stroke, centroid_x, cell_w, hole_par,
        )
        return stroke

    except Exception as exc:
        log.error("arrow_detector: stroke ticker detection failed — %s", exc)
        return None


def detect_height_delta(
    img: Image.Image,
    debug_path: Optional[Path] = None,
    split_ratio: float = 0.38,
) -> str:
    """
    Read the elevation indicator, e.g. "▲10ft 5" or "▼7ft 9". Returns signed
    FEET to one decimal ("+10.4", "-7.8", "0.0"), or "" if unreadable.

    GSPro draws BOTH triangles green; direction is shown by orientation, not
    colour. So the sign comes from the triangle's shape — ▲ is widest at the
    bottom, ▼ at the top — and the magnitude from reading the whole
    feet-and-inches text. (The earlier version decided the sign by colour and
    read only the right-hand digit — the inches — so "▼7ft 9" came out "+9".)
    split_ratio is accepted for compatibility and no longer used.
    """
    try:
        bgr = _upscale(_pil_to_bgr(img))
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        H, W = bgr.shape[:2]
        tri = cv2.inRange(hsv, np.array([0, 90, 60]), np.array([180, 255, 255]))
        ys, xs = np.nonzero(tri)
        sign, text_x0 = "", 0
        if len(xs) >= 6:
            left = xs <= xs.min() + max(4, W // 5)          # the leftmost coloured shape
            ty, tx = ys[left], xs[left]
            mid = (ty.min() + ty.max()) / 2.0
            upper = int(np.count_nonzero(ty < mid))
            lower = int(np.count_nonzero(ty > mid))
            sign = "+" if lower > upper else "-"
            text_x0 = int(tx.max()) + 2
        # Read only the WHITE text. The triangle is green even at its soft,
        # anti-aliased edges, so it drops out entirely — cropping at its edge
        # left a sliver that Tesseract read as a leading "4" ("4ft2" for 0ft 2).
        text_mask = cv2.inRange(hsv, np.array([0, 0, 130]), np.array([180, 70, 255]))
        text_mask[:, :max(0, text_x0 - 2)] = 0
        # Thicken the strokes slightly and pad: GSPro's thin "0" is otherwise
        # often dropped by Tesseract ("ft9" for 0ft 9).
        text_mask = cv2.dilate(text_mask, np.ones((2, 2), np.uint8))
        bw = cv2.copyMakeBorder(255 - text_mask, 10, 10, 10, 10,
                                cv2.BORDER_CONSTANT, value=255)
        if debug_path is not None:
            cv2.imwrite(str(debug_path), bw)
        raw = DIGIT_READER.read("F1", img) if DIGIT_READER is not None else None
        if raw is not None and DIGIT_READER.needs_audit("F1") and _TSR:
            tess = _tsr.image_to_string(Image.fromarray(bw), config="--psm 7 -c "
                                        "tessedit_char_whitelist=0123456789ft").strip()
            if not DIGIT_READER.record_audit("F1", raw, tess):
                raw = tess
        if raw is None:
            if not _TSR:
                return ""
            raw = _tsr.image_to_string(Image.fromarray(bw),
                                       config="--psm 7 -c tessedit_char_whitelist=0123456789ft").strip()
            if DIGIT_READER is not None and raw:
                DIGIT_READER.learn("F1", img, raw)
        # A reading that starts at "ft" lost its feet digit. In testing on real
        # frames that only ever happened to a 0 (non-zero feet read reliably),
        # so a missing feet number is taken as 0.
        m = re.search(r"(\d*)\s*ft\s*(\d{1,2})?", raw)
        if not m:
            return ""
        feet = int(m.group(1) or 0) + (int(m.group(2)) if m.group(2) else 0) / 12.0
        if feet == 0:
            return "0.0"
        return f"{sign or '+'}{feet:.1f}"
    except Exception as exc:
        log.error("arrow_detector: height delta failed — %s", exc)
        return ""
