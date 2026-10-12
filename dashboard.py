#!/usr/bin/env python3
"""
dashboard.py — Your strokes gained as one self-contained HTML page (T-16).

Four views the spreadsheet cannot show well:
  * week by week   — strokes gained per hole for each category, as trend lines
  * by category    — this session against the last 30 days of play
  * club gapping   — carry median, middle half and extremes for every club,
                     with the gap to the next club and overlap/large-gap flags
  * dispersion     — every shot's distance offline against short/long, per club

It is built from the same three inputs as the spreadsheet's summary sheets (the
live round, the archived sessions and the SG_Shots rows) and the same
calculations (reports._aggregate_weeks, reports.gapping_stats,
reports._shots_from_rows), so its numbers agree with the report.

The page needs nothing else: no internet, no scripts, no image files. Charts
are inline SVG; hovering a point shows its value (SVG <title>); light and dark
follow the system setting. Open it in any browser.
"""

from __future__ import annotations

import html
import math
from datetime import datetime, timedelta

import reports
import scoring
import strokes_gained as sg_mod

DATE_FMT = "%Y-%m-%d"
CATS = list(sg_mod.ALL_CATS)                     # Off Tee, Approach, Around Green, Putting
CLASS = {sg_mod.CAT_OTT: "ott", sg_mod.CAT_APP: "app", sg_mod.CAT_ATG: "atg",
         sg_mod.CAT_PUTT: "putt", sg_mod.CAT_UNATTRIBUTED: "una", "Total": "tot"}
MIN_DISPERSION_SHOTS = 3


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def _signed(v: float | None, d: int = 2) -> str:
    if v is None:
        return "—"
    v = round(v, d)
    return f"{0:.{d}f}" if v == 0 else f"{v:+.{d}f}".replace("-", "−")


def _n(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    """Round-numbered axis ticks spanning lo..hi."""
    if hi - lo < 1e-9:
        lo, hi = lo - 0.5, hi + 0.5
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), 10 * mag)
    t, ticks = math.floor(lo / step) * step, []
    while t <= hi + step * 0.999:
        ticks.append(round(t, 10))
        t += step
    if ticks[0] > lo:
        ticks.insert(0, round(ticks[0] - step, 10))
    return ticks


def _fmt_tick(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return (s if s != "-0" else "0").replace("-", "−")


def _svg(w: int, h: int, body: str, label: str) -> str:
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
            f'aria-label="{esc(label)}" xmlns="http://www.w3.org/2000/svg">{body}</svg>')


# ── data ──────────────────────────────────────────────────────────────────────

def _rounds(round_state: dict | None, sessions: list) -> list[dict]:
    """Archived sessions plus the session in progress, if it has holes."""
    rounds = [dict(s) for s in (sessions or [])]
    try:
        live = scoring.build_session_entry(round_state) if round_state else None
    except Exception:
        live = None
    archived = {s.get("round_id") for s in rounds if s.get("round_id")}
    if live and live.get("holes") and live.get("round_id") not in archived:
        live["live"] = True               # not yet archived, so not counted twice
        rounds.append(live)
    return rounds


def _per_hole(rounds: list[dict]) -> dict:
    holes = sum(r.get("holes", 0) for r in rounds)
    sg = {c: sum((r.get("sg") or {}).get(c, 0.0) for r in rounds) for c in sg_mod.SG_CATS}
    tot = sum(r.get("sg_total", 0.0) for r in rounds)
    return {"holes": holes, "rounds": len(rounds),
            "cat": {c: (sg[c] / holes if holes else None) for c in sg_mod.SG_CATS},
            "total": tot / holes if holes else None,
            "coverage": _cov_text(rounds)}


def _coverage(rounds: list[dict]) -> tuple[int, int]:
    """Strokes known and in all — the same calculation as the report (T-18)."""
    known, total, _ = reports.coverage_counts(rounds)
    return known, total


def _cov_text(rounds: list[dict]) -> str:
    return reports.coverage_label(*_coverage(rounds))


def _last_days(rounds: list[dict], days: int = 30) -> list[dict]:
    dated = [r for r in rounds if r.get("date")]
    if not dated:
        return []
    latest = max(datetime.strptime(r["date"], DATE_FMT) for r in dated)
    since = latest - timedelta(days=days)
    return [r for r in dated if datetime.strptime(r["date"], DATE_FMT) > since]


# ── charts ────────────────────────────────────────────────────────────────────

def _legend(items: list[tuple[str, str]]) -> str:
    return '<p class="legend">' + "".join(
        f'<span class="key"><span class="chip c-{cls}"></span>{esc(name)}</span>'
        for name, cls in items) + "</p>"


def week_chart(rounds: list[dict]) -> str:
    weeks = reports._aggregate_weeks(rounds)
    if not weeks:
        return '<p class="empty">No completed holes yet.</p>'
    labels = list(weeks)
    series = {c: [(w["sg"][c] / w["holes"] if w["holes"] else None) for w in weeks.values()]
              for c in CATS}
    series["Total"] = [(w["sg_total"] / w["holes"] if w["holes"] else None) for w in weeks.values()]
    vals = [v for s in series.values() for v in s if v is not None] + [0.0]
    ticks = _nice_ticks(min(vals), max(vals))
    W, H, L, R, T, B = 760, 312, 52, 36, 14, 66
    x0, x1, y0, y1 = L, W - R, T, H - B
    lo, hi = ticks[0], ticks[-1]
    ys = lambda v: y1 - (v - lo) / (hi - lo) * (y1 - y0)
    n = len(labels)
    xs = (lambda i: (x0 + x1) / 2) if n == 1 else (lambda i: x0 + i * (x1 - x0) / (n - 1))
    out = []
    for t in ticks:
        cls = "zero" if abs(t) < 1e-9 else "grid"
        out.append(f'<line class="{cls}" x1="{x0}" x2="{x1}" y1="{ys(t):.1f}" y2="{ys(t):.1f}"/>'
                   f'<text class="tick" x="{x0 - 6}" y="{ys(t) + 4:.1f}" text-anchor="end">{_fmt_tick(t)}</text>')
    every = max(1, math.ceil(n / 10))
    for i, (lab, w) in enumerate(weeks.items()):
        if i % every == 0 or i == n - 1:
            cov = _cov_text(w["sessions"])
            out.append(f'<text class="tick" x="{xs(i):.1f}" y="{y1 + 18}" text-anchor="middle">{esc(lab[5:] or lab)}</text>'
                       f'<text class="sub" x="{xs(i):.1f}" y="{y1 + 33}" text-anchor="middle">{w["holes"]} holes</text>'
                       f'<text class="sub{" low" if "low" in cov else ""}" x="{xs(i):.1f}" y="{y1 + 46}" '
                       f'text-anchor="middle">{esc(cov.replace(" (low)", ""))} captured</text>')
    for name, vs in series.items():
        cls = CLASS[name]
        pts = [(xs(i), ys(v)) for i, v in enumerate(vs) if v is not None]
        if len(pts) >= 2:
            out.append(f'<polyline class="line c-{cls}" points="' +
                       " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '"/>')
        for i, v in enumerate(vs):
            if v is None:
                continue
            out.append(f'<circle class="dot c-{cls}" cx="{xs(i):.1f}" cy="{ys(v):.1f}" r="{4 if name == "Total" else 3.2}">'
                       f'<title>{esc(labels[i])} · {esc(name)}: {_signed(v)} per hole '
                       f'({list(weeks.values())[i]["holes"]} holes, coverage '
                       f'{esc(_cov_text(list(weeks.values())[i]["sessions"]))})</title></circle>')
    note = ('<p class="note">Trend lines appear from the second week of play.</p>' if n == 1 else "")
    return (_legend([(c, CLASS[c]) for c in CATS] + [("Total", "tot")]) +
            _svg(W, H, "".join(out), "Strokes gained per hole by week") + note)


def category_chart(session: dict, recent: dict, session_label: str) -> str:
    rows = [c for c in CATS] + ([sg_mod.CAT_UNATTRIBUTED]
                                if any((d["cat"].get(sg_mod.CAT_UNATTRIBUTED) or 0) != 0
                                       for d in (session, recent) if d["holes"]) else [])
    vals = [d["cat"][c] for d in (session, recent) if d["holes"] for c in rows if d["cat"][c] is not None]
    if not vals:
        return '<p class="empty">No completed holes yet.</p>'
    m = max(abs(v) for v in vals) or 0.1
    ticks = _nice_ticks(-m, m, 4)
    W, LBL, R, T, ROW = 760, 140, 70, 10, 44
    H = T + ROW * len(rows) + 30
    x0, x1 = LBL, W - R
    lo, hi = ticks[0], ticks[-1]
    xs = lambda v: x0 + (v - lo) / (hi - lo) * (x1 - x0)
    out = []
    for t in ticks:
        cls = "zero" if abs(t) < 1e-9 else "grid"
        out.append(f'<line class="{cls}" x1="{xs(t):.1f}" x2="{xs(t):.1f}" y1="{T}" y2="{H - 26}"/>'
                   f'<text class="tick" x="{xs(t):.1f}" y="{H - 10}" text-anchor="middle">{_fmt_tick(t)}</text>')
    for k, c in enumerate(rows):
        y = T + k * ROW
        out.append(f'<text class="lab" x="{LBL - 10}" y="{y + ROW / 2 + 4:.1f}" text-anchor="end">{esc(c)}</text>')
        for j, (d, kind, name) in enumerate(((session, "solid", session_label), (recent, "soft", "Last 30 days"))):
            v = d["cat"][c] if d["holes"] else None
            if v is None:
                continue
            by, bh = y + 6 + j * 17, 14
            xa, xb = sorted((xs(0), xs(v)))
            out.append(f'<rect class="bar c-{CLASS[c]} {kind}" x="{xa:.1f}" y="{by}" width="{max(xb - xa, 1):.1f}" height="{bh}">'
                       f'<title>{esc(name)} · {esc(c)}: {_signed(v)} per hole</title></rect>'
                       f'<text class="val" x="{(xb + 5) if v >= 0 else (xa - 5):.1f}" y="{by + 11}" '
                       f'text-anchor="{"start" if v >= 0 else "end"}">{_signed(v)}</text>')
    legend = ('<p class="legend"><span class="key"><span class="chip c-tot"></span>'
              f'{esc(session_label)} ({session["holes"]} holes, coverage {esc(session["coverage"])})</span>'
              '<span class="key"><span class="chip c-tot soft"></span>'
              f'Last 30 days ({_n(recent["holes"], "hole")}, {_n(recent["rounds"], "session")}, '
              f'coverage {esc(recent["coverage"])})</span></p>')
    note = ('<p class="note">Unattributed: strokes inferred from the round score whose '
            'start position is unknown.</p>' if sg_mod.CAT_UNATTRIBUTED in rows else "")
    note += ('<p class="note">Coverage: share of strokes captured (or conceded); the four '
             'categories are built from those strokes only. Below '
             f'{reports.COVERAGE_LOW:.0%} they leave out a fifth or more of the play.</p>')
    return legend + _svg(W, H, "".join(out), "Strokes gained per hole by category") + note


def gapping_chart(stats: list[dict]) -> str:
    rows = [s for s in stats]
    measured = [s for s in rows if s["median"] is not None]
    if not measured:
        return '<p class="empty">No full-swing shots with a club yet.</p>'
    lo = min(s["min"] for s in measured)
    hi = max(s["max"] for s in measured)
    ticks = _nice_ticks(max(0.0, lo - 5), hi + 5, 6)
    W, LBL, R, T, ROW = 760, 120, 215, 8, 30
    H = T + ROW * len(rows) + 32
    x0, x1 = LBL, W - R
    a, b = ticks[0], ticks[-1]
    xs = lambda v: x0 + (v - a) / (b - a) * (x1 - x0)
    out = []
    for t in ticks:
        out.append(f'<line class="grid" x1="{xs(t):.1f}" x2="{xs(t):.1f}" y1="{T}" y2="{H - 26}"/>'
                   f'<text class="tick" x="{xs(t):.1f}" y="{H - 10}" text-anchor="middle">{_fmt_tick(t)}</text>')
    for k, s in enumerate(rows):
        y = T + k * ROW + ROW / 2
        out.append(f'<text class="lab" x="{LBL - 10}" y="{y + 4:.1f}" text-anchor="end">{esc(s["club"])}'
                   f'<tspan class="sub"> ({s["n"]})</tspan></text>')
        if s["median"] is None:
            out.append(f'<text class="sub" x="{x0 + 4}" y="{y + 4:.1f}">{esc(s["note"])}</text>')
            continue
        few = s["note"].startswith("few shots")
        tip = (f'{s["club"]}: median carry {s["median"]:.1f} yd · middle half {s["p25"]:.1f}–{s["p75"]:.1f}'
               f' · range {s["min"]:.1f}–{s["max"]:.1f} · {s["n"]} full shots')
        out.append(f'<g class="{"few" if few else "box"}"><title>{esc(tip)}</title>'
                   f'<line class="whisk" x1="{xs(s["min"]):.1f}" x2="{xs(s["max"]):.1f}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<rect class="iqr" x="{xs(s["p25"]):.1f}" y="{y - 8:.1f}" width="{max(xs(s["p75"]) - xs(s["p25"]), 2):.1f}" height="16"/>'
                   f'<line class="med" x1="{xs(s["median"]):.1f}" x2="{xs(s["median"]):.1f}" y1="{y - 10:.1f}" y2="{y + 10:.1f}"/></g>')
        gap = s.get("gap")
        flag = ("bad" if s["note"].startswith("large gap") else
                "warn" if s["note"].startswith("overlaps") else "muted" if few else "")
        text = (f'{s["median"]:.0f} yd' + (f' · gap {gap:.0f}' if gap is not None else "")
                + (" · overlap" if flag == "warn" else " · large gap" if flag == "bad" else
                   " · few shots" if flag == "muted" else ""))
        out.append(f'<text class="gap {flag}" x="{x1 + 10}" y="{y + 4:.1f}">{esc(text)}</text>')
    return _svg(W, H, "".join(out), "Club gapping: carry distance by club")


def _side_word(p: dict) -> str:
    if p.get("side", 0) == 0:
        return "side unknown"
    word = "right" if p["side"] > 0 else "left"
    if p.get("bias_unit") == "ft":
        return f"{word} (start line only — the green's slope is not known)"
    how = (f'estimated from HLA {p["hla"]:+.1f}°, spin axis {p.get("axis") or 0:+.1f}°'
           if p.get("hla") is not None else "estimated")
    return f"{word} ({how})" if p.get("side_firm") else f"too close to the line to call ({word}?)"


def _panel(club: str, pts: list[dict], unit: str) -> str:
    """Short/long against offline. The side (left/right) is estimated from the
    launch data (T-31): filled = firm estimate, hollow = too close to call or a putt."""
    W, H, L, R, T, B = 236, 222, 40, 10, 10, 46
    x0, x1, y0, y1 = L, W - R, T, H - B
    xmax = max(max(p["offline"] for p in pts), 1.0)
    xt = _nice_ticks(-xmax, xmax, 4)
    ym = max(max(abs(p["bias"]) for p in pts), 1.0)
    yt = _nice_ticks(-ym, ym, 4)
    ylo, yhi, xlo, xhi = yt[0], yt[-1], xt[0], xt[-1]
    xs = lambda v: x0 + (v - xlo) / (xhi - xlo) * (x1 - x0)
    ys = lambda v: y1 - (v - ylo) / (yhi - ylo) * (y1 - y0)
    out = [f'<rect class="frame" x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}"/>']
    for t in yt:
        cls = "zero" if abs(t) < 1e-9 else "grid"
        out.append(f'<line class="{cls}" x1="{x0}" x2="{x1}" y1="{ys(t):.1f}" y2="{ys(t):.1f}"/>'
                   f'<text class="tick" x="{x0 - 5}" y="{ys(t) + 4:.1f}" text-anchor="end">{_fmt_tick(t)}</text>')
    out.append(f'<line class="zero" x1="{xs(0):.1f}" x2="{xs(0):.1f}" y1="{y0}" y2="{y1}"/>')
    for t in xt:
        out.append(f'<text class="tick" x="{xs(t):.1f}" y="{y1 + 14}" text-anchor="middle">{_fmt_tick(abs(t))}</text>')
    out.append(f'<text class="sub" x="{x1 - 4}" y="{ys(0) - 5:.1f}" text-anchor="end">pin high</text>'
               f'<text class="sub" x="{x0 + 2}" y="{H - 18}">← left</text>'
               f'<text class="sub" x="{x1 - 2}" y="{H - 18}" text-anchor="end">right →</text>'
               f'<text class="sub" x="{(x0 + x1) / 2:.1f}" y="{H - 4}" text-anchor="middle">offline ({unit})</text>')
    for p in pts:
        where = "long" if p["bias"] > 0 else "short" if p["bias"] < 0 else "pin high"
        x = p["offline"] * (p.get("side") or 1)
        firm = bool(p.get("side_firm"))
        tip = (f'{p.get("date", "")} {p.get("time", "")} · hole {p.get("hole", "")}: '
               f'{abs(p["bias"]):.1f} {unit} {where}, {p["offline"]:.1f} {unit} offline, {_side_word(p)}')
        out.append(f'<circle class="pt{"" if firm else " open"}" cx="{xs(x):.1f}" cy="{ys(p["bias"]):.1f}" r="3.4">'
                   f'<title>{esc(tip)}</title></circle>')
    mb = sum(p["bias"] for p in pts) / len(pts)
    mo = sum(p["offline"] for p in pts) / len(pts)
    left = sum(1 for p in pts if p.get("side_firm") and p["side"] < 0)
    right = sum(1 for p in pts if p.get("side_firm") and p["side"] > 0)
    unsure = len(pts) - left - right
    sides = (f" · {left} left, {right} right" + (f", {unsure} too close to call" if unsure else "")
             if unit != "ft" else " · side = start line")
    cap = (f'{len(pts)} shots · average {abs(mb):.1f} {unit} '
           f'{"long" if mb > 0 else "short" if mb < 0 else "pin high"} · {mo:.1f} {unit} offline{sides}')
    return (f'<figure class="panel"><figcaption><b>{esc(club)}</b><br><span>{esc(cap)}</span></figcaption>'
            + _svg(W, H, "".join(out), f"Dispersion for {club}") + "</figure>")


def dispersion_panels(shots: list[dict]) -> str:
    by: dict[str, list[dict]] = {}
    for s in shots:
        if s.get("bias") is not None and s.get("offline") is not None:
            by.setdefault(s["club"], []).append(s)
    order = [c for c in reports._bag_order() if c in by] + sorted(c for c in by if c not in reports._bag_order())
    shown = [c for c in order if len(by[c]) >= MIN_DISPERSION_SHOTS]
    if not shown:
        return ('<p class="empty">Not enough shots with distance readings yet '
                f'(each club needs {MIN_DISPERSION_SHOTS}).</p>')
    few = [c for c in order if c not in shown]
    return ('<div class="panels">' + "".join(_panel(c, by[c], by[c][0].get("bias_unit") or "yd")
                                             for c in shown) + "</div>"
            + (f'<p class="note">Not shown yet (under {MIN_DISPERSION_SHOTS} shots): '
               f'{esc(", ".join(few))}.</p>' if few else ""))


# ── page ──────────────────────────────────────────────────────────────────────

CSS = """
body{margin:0;background:#f7f7f5;color:#1f2328;font:15px/1.5 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:820px;margin:0 auto;padding:28px 20px 40px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:0 0 4px}
.meta,.lede,.note,.empty,figcaption span,footer{color:#5b6470}
.meta{margin:0 0 20px;font-size:13px}.lede{margin:0 0 10px;font-size:14px}
.note{font-size:13px;margin:6px 0 0}.empty{font-style:italic}
section{background:#fff;border:1px solid #e3e3df;border-radius:10px;padding:18px 18px 14px;margin:0 0 18px;overflow-x:auto}
.tiles{background:none;border:0;padding:0}
.tile{display:inline-block;vertical-align:top;width:152px;margin:0 10px 10px 0;background:#fff;border:1px solid #e3e3df;border-radius:10px;padding:12px 14px}
.tile .k{font-size:12px;color:#5b6470;text-transform:uppercase;letter-spacing:.04em}
.tile .v{font-size:24px;font-weight:600;margin:2px 0}.tile .s{font-size:12px;color:#5b6470}
.pos{color:#15803d}.neg{color:#b91c1c}
.legend{margin:0 0 6px;font-size:13px}.key{margin-right:16px;white-space:nowrap}
.chip{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}
svg{max-width:100%;height:auto;display:block}
svg text{font-family:inherit}
.tick,.sub{fill:#6b7280;font-size:11px}.lab{fill:#1f2328;font-size:13px}.val{fill:#1f2328;font-size:11px}
.grid{stroke:#ececea;stroke-width:1}.zero{stroke:#9ca3af;stroke-width:1.2}
.line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.line.c-tot{stroke-width:3}.dot{stroke:#fff;stroke-width:1.2}
.c-ott{stroke:#2563eb;fill:#2563eb;background:#2563eb}.c-app{stroke:#16a34a;fill:#16a34a;background:#16a34a}
.c-atg{stroke:#d97706;fill:#d97706;background:#d97706}.c-putt{stroke:#9333ea;fill:#9333ea;background:#9333ea}
.c-tot{stroke:#1f2328;fill:#1f2328;background:#1f2328}.c-una{stroke:#9ca3af;fill:#9ca3af;background:#9ca3af}
.line.c-ott,.line.c-app,.line.c-atg,.line.c-putt,.line.c-tot{fill:none}
.bar{stroke:none}.bar.soft,.chip.soft{opacity:.38}
.whisk{stroke:#6b7280;stroke-width:1.5}.iqr{fill:#99d5cf;stroke:#0f766e;stroke-width:1.2}
.med{stroke:#0f3d3a;stroke-width:2.5}.few .iqr{fill:#e5e7eb;stroke:#9ca3af}.few .med{stroke:#6b7280}
.gap{fill:#1f2328;font-size:12px}.sub.low{fill:#b45309;font-weight:600}.gap.warn{fill:#b45309;font-weight:600}.gap.bad{fill:#b91c1c;font-weight:600}.gap.muted{fill:#6b7280}
.panels{font-size:0}.panel{display:inline-block;vertical-align:top;width:236px;margin:0 14px 14px 0;font-size:13px}
.panel figcaption{margin-bottom:4px}.frame{fill:#fbfbfa;stroke:#e3e3df}
.pt{fill:#0f766e;fill-opacity:.55;stroke:#0f766e;stroke-width:.8}.pt.open{fill:none;stroke-width:1.2}
footer{font-size:12.5px;margin-top:8px}footer li{margin-bottom:4px}
@media (prefers-color-scheme: dark){
body{background:#15171a;color:#e6e6e3}section,.tile{background:#1d2024;border-color:#2c3036}
.meta,.lede,.note,.empty,figcaption span,footer,.tile .k,.tile .s{color:#9aa1aa}
.tick,.sub{fill:#9aa1aa}.lab,.val,.gap{fill:#e6e6e3}.grid{stroke:#2a2e33}.zero{stroke:#6b7280}.dot{stroke:#1d2024}
.c-ott{stroke:#60a5fa;fill:#60a5fa;background:#60a5fa}.c-app{stroke:#4ade80;fill:#4ade80;background:#4ade80}
.c-atg{stroke:#fbbf24;fill:#fbbf24;background:#fbbf24}.c-putt{stroke:#c084fc;fill:#c084fc;background:#c084fc}
.c-tot{stroke:#f3f4f6;fill:#f3f4f6;background:#f3f4f6}
.line.c-ott,.line.c-app,.line.c-atg,.line.c-putt,.line.c-tot{fill:none}
.pos{color:#4ade80}.neg{color:#f87171}.frame{fill:#191c1f;stroke:#2c3036}
.iqr{fill:#134e4a;stroke:#5eead4}.med{stroke:#ccfbf1}.whisk{stroke:#9aa1aa}.pt{fill:#5eead4;stroke:#5eead4}
.gap.warn{fill:#fbbf24}.gap.bad{fill:#f87171}.sub.low{fill:#fbbf24}}
"""


def _tile(k: str, v: str, s: str, tone: str = "") -> str:
    return (f'<div class="tile"><div class="k">{esc(k)}</div>'
            f'<div class="v {tone}">{esc(v)}</div><div class="s">{esc(s)}</div></div>')


def _tone(v: float | None) -> str:
    return "" if v is None else "pos" if v > 0 else "neg" if v < 0 else ""


def build_dashboard(round_state: dict | None, sessions: list, shot_rows: list,
                    version: str = "", generated_at: datetime | None = None) -> str:
    """The whole page as an HTML string. Pure: same inputs, same output."""
    generated_at = generated_at or datetime.now()
    rounds = _rounds(round_state, sessions)
    shots, excluded = reports._shots_from_rows(shot_rows or [])

    live = next((r for r in rounds if r.get("live")), None)
    current = live or (rounds[-1] if rounds else None)
    session_label = "This session" if live else "Last session"
    s_stats = _per_hole([current] if current else [])
    recent_rounds = _last_days(rounds)
    r_stats = _per_hole(recent_rounds)
    known, total = _coverage(recent_rounds)

    dates = sorted(r["date"] for r in rounds if r.get("date"))
    span = f"{dates[0]} to {dates[-1]}" if dates else "no rounds yet"
    holes = sum(r.get("holes", 0) for r in rounds)
    nshots = sum(r.get("shots", 0) for r in rounds)

    tiles = []
    if current:
        vp = current.get("vs_par", 0)
        tiles.append(_tile(session_label, "E" if vp == 0 else f"{vp:+d}".replace("-", "−"),
                           f'{current.get("course", "") or "—"} · {_n(current.get("holes", 0), "hole")}'))
        tiles.append(_tile("SG per hole", _signed(s_stats["total"]), session_label.lower(),
                           _tone(s_stats["total"])))
    tiles.append(_tile("SG per hole, 30 days", _signed(r_stats["total"]),
                       f'{_n(r_stats["rounds"], "session")} · {_n(r_stats["holes"], "hole")}',
                       _tone(r_stats["total"])))
    tiles.append(_tile("Coverage, 30 days", f"{known / total * 100:.0f}%" if total else "—",
                       f"{known} of {total} strokes" if total else "no strokes yet"))

    gap_shots, gap_title = shots, "All sessions"
    sdates = sorted(s["date"] for s in shots if s.get("date"))
    if sdates:
        since = (datetime.strptime(sdates[-1], DATE_FMT) - timedelta(days=30)).strftime(DATE_FMT)
        recent_shots = [s for s in shots if s.get("date") and s["date"] > since]
        if 0 < len(recent_shots) < len(shots):
            gap_shots, gap_title = recent_shots, f"Last 30 days of play (to {sdates[-1]})"
    stats, blank = reports.gapping_stats(gap_shots)

    left = []
    if excluded["estimated"]:
        left.append(f'{excluded["estimated"]} estimated stroke(s)')
    if excluded["conceded"]:
        left.append(f'{excluded["conceded"]} conceded putt(s)')
    if excluded["club_unknown"]:
        left.append(f'{excluded["club_unknown"]} shot(s) with no club recorded')

    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>Strokes gained dashboard — {esc(span)}</title><style>{CSS}</style></head><body><main>',
        '<header><h1>Strokes gained dashboard</h1>',
        f'<p class="meta">Data from {esc(span)} · {_n(len(rounds), "session")}, '
        f'{_n(holes, "hole")}, {_n(nshots, "shot")} · generated {generated_at:%Y-%m-%d %H:%M}'
        + (f' by daemon {esc(version)}' if version else "") + '</p></header>',
        '<section class="tiles">' + "".join(tiles) + '</section>',
        '<section><h2>Week by week</h2><p class="lede">Strokes gained per hole against the PGA '
        'Tour baseline, for each category. Above the zero line is better than tour average.</p>'
        + week_chart(rounds) + '</section>',
        '<section><h2>By category</h2><p class="lede">Where strokes are won and lost, per hole: '
        f'{esc(session_label.lower())} against the last 30 days of play.</p>'
        + category_chart(s_stats, r_stats, session_label) + '</section>',
        f'<section><h2>Club gapping</h2><p class="lede">{esc(gap_title)}. Carry per club: the box '
        'is the middle half of shots, the bar the median, the whiskers the shortest and longest. '
        'Gap = median carry to the next club.</p>' + gapping_chart(stats)
        + (f'<p class="note">{blank} full shot(s) had no carry reading and are not shown.</p>'
           if blank else "")
        + f'<p class="note">Gaps are judged for clubs with {reports.MIN_GAP_SHOTS}+ full shots: '
        f'under {reports.OVERLAP_YD:.0f} yd to the next club = overlap, over '
        f'{reports.LARGE_GAP_YD:.0f} yd = large gap.</p></section>',
        '<section><h2>Dispersion by club</h2><p class="lede">Every shot with distance readings: '
        'how far left or right of the line it finished against how far short (below) or long '
        '(above) of pin high, as you face the target. Putts are in feet.</p>' + dispersion_panels(shots)
        + '<p class="note">How far offline is measured from the screen; which side is <b>estimated</b> '
        'from the launch direction (HLA) and spin axis. The estimate matched the screen on every shot '
        f'checked where it predicted a miss of {reports.SIDE_FIRM_YD:.1f} yd or more. Hollow points '
        'are too close to the line to call, or putts, whose side is only their start line.</p>'
        '</section>',
        '<footer><ul>',
        '<li>Figures come from the same calculations as the spreadsheet report.</li>',
        ('<li>Not in the club charts: ' + esc("; ".join(left)) + '.</li>') if left else "",
        '<li>Practice sessions (such as Operation 36) are not yet separated from rounds.</li>',
        '</ul></footer></main></body></html>',
    ]
    return "".join(parts)
