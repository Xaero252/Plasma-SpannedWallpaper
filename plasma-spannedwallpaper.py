#!/usr/bin/env python
"""
plasma-spannedwallpaper.py — Span a single image across all KDE Plasma monitors.

GUI mode (default):
    python3 plasma-spannedwallpaper.py
    python3 plasma-spannedwallpaper.py path/to/image.jpg

Headless / CLI mode (skips the GUI entirely):
    python3 plasma-spannedwallpaper.py image.jpg --no-gui
    python3 plasma-spannedwallpaper.py image.jpg --no-gui --scale-mode fit
    python3 plasma-spannedwallpaper.py image.jpg --no-gui --dry-run
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from typing import Optional

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("Pillow is required:  sudo pacman -S python-pillow")

try:
    from screeninfo import get_monitors
except ImportError:
    sys.exit("screeninfo is required:  pip install --user screeninfo")

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
    from PIL import ImageTk
    _TK_AVAILABLE = True
except (ImportError, OSError):
    _TK_AVAILABLE = False


# ============================================================================
# Monitor detection & Canvas Math
# ============================================================================

def detect_monitors() -> list[dict]:
    monitors = get_monitors()
    if not monitors:
        sys.exit("No monitors detected. Is a display server running?")
    result = [
        {"name": m.name or f"monitor_{i}",
         "x": m.x, "y": m.y, "width": m.width, "height": m.height}
        for i, m in enumerate(monitors)
    ]
    result.sort(key=lambda m: (m["x"], m["y"]))
    return result

def compute_canvas(monitors: list[dict]) -> tuple[int, int, int, int]:
    """Return (min_x, min_y, canvas_w, canvas_h)."""
    min_x = min(m["x"] for m in monitors)
    min_y = min(m["y"] for m in monitors)
    max_x = max(m["x"] + m["width"]  for m in monitors)
    max_y = max(m["y"] + m["height"] for m in monitors)
    return min_x, min_y, max_x - min_x, max_y - min_y

def compute_image_layout(src_w: int, src_h: int, canvas_w: int, canvas_h: int, 
                         user_scale: float, pan_x: float, pan_y: float) -> tuple[int, int, int, int]:
    """Calculate and return (new_w, new_h, crop_left, crop_top) for the scaled image."""
    src_ratio    = src_w / src_h
    canvas_ratio = canvas_w / canvas_h
    
    if src_ratio > canvas_ratio:
        base_h = canvas_h
        base_w = int(src_w * canvas_h / src_h)
    else:
        base_w = canvas_w
        base_h = int(src_h * canvas_w / src_w)

    new_w = max(canvas_w, int(base_w * user_scale))
    new_h = max(canvas_h, int(base_h * user_scale))

    slack_x = new_w - canvas_w
    slack_y = new_h - canvas_h
    left = int(slack_x * (0.5 + pan_x * 0.5))
    top  = int(slack_y * (0.5 + pan_y * 0.5))
    
    left = max(0, min(left, slack_x))
    top  = max(0, min(top,  slack_y))

    return new_w, new_h, left, top


# ============================================================================
# Image composition (Backend Processing)
# ============================================================================

def build_canvas_image(src: Image.Image, monitors: list[dict], user_scale: float, pan_x: float, pan_y: float) -> Image.Image:
    """Return a pristine, high-quality cropped PIL Image exactly the size of the virtual desktop canvas."""
    _, _, canvas_w, canvas_h = compute_canvas(monitors)
    new_w, new_h, left, top = compute_image_layout(src.width, src.height, canvas_w, canvas_h, user_scale, pan_x, pan_y)

    scaled = src.resize((new_w, new_h), Image.LANCZOS)
    return scaled.crop((left, top, left + canvas_w, top + canvas_h))

def _slice_subdir(base_output_dir: str, image_path: str) -> str:
    import hashlib
    abs_path = os.path.abspath(image_path)
    stem     = os.path.splitext(os.path.basename(abs_path))[0]
    safe_stem = "".join(c if c.isalnum() or c in ".-" else "_" for c in stem)
    short_hash = hashlib.sha1(abs_path.encode()).hexdigest()[:8]
    return os.path.join(base_output_dir, f"{safe_stem}_{short_hash}")

def slice_and_save(src: Image.Image, monitors: list[dict], output_dir: str, 
                   user_scale: float, pan_x: float, pan_y: float, image_path: str = "") -> list[tuple[dict, str]]:
    canvas_img = build_canvas_image(src, monitors, user_scale, pan_x, pan_y)
    min_x, min_y, _, _ = compute_canvas(monitors)

    out_dir = _slice_subdir(output_dir, image_path) if image_path else output_dir
    os.makedirs(out_dir, exist_ok=True)

    slices = []
    for idx, mon in enumerate(monitors):
        rel_x = mon["x"] - min_x
        rel_y = mon["y"] - min_y
        box   = (rel_x, rel_y, rel_x + mon["width"], rel_y + mon["height"])
        sl    = canvas_img.crop(box)
        path  = os.path.join(out_dir, f"{mon['name'].replace('/', '_')}.png")
        sl.save(path, "PNG")
        slices.append((mon, path))
        print(f"  [slice] {mon['name']}  {mon['width']}x{mon['height']}  -> {path}")
    return slices


# ============================================================================
# KDE Plasma wallpaper application
# ============================================================================

QDBUS_CONNECTOR = """\
var allDesktops = desktops();
for (var i = 0; i < allDesktops.length; i++) {{
    var d = allDesktops[i];
    if (d.screen === screenForConnector("{connector}")) {{
        d.wallpaperPlugin = "org.kde.image";
        d.currentConfigGroup = ["Wallpaper", "org.kde.image", "General"];
        d.writeConfig("Image", "file://{path}");
        d.writeConfig("FillMode", {fill});
    }}
}}
"""

QDBUS_GEOMETRY = """\
var allDesktops = desktops();
for (var i = 0; i < allDesktops.length; i++) {{
    var d = allDesktops[i];
    var g = screenGeometry(d.screen);
    if (g.x==={x} && g.y==={y} && g.width==={w} && g.height==={h}) {{
        d.wallpaperPlugin = "org.kde.image";
        d.currentConfigGroup = ["Wallpaper", "org.kde.image", "General"];
        d.writeConfig("Image", "file://{path}");
        d.writeConfig("FillMode", {fill});
    }}
}}
"""

FILL_MODES = {"zoom": 6, "fit": 1, "stretch": 0, "crop": 2}

def find_qdbus() -> str:
    for name in ("qdbus6", "qdbus"):
        try:
            subprocess.run([name, "--version"], capture_output=True, check=True)
            return name
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
    sys.exit("Neither qdbus6 nor qdbus found. Install qt6-tools or qt5-tools.")

def _qdbus(script: str, qdbus: str) -> tuple[bool, str]:
    r = subprocess.run(
        [qdbus, "org.kde.plasmashell", "/PlasmaShell", "org.kde.PlasmaShell.evaluateScript", script],
        capture_output=True, text=True,
    )
    return (r.returncode == 0 and "error" not in r.stdout.lower()), (r.stderr or r.stdout).strip()

def apply_wallpaper(mon: dict, image_path: str, fill: int, qdbus: str, dry_run: bool):
    p  = os.path.abspath(image_path).replace("\\", "/")
    s1 = QDBUS_CONNECTOR.format(connector=mon["name"], path=p, fill=fill)
    s2 = QDBUS_GEOMETRY.format(x=mon["x"], y=mon["y"], w=mon["width"], h=mon["height"], path=p, fill=fill)

    if dry_run:
        print(f"  [dry-run] {mon['name']}: {p}")
        return

    ok, msg = _qdbus(s1, qdbus)
    if ok:
        print(f"  [kde] {mon['name']}: set via connector name  OK")
        return
    ok, msg = _qdbus(s2, qdbus)
    if ok:
        print(f"  [kde] {mon['name']}: set via geometry  OK")
    else:
        print(f"  [error] {mon['name']}: failed -- {msg}", file=sys.stderr)


# ============================================================================
# GUI
# ============================================================================

ZOOM_STEP  = 0.08
MON_COLORS = ["#4fc3f7", "#81c784", "#ffb74d", "#e57373", "#ce93d8"]

DARK_BG  = "#1e1e2e"
PANEL_BG = "#2a2a3e"
ACCENT   = "#7c6af7"
FG       = "#cdd6f4"
FG_DIM   = "#6c7086"
BTN_BG   = "#313244"
BTN_HOV  = "#45475a"

class WallpaperApp(tk.Tk):

    def __init__(self, initial_image: Optional[str] = None):
        super().__init__()
        self.title("Plasma Spanned Wallpaper")
        self.configure(bg=DARK_BG)
        self.resizable(True, True)
        self.minsize(860, 520)

        self.monitors   = detect_monitors()
        self.src_image: Optional[Image.Image] = None
        self.image_path: Optional[str]        = None

        self.user_scale     = 1.0
        self.pan_x          = 0.0
        self.pan_y          = 0.0
        self._drag_start_xy:   Optional[tuple[int, int]]     = None
        self._drag_pan_origin: Optional[tuple[float, float]] = None

        self._build_ui()
        self._update_monitor_strip()

        if initial_image:
            self._load_image(initial_image)

    def _build_ui(self):
        btn_kw = dict(bg=BTN_BG, fg=FG, relief="flat", padx=14, pady=5, font=("sans-serif", 10), cursor="hand2", activebackground=BTN_HOV, activeforeground=FG, bd=0)

        toolbar = tk.Frame(self, bg=PANEL_BG, pady=8, padx=12)
        toolbar.pack(fill="x", side="top")
        tk.Label(toolbar, text="Plasma Spanned Wallpaper", bg=PANEL_BG, fg=FG, font=("sans-serif", 13, "bold")).pack(side="left")

        self.apply_btn = tk.Button(toolbar, text="Apply Wallpaper", command=self._on_apply, bg=ACCENT, activebackground="#6a5ae0", fg=FG, relief="flat", padx=16, pady=5, font=("sans-serif", 10, "bold"), cursor="hand2", activeforeground=FG, bd=0)
        self.apply_btn.pack(side="right", padx=(6, 0))

        tk.Button(toolbar, text="Reset View", command=self._reset_view, **btn_kw).pack(side="right", padx=(6, 0))
        tk.Button(toolbar, text="Open Image", command=self._open_dialog, **btn_kw).pack(side="right", padx=(6, 0))

        optbar = tk.Frame(self, bg=DARK_BG, pady=5, padx=14)
        optbar.pack(fill="x", side="top")
        tk.Label(optbar, text="KDE fill mode per slice:", bg=DARK_BG, fg=FG_DIM, font=("sans-serif", 9)).pack(side="left")

        self.fill_var = tk.StringVar(value="zoom")
        for mode in FILL_MODES:
            tk.Radiobutton(optbar, text=mode, variable=self.fill_var, value=mode, bg=DARK_BG, fg=FG, selectcolor=PANEL_BG, activebackground=DARK_BG, activeforeground=FG, font=("sans-serif", 9)).pack(side="left", padx=8)

        tk.Label(optbar, text="Scroll = zoom   Drag = pan", bg=DARK_BG, fg=FG_DIM, font=("sans-serif", 8)).pack(side="right")

        canvas_outer = tk.Frame(self, bg="#0d0d1a")
        canvas_outer.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(canvas_outer, bg="#0d0d1a", highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>",       lambda _e: self._refresh_preview())
        self.canvas.bind("<ButtonPress-1>",   self._on_drag_start)
        self.canvas.bind("<B1-Motion>",       self._on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self.canvas.bind("<MouseWheel>",      self._on_scroll) 
        self.canvas.bind("<Button-4>",        self._on_scroll) 
        self.canvas.bind("<Button-5>",        self._on_scroll) 

        self._draw_placeholder()

        self.mon_strip = tk.Frame(self, bg=PANEL_BG, pady=5, padx=12)
        self.mon_strip.pack(fill="x", side="bottom")

        self.status_var = tk.StringVar(value="Open an image to get started.")
        tk.Label(self, textvariable=self.status_var, bg=DARK_BG, fg=FG_DIM, font=("sans-serif", 8), anchor="w", padx=14).pack(fill="x", side="bottom", pady=(0, 3))

    def _update_monitor_strip(self):
        for w in self.mon_strip.winfo_children(): w.destroy()
        tk.Label(self.mon_strip, text="Monitors:", bg=PANEL_BG, fg=FG_DIM, font=("sans-serif", 9)).pack(side="left", padx=(0, 8))
        for i, m in enumerate(self.monitors):
            tk.Label(self.mon_strip, text=f"  {m['name']}  {m['width']}x{m['height']}  ", bg=MON_COLORS[i % len(MON_COLORS)], fg="#0d0d1a", font=("sans-serif", 9, "bold"), padx=4, pady=2, relief="flat").pack(side="left", padx=4)

    def _draw_placeholder(self):
        self.canvas.delete("all")
        cw, ch = max(self.canvas.winfo_width(), 100), max(self.canvas.winfo_height(), 100)
        self.canvas.create_rectangle(0, 0, cw, ch, fill="#0d0d1a", outline="")

        _, _, virt_w, virt_h = compute_canvas(self.monitors)
        scale = min(cw / virt_w, ch / virt_h) * 0.80
        ox, oy = (cw - int(virt_w * scale)) // 2, (ch - int(virt_h * scale)) // 2
        min_x, min_y, _, _ = compute_canvas(self.monitors)

        for i, mon in enumerate(self.monitors):
            color = MON_COLORS[i % len(MON_COLORS)]
            rx = ox + int((mon["x"] - min_x) * scale)
            ry = oy + int((mon["y"] - min_y) * scale)
            rw = int(mon["width"] * scale)
            rh = int(mon["height"] * scale)
            
            self.canvas.create_rectangle(rx, ry, rx + rw, ry + rh, outline=color, width=3, fill="#1a1a2e")
            self.canvas.create_text(rx + rw // 2, ry + rh // 2, text=f"{mon['name']}\n{mon['width']}x{mon['height']}", fill=color, font=("sans-serif", 13, "bold"), justify="center")

        self.canvas.create_text(cw // 2, oy - 25, text="Open an image to preview the span layout", fill=FG_DIM, font=("sans-serif", 12))

    def _open_dialog(self):
        path = filedialog.askopenfilename(title="Choose wallpaper image", filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp *.webp *.tiff *.tif"), ("All files", "*.*")])
        if path: self._load_image(path)

    def _load_image(self, path: str):
        try:
            img = Image.open(path).convert("RGB")
        except Exception as exc:
            messagebox.showerror("Cannot open image", str(exc))
            return
            
        self.src_image  = img
        self.image_path = path
        self.user_scale = 1.0
        self.pan_x, self.pan_y = 0.0, 0.0
        
        # ⚡ OPTIMIZATION 1: Create a max 1920x1920 thumbnail to use exclusively during GUI interactions.
        self.preview_image = img.copy()
        self.preview_image.thumbnail((1920, 1920), Image.LANCZOS)
        
        self.status_var.set(f"{os.path.basename(path)}   {img.width}x{img.height} px   |   {len(self.monitors)} monitor(s) detected")
        self._refresh_preview()

    def _refresh_preview(self):
        if self.src_image is None or not hasattr(self, 'preview_image'):
            self._draw_placeholder()
            return

        cw = max(self.canvas.winfo_width(), 100)
        ch = max(self.canvas.winfo_height(), 100)
        min_x, min_y, virt_w, virt_h = compute_canvas(self.monitors)

        # Calculate where the full image maps to the virtual canvas bounds
        new_w, new_h, left, top = compute_image_layout(
            self.src_image.width, self.src_image.height, virt_w, virt_h, self.user_scale, self.pan_x, self.pan_y
        )

        # Calculate scale to fit the virtual canvas into the GUI widget comfortably
        display_scale = min(cw / virt_w, ch / virt_h) * 0.85
        ox = (cw - (virt_w * display_scale)) / 2
        oy = (ch - (virt_h * display_scale)) / 2

        # ⚡ OPTIMIZATION 2: Resize from the cached thumbnail using ultra-fast BILINEAR math
        disp_img_w = int(new_w * display_scale)
        disp_img_h = int(new_h * display_scale)
        disp_img_x = int(ox - (left * display_scale))
        disp_img_y = int(oy - (top * display_scale))

        scaled_preview = self.preview_image.resize((disp_img_w, disp_img_h), Image.BILINEAR)

        # Create Background and Paste full image
        bg = Image.new("RGBA", (cw, ch), (13, 13, 26, 255))
        bg.paste(scaled_preview, (disp_img_x, disp_img_y))

        # ⚡ FADED REGIONS: Create an alpha mask to "punch holes" for the monitors
        mask = Image.new("L", (cw, ch), 160) # 160/255 Darkness level
        mask_draw = ImageDraw.Draw(mask)

        for mon in self.monitors:
            rx = ox + (mon["x"] - min_x) * display_scale
            ry = oy + (mon["y"] - min_y) * display_scale
            rw = mon["width"] * display_scale
            rh = mon["height"] * display_scale
            mask_draw.rectangle([rx, ry, rx + rw, ry + rh], fill=0) # 0 = fully transparent hole

        # Apply darkness to the outside bounds
        overlay = Image.new("RGBA", (cw, ch), (0, 0, 0, 255))
        overlay.putalpha(mask)
        bg = Image.alpha_composite(bg, overlay)

        # Map rendered PIL image to Tkinter
        self.canvas.delete("all")
        self._tk_image = ImageTk.PhotoImage(bg)
        self.canvas.create_image(0, 0, anchor="nw", image=self._tk_image)

        # ⚡ BETTER TEXT: Draw borders and text natively in Tkinter using scalable vector fonts
        for i, mon in enumerate(self.monitors):
            color = MON_COLORS[i % len(MON_COLORS)]
            rx = ox + (mon["x"] - min_x) * display_scale
            ry = oy + (mon["y"] - min_y) * display_scale
            rw = mon["width"] * display_scale
            rh = mon["height"] * display_scale

            self.canvas.create_rectangle(rx, ry, rx + rw, ry + rh, outline=color, width=3)

            label  = f"{mon['name']}\n{mon['width']}x{mon['height']}"
            cx, cy = rx + rw/2, ry + rh/2
            
            # Shadow
            self.canvas.create_text(cx + 2, cy + 2, text=label, fill="#000000", font=("sans-serif", 13, "bold"), justify="center")
            # Text
            self.canvas.create_text(cx, cy, text=label, fill=color, font=("sans-serif", 13, "bold"), justify="center")

        zoom_pct = int(self.user_scale * 100)
        self.canvas.create_text(cw - 8, ch - 8, text=f"zoom {zoom_pct}%   pan ({self.pan_x:+.2f}, {self.pan_y:+.2f})", fill=FG_DIM, font=("monospace", 9, "bold"), anchor="se")

    def _on_scroll(self, event):
        if self.src_image is None: return
        if event.num == 4 or (hasattr(event, "delta") and event.delta > 0): self.user_scale = min(self.user_scale + ZOOM_STEP, 6.0)
        else: self.user_scale = max(self.user_scale - ZOOM_STEP, 0.15)
        self._refresh_preview()

    def _on_drag_start(self, event):
        self._drag_start_xy    = (event.x, event.y)
        self._drag_pan_origin  = (self.pan_x, self.pan_y)

    def _on_drag_move(self, event):
        if self._drag_start_xy is None or self.src_image is None: return
        dx, dy = event.x - self._drag_start_xy[0], event.y - self._drag_start_xy[1]
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        
        self.pan_x = max(-1.0, min(1.0, self._drag_pan_origin[0] - dx / (cw * 0.45)))
        self.pan_y = max(-1.0, min(1.0, self._drag_pan_origin[1] - dy / (ch * 0.45)))
        self._refresh_preview()

    def _on_drag_end(self, _event):
        self._drag_start_xy, self._drag_pan_origin = None, None

    def _reset_view(self):
        self.user_scale, self.pan_x, self.pan_y = 1.0, 0.0, 0.0
        self._refresh_preview()

    def _on_apply(self):
        if self.src_image is None:
            messagebox.showwarning("No image", "Please open an image first.")
            return

        self.apply_btn.config(state="disabled", text="Applying...")
        self.status_var.set("Slicing and setting wallpapers...")
        self.update_idletasks()

        def _worker():
            try:
                out_dir = os.path.expanduser("~/.local/share/wallpapers/span")
                slices  = slice_and_save(self.src_image, self.monitors, out_dir, self.user_scale, self.pan_x, self.pan_y, image_path=self.image_path or "")
                fill  = FILL_MODES[self.fill_var.get()]
                qdbus = find_qdbus()
                for mon, path in slices: apply_wallpaper(mon, path, fill, qdbus, dry_run=False)

                msg = f"Wallpapers applied to {len(slices)} monitor(s).\nSlices saved to:\n{out_dir}"
                self.after(0, lambda: self.status_var.set(f"Done!  {len(slices)} wallpaper(s) applied.  Slices at {out_dir}"))
                self.after(0, lambda: messagebox.showinfo("Done", msg))
            except Exception as exc:
                self.after(0, lambda: self.status_var.set(f"Error: {exc}"))
                self.after(0, lambda: messagebox.showerror("Error", str(exc)))
            finally:
                self.after(0, lambda: self.apply_btn.config(state="normal", text="Apply Wallpaper"))

        threading.Thread(target=_worker, daemon=True).start()


# ============================================================================
# CLI (headless) path
# ============================================================================

def run_cli(args):
    if not os.path.isfile(args.image): sys.exit(f"Image not found: {args.image}")

    print("=== Detecting monitors ===")
    monitors = detect_monitors()
    for i, m in enumerate(monitors): print(f"  [{i}] {m['name']}  {m['width']}x{m['height']}  offset=({m['x']}, {m['y']})")

    src = Image.open(args.image).convert("RGB")
    out_dir = args.output_dir or os.path.expanduser("~/.local/share/wallpapers/span")

    print("\n=== Slicing image ===")
    slices = slice_and_save(src, monitors, out_dir, user_scale=1.0, pan_x=0.0, pan_y=0.0, image_path=args.image)

    fill = FILL_MODES[args.scale_mode]
    qdbus = None if args.dry_run else find_qdbus()

    print(f"\n=== Setting wallpapers (fill={args.scale_mode}) ===")
    for mon, path in slices: apply_wallpaper(mon, path, fill, qdbus or "qdbus6", args.dry_run)
    print("\n[dry-run complete]" if args.dry_run else "\nDone!")


def main():
    parser = argparse.ArgumentParser(description="Span a wallpaper image across all KDE Plasma monitors.")
    parser.add_argument("image", nargs="?", default=None, help="Path to the image (optional in GUI mode)")
    parser.add_argument("--no-gui", action="store_true", help="Headless mode — requires image argument")
    parser.add_argument("--scale-mode", choices=list(FILL_MODES), default="zoom", help="KDE fill mode (CLI only, default: zoom)")
    parser.add_argument("--output-dir", default=None, help="Slice output directory (default: ~/.local/share/wallpapers/span/)")
    parser.add_argument("--dry-run", action="store_true", help="Slice image but do not set wallpapers (CLI only)")
    args = parser.parse_args()

    if args.no_gui:
        if not args.image: parser.error("--no-gui requires an image path.")
        run_cli(args)
        return

    if not _TK_AVAILABLE:
        print("tkinter unavailable — falling back to CLI mode.", file=sys.stderr)
        if not args.image: sys.exit("No image specified and GUI is unavailable.")
        run_cli(args)
        return

    app = WallpaperApp(initial_image=args.image)
    app.mainloop()

if __name__ == "__main__":
    main()