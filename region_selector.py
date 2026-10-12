#!/usr/bin/env python3
"""
region_selector.py — Interactive GUI for defining OCR screen regions.

Draw rectangles on a fullscreen semi-transparent overlay.
Each selection prompts for:  name · target cell · sheet · Tesseract PSM · preprocessing.
Press ESC (or close) when done — the result is saved to config.json.

Usage:
    python region_selector.py
    python region_selector.py --config my_config.json   # use a custom path
"""

from __future__ import annotations

import argparse
import json
import tkinter as tk
from tkinter import simpledialog
from pathlib import Path


# ── config skeleton (written if config.json does not exist yet) ───────────────

SKELETON: dict = {
    "hotkey":       "<ctrl>+<shift>+F9",
    "exit_hotkey":  "<ctrl>+<shift>+F10",
    "ods_file":     "ocr_output.ods",
    "tesseract_cmd":  None,
    "tesseract_lang": "eng",
    "regions": [],
}


# ── PSM reference shown in the dialog ────────────────────────────────────────

PSM_HELP = (
    "Tesseract Page Segmentation Mode (PSM):\n"
    "   3  — automatic (default engine)\n"
    "   6  — uniform block of text  ← good default\n"
    "   7  — single text line       ← labels / IDs\n"
    "   8  — single word\n"
    "  11  — sparse text\n"
    "  13  — raw line (no layout)\n"
    "\nEnter the number:"
)

PRE_HELP = (
    "Image preprocessing before OCR:\n"
    "  (leave blank) — none\n"
    "  scale         — 2× upscale (small text)\n"
    "  threshold     — binarise  (high-contrast docs)\n"
    "  auto          — scale + threshold  ← recommended\n"
    "\nEnter mode:"
)


# ── main class ────────────────────────────────────────────────────────────────

class RegionSelector:
    """Fullscreen transparent overlay with click-drag region selection."""

    # visual constants
    ALPHA      = 0.28
    BG         = "#0a0a0a"
    BOX_CLR    = "#00e676"   # green
    DONE_CLR   = "#2979ff"   # blue — confirmed boxes
    TXT_CLR    = "white"
    FONT_MAIN  = ("Consolas", 12, "bold")
    FONT_SMALL = ("Consolas", 10)

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.regions: list[dict] = []
        self._box_drawing: int | None = None   # canvas item id of current drag rect
        self._sx = self._sy = 0

        self._setup_window()
        self._draw_instructions()
        self._bind_events()
        self.root.mainloop()

    # ── window setup ─────────────────────────────────────────────────────────

    def _setup_window(self) -> None:
        root = tk.Tk()
        root.title("OCR Region Selector")
        root.attributes("-fullscreen", True)
        root.attributes("-alpha", self.ALPHA)
        root.attributes("-topmost", True)
        root.configure(bg=self.BG)
        self.root = root

        self.sw = root.winfo_screenwidth()
        self.sh = root.winfo_screenheight()

        canvas = tk.Canvas(root, bg=self.BG, cursor="crosshair",
                           highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas = canvas

    def _draw_instructions(self) -> None:
        c = self.canvas
        cx = self.sw // 2

        c.create_text(cx, 22, fill=self.TXT_CLR, font=self.FONT_MAIN,
                      text="═══  OCR Region Selector  ═══")
        c.create_text(cx, 46, fill="#aaaaaa", font=self.FONT_SMALL,
                      text="Left-drag = new region  │  Right-click = undo last  │  ESC = save & quit")

    # ── event binding ─────────────────────────────────────────────────────────

    def _bind_events(self) -> None:
        c = self.canvas
        c.bind("<ButtonPress-1>",   self._on_press)
        c.bind("<B1-Motion>",       self._on_drag)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<ButtonPress-3>",   self._undo)
        self.root.bind("<Escape>",  self._on_done)
        self.root.protocol("WM_DELETE_WINDOW", lambda: self._on_done(None))

    # ── drawing interactions ──────────────────────────────────────────────────

    def _on_press(self, ev: tk.Event) -> None:
        self._sx, self._sy = ev.x, ev.y
        if self._box_drawing is not None:
            self.canvas.delete(self._box_drawing)
            self._box_drawing = None

    def _on_drag(self, ev: tk.Event) -> None:
        if self._box_drawing is not None:
            self.canvas.delete(self._box_drawing)
        self._box_drawing = self.canvas.create_rectangle(
            self._sx, self._sy, ev.x, ev.y,
            outline=self.BOX_CLR, width=2,
            fill=self.BOX_CLR, stipple="gray25",
        )

    def _on_release(self, ev: tk.Event) -> None:
        x1, y1 = min(self._sx, ev.x), min(self._sy, ev.y)
        x2, y2 = max(self._sx, ev.x), max(self._sy, ev.y)
        w, h = x2 - x1, y2 - y1

        # Ignore tiny accidental clicks
        if w < 12 or h < 8:
            if self._box_drawing is not None:
                self.canvas.delete(self._box_drawing)
                self._box_drawing = None
            return

        self._ask_region_details(x1, y1, x2, y2, w, h)

    def _ask_region_details(self, x1: int, y1: int, x2: int, y2: int,
                             w: int, h: int) -> None:
        """Show dialogs to fill in region metadata, then commit."""
        idx = len(self.regions) + 1

        # Temporarily make overlay invisible so dialogs are readable
        self.root.attributes("-alpha", 0.0)
        self.root.update()

        try:
            name = simpledialog.askstring(
                "Region name",
                f"Name for this region?\n\n  Position : ({x1}, {y1})\n  Size     : {w} × {h}",
                initialvalue=f"Region{idx}",
                parent=self.root,
            )
            if not name:
                self._cancel_box()
                return

            cell = simpledialog.askstring(
                "Target cell",
                "Cell address in the ODS file\n(e.g.  A1  C4  AB12):",
                initialvalue=f"A{idx}",
                parent=self.root,
            )
            if not cell:
                self._cancel_box()
                return
            cell = cell.upper().strip()

            sheet = simpledialog.askstring(
                "Sheet name",
                "Sheet / tab name in the ODS file:",
                initialvalue="Sheet1",
                parent=self.root,
            ) or "Sheet1"

            psm = simpledialog.askstring(
                "Tesseract PSM", PSM_HELP,
                initialvalue="6",
                parent=self.root,
            ) or "6"

            pre = simpledialog.askstring(
                "Preprocessing", PRE_HELP,
                initialvalue="auto",
                parent=self.root,
            ) or ""

        finally:
            self.root.attributes("-alpha", self.ALPHA)

        region = {
            "name":             name.strip(),
            "x": x1, "y": y1, "width": w, "height": h,
            "sheet":            sheet.strip(),
            "cell":             cell,
            "tesseract_config": f"--psm {psm.strip()}",
            "preprocess":       pre.strip() or None,
        }
        self.regions.append(region)
        self._draw_committed_box(x1, y1, x2, y2, name, sheet, cell, idx)
        self._box_drawing = None   # committed — don't delete on next press

    def _cancel_box(self) -> None:
        """User cancelled a dialog — remove the temporary rectangle."""
        if self._box_drawing is not None:
            self.canvas.delete(self._box_drawing)
            self._box_drawing = None

    def _draw_committed_box(self, x1: int, y1: int, x2: int, y2: int,
                             name: str, sheet: str, cell: str, idx: int) -> None:
        """Replace the green drag rect with a permanent labelled blue box."""
        if self._box_drawing is not None:
            self.canvas.delete(self._box_drawing)

        tag = f"region_{idx}"
        self.canvas.create_rectangle(
            x1, y1, x2, y2,
            outline=self.DONE_CLR, width=2,
            fill=self.DONE_CLR, stipple="gray25",
            tags=tag,
        )
        # Number badge
        self.canvas.create_oval(x1, y1, x1 + 22, y1 + 22,
                                fill=self.DONE_CLR, outline="", tags=tag)
        self.canvas.create_text(x1 + 11, y1 + 11, fill="black",
                                font=("Consolas", 9, "bold"), text=str(idx), tags=tag)
        # Label
        label = f"[{name}]  →  {sheet}!{cell}"
        self.canvas.create_text(x1 + 28, y1 + 6, anchor="nw",
                                fill=self.TXT_CLR, font=self.FONT_SMALL,
                                text=label, tags=tag)

    # ── undo ──────────────────────────────────────────────────────────────────

    def _undo(self, _ev: tk.Event) -> None:
        if not self.regions:
            return
        idx = len(self.regions)
        self.regions.pop()
        self.canvas.delete(f"region_{idx}")
        print(f"Removed region #{idx}")

    # ── finish & save ─────────────────────────────────────────────────────────

    def _on_done(self, _ev: object) -> None:
        self.root.destroy()

        if not self.regions:
            print("No regions defined — config.json unchanged.")
            return

        self._save_config()

    def _save_config(self) -> None:
        if self.config_path.exists():
            try:
                cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                cfg = dict(SKELETON)
        else:
            cfg = dict(SKELETON)

        cfg["regions"] = self.regions
        self.config_path.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        bar = "─" * 60
        print(f"\n{bar}")
        print(f"  Saved {len(self.regions)} region(s) to  {self.config_path}")
        print(bar)
        for r in self.regions:
            pos = f"({r['x']},{r['y']}) {r['width']}×{r['height']}"
            print(f"  {r['name']:<22}  {pos:<22}  →  {r['sheet']}!{r['cell']}")
        print(bar)
        print("\nNext step:  python ocr_daemon.py")
        print("            python ocr_daemon.py --test   ← verify regions first\n")


# ── entry point ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Interactive region selector — draws screen overlay to define OCR capture zones"
    )
    p.add_argument("--config", default="config.json",
                   help="Config file to write (default: config.json)")
    return p.parse_args()


def main() -> None:
    args  = parse_args()
    cpath = Path(args.config)
    print(f"Region Selector — results will be saved to  {cpath}")
    print("  • Left-drag to draw a region")
    print("  • Right-click to undo the last region")
    print("  • Press ESC or close the window when finished\n")
    RegionSelector(cpath)


if __name__ == "__main__":
    main()
