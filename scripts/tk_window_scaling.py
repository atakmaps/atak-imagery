"""Scale Tk window sizes to the user's display (small laptops → shrink, large monitors → grow).

Call sites pass "design" pixel sizes intended for a 1920×1080-class monitor. This module
scales proportionally to the available screen, clamps so the window fits within a margin
(taskbars / chrome), and centers the window.
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, List, Optional, Tuple

# Layout baseline matching typical design assumptions in this repo.
REF_SCREEN_W = 1920
REF_SCREEN_H = 1080
# Leave a margin so controls stay on-screen (title bar, panels, taskbars).
MARGIN_FRAC = 0.06
# Readability floor / cap so scaling stays reasonable on extremes.
MIN_SCALE = 0.52
MAX_SCALE = 1.35


def usable_screen_bounds(widget: tk.Misc) -> Tuple[int, int]:
    top = widget.winfo_toplevel()
    top.update_idletasks()
    sw = max(int(top.winfo_screenwidth()), 640)
    sh = max(int(top.winfo_screenheight()), 480)
    mx = max(320, int(sw * (1.0 - MARGIN_FRAC)))
    my = max(240, int(sh * (1.0 - MARGIN_FRAC)))
    return mx, my


def scale_factor(widget: tk.Misc) -> float:
    mx, my = usable_screen_bounds(widget)
    raw = min(mx / REF_SCREEN_W, my / REF_SCREEN_H)
    return max(MIN_SCALE, min(MAX_SCALE, raw))


def scaled_dimensions(widget: tk.Misc, base_w: int, base_h: int) -> Tuple[int, int, float]:
    mx, my = usable_screen_bounds(widget)
    s = scale_factor(widget)
    w = int(round(base_w * s))
    h = int(round(base_h * s))
    w = max(320, min(w, mx))
    h = max(240, min(h, my))
    return w, h, s


def _place_center(widget: tk.Misc, w: int, h: int) -> None:
    top = widget.winfo_toplevel()
    sw = max(int(top.winfo_screenwidth()), w)
    sh = max(int(top.winfo_screenheight()), h)
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 2)
    widget.geometry(f"{w}x{h}+{x}+{y}")


def apply_fixed_size_window(win: tk.Wm, base_w: int, base_h: int) -> float:
    """Fixed-size dialogs (resizable False). Returns scale for wraplength etc."""
    win.update_idletasks()
    w, h, s = scaled_dimensions(win, base_w, base_h)
    _place_center(win, w, h)
    try:
        win.minsize(w, h)
        win.maxsize(w, h)
    except tk.TclError:
        pass
    ensure_window_stacking(win)
    return s


def apply_resizable_window(win: tk.Wm, base_w: int, base_h: int, base_minsize: Tuple[int, int]) -> float:
    """Resizable main windows; initial geometry scaled; minsize scaled (clamped)."""
    win.update_idletasks()
    w, h, s = scaled_dimensions(win, base_w, base_h)
    _place_center(win, w, h)
    mw = int(round(base_minsize[0] * s))
    mh = int(round(base_minsize[1] * s))
    mw = max(280, min(mw, w))
    mh = max(200, min(mh, h))
    try:
        win.minsize(mw, mh)
    except tk.TclError:
        pass
    ensure_window_stacking(win)
    return s


def refit_toplevel_geometry(win: tk.Wm, base_w: int, base_h: int) -> float:
    """
    After children are packed: set size to max(design-at-scale, Tk's requested size),
    capped to usable screen, then re-center. Use so text wraps and lists lay out before
    the first paint at a resolution-aware size.
    """
    win.update_idletasks()
    mx, my = usable_screen_bounds(win)
    s = scale_factor(win)
    w_ds = max(320, min(int(round(base_w * s)), mx))
    h_ds = max(240, min(int(round(base_h * s)), my))
    try:
        reqw = int(win.winfo_reqwidth())
        reqh = int(win.winfo_reqheight())
    except tk.TclError:
        reqw, reqh = w_ds, h_ds
    w = min(max(reqw, w_ds), mx)
    h = min(max(reqh, h_ds), my)
    _place_center(win, w, h)
    return s


def scaled_int(base_px: int, scale: float) -> int:
    return max(80, int(round(base_px * scale)))


def scaled_gap_px(base_px: int, scale: float, *, lo: int = 4, hi: int = 48) -> int:
    """Scale margins and small vertical gaps. ``scaled_int`` floors at 80px for readability on
    wrap widths; do not use that floor between UI sections or indents look absurd."""
    return max(lo, min(hi, int(round(base_px * scale))))


def pack_vertical_scroll_area(parent: tk.Misc) -> tk.Frame:
    """
    Pack a ``Canvas`` + vertical ``Scrollbar`` into ``parent`` (fills ``parent``),
    and return an ``inner`` ``Frame`` to hold scrollable children. Width tracks the canvas.

    Use on small displays (e.g. 1024×768) so long checkbox lists stay reachable.
    """
    holder = tk.Frame(parent)
    holder.pack(fill=tk.BOTH, expand=True)
    canvas = tk.Canvas(holder, highlightthickness=0)
    vsb = tk.Scrollbar(holder, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas)
    win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _on_inner_configure(_event: object) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_canvas_configure(event: tk.Event) -> None:
        try:
            canvas.itemconfigure(win_id, width=event.width)
        except tk.TclError:
            pass

    inner.bind("<Configure>", _on_inner_configure)
    canvas.bind("<Configure>", _on_canvas_configure)
    canvas.configure(yscrollcommand=vsb.set)

    def _wheel_mousewheel(event: tk.Event) -> str | None:
        if getattr(event, "num", None) == 5:
            canvas.yview_scroll(1, "units")
        elif getattr(event, "num", None) == 4:
            canvas.yview_scroll(-1, "units")
        elif getattr(event, "delta", 0):
            canvas.yview_scroll(int(-1 * event.delta / 120), "units")
        return "break"

    for w in (canvas, inner):
        w.bind("<MouseWheel>", _wheel_mousewheel)
        w.bind("<Button-4>", _wheel_mousewheel)
        w.bind("<Button-5>", _wheel_mousewheel)

    vsb.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    return inner


def pack_vertical_scroll_area_when_needed(
    parent: tk.Misc,
    *,
    on_inner_layout: Optional[Callable[[], None]] = None,
) -> tk.Frame:
    """
    Like ``pack_vertical_scroll_area``, but the vertical scrollbar is only gridded when
    the inner content is taller than the canvas. When everything fits, no scrollbar is shown.
    """
    holder = tk.Frame(parent)
    holder.pack(fill=tk.BOTH, expand=True)
    holder.grid_columnconfigure(0, weight=1)
    holder.grid_rowconfigure(0, weight=1)

    canvas = tk.Canvas(holder, highlightthickness=0)
    vsb = tk.Scrollbar(holder, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas)
    win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
    scroll_active: List[bool] = [False]

    def _sync_vsb() -> None:
        canvas.update_idletasks()
        inner.update_idletasks()
        try:
            ch = int(canvas.winfo_height())
        except tk.TclError:
            return
        if ch <= 1:
            canvas.after_idle(_sync_vsb)
            return
        try:
            ih = int(inner.winfo_reqheight())
        except tk.TclError:
            ih = 0
        bbox = canvas.bbox("all")
        if bbox is not None:
            y1, y2 = int(bbox[1]), int(bbox[3])
            ih = max(ih, y2 - y1)
        content_h = ih
        need = content_h > ch + 2
        scroll_active[0] = need
        if need:
            vsb.grid(row=0, column=1, sticky="ns")
        else:
            vsb.grid_remove()
            canvas.yview_moveto(0)
        canvas.configure(yscrollcommand=vsb.set)

    def _on_inner_configure(_event: object) -> None:
        if on_inner_layout is not None:
            on_inner_layout()
        canvas.update_idletasks()
        br = canvas.bbox("all")
        canvas.configure(scrollregion=br if br else (0, 0, 0, 0))
        _sync_vsb()

    def _on_canvas_configure(event: tk.Event) -> None:
        try:
            w = int(event.width)
            if w > 8:
                canvas.itemconfigure(win_id, width=w)
        except tk.TclError:
            pass
        _sync_vsb()

    inner.bind("<Configure>", _on_inner_configure)
    canvas.bind("<Configure>", _on_canvas_configure)
    canvas.configure(yscrollcommand=vsb.set)

    def _wheel_mousewheel(event: tk.Event) -> str | None:
        if scroll_active[0]:
            if getattr(event, "num", None) == 5:
                canvas.yview_scroll(1, "units")
            elif getattr(event, "num", None) == 4:
                canvas.yview_scroll(-1, "units")
            elif getattr(event, "delta", 0):
                canvas.yview_scroll(int(-1 * event.delta / 120), "units")
        return "break"

    for wdg in (canvas, inner):
        wdg.bind("<MouseWheel>", _wheel_mousewheel)
        wdg.bind("<Button-4>", _wheel_mousewheel)
        wdg.bind("<Button-5>", _wheel_mousewheel)

    canvas.grid(row=0, column=0, sticky="nsew")
    try:
        vsb.grid_remove()
    except tk.TclError:
        pass

    holder.after_idle(_sync_vsb)
    holder.after(120, _sync_vsb)
    return inner


def ensure_window_stacking(
    win: tk.Misc,
    *,
    above: Optional[tk.Misc] = None,
    persistent_topmost: bool = False,
) -> None:
    """Keep a window reliably in front on Linux/X11 WMs.

    Many compositors (GNOME/Mutter, KDE/KWin) honor ``-topmost`` only after the window
    is fully mapped, which can take several frames.  Three quick nudges (lift + topmost)
    spaced 150 ms apart are enough to beat WM timing without causing visible flashing.
    After the nudges the flag is cleared (unless ``persistent_topmost=True``, in which
    case the window stays on top and is re-nudged every 5 seconds).
    """
    try:
        top = win.winfo_toplevel()
    except tk.TclError:
        return

    # Track nudge timers per toplevel so stale callbacks do not survive teardown.
    registry = getattr(top, "_atak_nudge_after_ids", None)
    if registry is None:
        registry = []
        setattr(top, "_atak_nudge_after_ids", registry)

        def _clear_nudges_on_destroy(_evt: object = None) -> None:
            ids = getattr(top, "_atak_nudge_after_ids", [])
            for aid in list(ids):
                try:
                    top.after_cancel(aid)
                except (tk.TclError, ValueError):
                    pass
            ids.clear()

        try:
            top.bind("<Destroy>", _clear_nudges_on_destroy, add="+")
        except tk.TclError:
            pass
    else:
        # Cancel any prior stacking nudges for this same toplevel before scheduling new ones.
        for aid in list(registry):
            try:
                top.after_cancel(aid)
            except (tk.TclError, ValueError):
                pass
        registry.clear()

    # Three quick nudges spaced 150 ms apart to beat WM restack timing without
    # visible flashing. focus_force() is intentionally omitted — it caused the
    # rapid-pulse appearance reported by the user.
    _MAX_NUDGES = 3
    _nudge_count = [0]

    def _nudge() -> None:
        try:
            if above is not None:
                try:
                    top.lift(above)
                except tk.TclError:
                    top.lift()
            else:
                top.lift()
            top.attributes("-topmost", True)
        except tk.TclError:
            return

        _nudge_count[0] += 1
        if persistent_topmost:
            aid = top.after(5000, _nudge)
            registry.append(aid)
        elif _nudge_count[0] < _MAX_NUDGES:
            aid = top.after(150, _nudge)
            registry.append(aid)
        else:
            try:
                top.attributes("-topmost", False)
            except tk.TclError:
                pass

    _nudge()


def cancel_all_scheduled_after(w: tk.Misc) -> None:
    """Cancel every pending ``after`` timer on this Tk interpreter.

    Call before destroying a toplevel that used :func:`ensure_window_stacking` or other
    ``after``-scheduled work; otherwise Linux/X11 may abort with
    ``Tcl_AsyncDelete: async handler deleted by the wrong thread``.
    """
    try:
        raw = w.tk.call("after", "info")
    except tk.TclError:
        return
    if not raw:
        return
    for aid in w.tk.splitlist(raw):
        try:
            w.after_cancel(aid)
        except (tk.TclError, ValueError):
            pass
