#!/usr/bin/env python3
"""THE ROOM daily cards v3 — the duel is the hero, price is context.

Two templates, one frozen visual system (palette + Inter Tight/tabular unchanged):
  build_today_card       — Template B: two symmetric bet panels + context sparkline.
  build_resolution_card  — Template A: closing price + tilted winner stamp.
"""
import colorsys, textwrap, requests, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Polygon, Rectangle
from matplotlib.transforms import Affine2D
from matplotlib import font_manager
from datetime import datetime, timezone

BG      = "#0A0A0A"
TEXT    = "#F2F0EB"
ORACLE  = "#A78BFA"
GUARDIAN= "#F2B544"
WIN     = "#4FCE7F"
LOSS    = "#EF6A6A"
MUTED   = "#6B7280"
DGREEN  = "#41E47D"  # direction: price/bet up · CORRECT in the resolution
DRED    = "#FF383C"  # direction: price/bet down · WRONG in the resolution
OUTLINE = "#3A3F46"

_INSTALLED = {f.name for f in font_manager.fontManager.ttflist}

def _tabular(fam):
    f = plt.figure(); a = f.text(0, 0, "0", fontfamily=fam); b = f.text(0, 0, "1", fontfamily=fam)
    f.canvas.draw(); r = f.canvas.get_renderer()
    ok = abs(a.get_window_extent(r).width - b.get_window_extent(r).width) < 0.5
    plt.close(f); return ok

HEAD = next((c for c in ["Inter Tight", "Inter", "DejaVu Sans"] if c in _INSTALLED), "DejaVu Sans")
NUM  = next((c for c in ["Inter Tight", "Inter", "DejaVu Sans"]
             if c in _INSTALLED and _tabular(c)), "DejaVu Sans")
plt.rcParams["font.family"] = HEAD
# render "$" literally everywhere — never interpret paired $ as a math formula
plt.rcParams["text.parse_math"] = False


def _at_light(hex_c, l):
    r, g, b = (int(hex_c[i:i+2], 16) / 255 for i in (1, 3, 5))
    h, _, s = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#%02x%02x%02x" % (round(r*255), round(g*255), round(b*255))


def conf_bucket(conf):
    p = conf * 100
    if p <= 60:
        return "lean"
    if p <= 70:
        return "confident"
    return "conviction"


def _wfrac(t, fig):
    return t.get_window_extent(fig.canvas.get_renderer()).width / fig.bbox.width


def _fit(fig, t, max_w):
    """Shrink a text object until it fits within max_w (figure fraction)."""
    fig.canvas.draw()
    w = _wfrac(t, fig)
    if w > max_w:
        t.set_fontsize(t.get_fontsize() * max_w / w)


def load_closes(n=14):
    r = requests.get("https://data-api.binance.vision/api/v3/klines",
                     params={"symbol": "BTCUSDT", "interval": "1d", "limit": n + 1}, timeout=15)
    r.raise_for_status()
    return [float(k[4]) for k in r.json()][-n:]


def _shaft_frac(conf):
    """Shaft length in figure fraction: 55% -> ~24px, 80% -> ~64px, linear."""
    pct = min(max(conf * 100, 55), 80)
    return (24 + (pct - 55) / 25 * 40) / 1080


def _arrow(fig, x, mid_y, up, color, conf):
    """Direction arrow = triangle head + a confidence-length vertical shaft,
    centred vertically at mid_y. up: head up / shaft down (Guardian: mirrored)."""
    HH, HW, SW = 0.026, 0.020, 0.008
    sf = _shaft_frac(conf)
    total = HH + sf
    if up:
        apex = mid_y + total / 2
        base = apex - HH
        sy = base - sf
    else:
        apex = mid_y - total / 2
        base = apex + HH
        sy = base
    tri = [(x, apex), (x - HW, base), (x + HW, base)]
    fig.patches.append(Polygon(tri, closed=True, facecolor=color, edgecolor="none",
                               transform=fig.transFigure, zorder=4))
    fig.patches.append(Rectangle((x - SW / 2, sy), SW, sf, facecolor=color,
                                 edgecolor="none", transform=fig.transFigure, zorder=4))


def _sparkline(fig, rect, closes, level):
    ax = fig.add_axes(rect); ax.patch.set_visible(False)
    ax.set_zorder(5)  # draw above the context block behind it
    lo, hi = min(min(closes), level), max(max(closes), level)
    pad = (hi - lo) * 0.20 or hi * 0.01
    ax.set_ylim(lo - pad, hi + pad); ax.set_xlim(-0.3, len(closes) - 0.7)
    ax.axhline(level, color=TEXT, ls=(0, (4, 3)), lw=1.0, alpha=0.45, zorder=1)
    ax.plot(range(len(closes)), closes, color=TEXT, lw=2.2,
            solid_capstyle="round", solid_joinstyle="round", zorder=2)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks([]); ax.set_yticks([]); ax.grid(False)


CTX_BG = "#17171A"  # a hair lighter than the background
# panel lightness by confidence bucket, tuned for text contrast (hue unchanged)
_VIOLET_L = {"lean": 0.45, "confident": 0.38, "conviction": 0.31}   # light text
_AMBER_L  = {"lean": 0.72, "confident": 0.65, "conviction": 0.58}   # dark text


def build_today_card(out, price, predictions, level, case_no, footer_left,
                     footer_right, closes=None):
    """Template B — owner's sandwich: Oracle panel on top, the level + price in
    the middle, Guardian panel on the bottom."""
    if closes is None:
        closes = load_closes(14)
    day_chg = (closes[-1] / closes[-2] - 1) * 100
    by = {p["agent"]: p for p in predictions}
    fig = plt.figure(figsize=(10.8, 10.8), dpi=100, facecolor=BG)

    # 1. header: brand left, case right
    fig.text(0.05, 0.955, "The ROOM", color=TEXT, fontsize=20, fontweight="bold", va="center")
    fig.text(0.95, 0.955, f"Case no.{case_no}", color=MUTED, fontsize=17, ha="right", va="center")

    # panels (2 & 4): Oracle violet on top, Guardian amber on the bottom
    def panel(py, agent, hue, up, arrow_c, relation, light_text):
        p = by[agent]
        bucket = conf_bucket(p["confidence"])
        L = (_VIOLET_L if light_text else _AMBER_L)[bucket]
        main = TEXT if light_text else "#141210"
        sec = "#D8D6D1" if light_text else "#3B3520"
        fig.patches.append(FancyBboxPatch(
            (0.05, py), 0.90, 0.225, boxstyle="round,pad=0,rounding_size=0.022",
            transform=fig.transFigure, facecolor=_at_light(hue, L), edgecolor="none", zorder=1))
        T = py + 0.225
        _arrow(fig, 0.088, py + 0.1125, up, arrow_c, p["confidence"])
        # name (upper) + ABOVE·NN% / bucket (lower), same left vertical
        fig.text(0.15, T - 0.05, agent.capitalize(), color=main, fontsize=33,
                 fontweight="bold", ha="left", va="center", zorder=3)
        fig.text(0.15, py + 0.085, f"{relation} · {round(p['confidence']*100)}%",
                 color=main, fontsize=20, fontweight="bold", ha="left", va="center", zorder=3)
        fig.text(0.15, py + 0.045, bucket, color=sec, fontsize=18, ha="left", va="center", zorder=3)
        # Why column: hard wrap, max 3 lines, shrink to stay inside the panel
        fig.text(0.55, T - 0.05, "Why:", color=sec, fontsize=16, ha="left", va="center", zorder=3)
        drv = str(p.get("driver", "")).strip()[:90]
        fs, wrapped = 15, textwrap.wrap(drv, width=40)
        if len(wrapped) > 3:
            fs, wrapped = 13, textwrap.wrap(drv, width=46)[:3]
        for i, ln in enumerate(wrapped[:3]):
            fig.text(0.55, T - 0.095 - i * 0.037, ln, color=main, fontsize=fs,
                     ha="left", va="center", zorder=3)
    panel(0.66, "oracle", ORACLE, True, DGREEN, "ABOVE", light_text=True)
    panel(0.135, "guardian", GUARDIAN, False, DRED, "BELOW", light_text=False)

    # 3. centre: level hero on the left, outlined BTC-now chip on the right
    fig.text(0.06, 0.612, "THE LINE · daily close", color=MUTED, fontsize=15, va="center")
    hero = fig.text(0.06, 0.51, f"${level:,.0f}", color=TEXT, fontsize=94,
                    fontweight="bold", va="center", fontfamily=NUM)
    _fit(fig, hero, 0.48)
    fig.patches.append(FancyBboxPatch(
        (0.60, 0.42), 0.35, 0.19, boxstyle="round,pad=0,rounding_size=0.022",
        transform=fig.transFigure, facecolor=BG, edgecolor=OUTLINE, linewidth=1.5, zorder=1))
    fig.text(0.625, 0.578, "BTC now", color=MUTED, fontsize=16, ha="left", va="center", zorder=3)
    dcol = DGREEN if day_chg >= 0 else DRED
    tv = fig.text(0.925, 0.578, f"{day_chg:+.2f}%", color=dcol, fontsize=16,
                  ha="right", va="center", zorder=3, fontfamily=NUM)
    fig.canvas.draw()
    fig.text(0.925 - _wfrac(tv, fig), 0.578, "Today: ", color=MUTED, fontsize=16,
             ha="right", va="center", zorder=3)
    fig.text(0.775, 0.485, f"${price:,.0f}", color=TEXT, fontsize=32, fontweight="bold",
             ha="center", va="center", zorder=3, fontfamily=NUM)

    # 5. footer: date left, day + score right
    fig.text(0.05, 0.065, footer_left, color=MUTED, fontsize=15, va="center")
    fig.text(0.95, 0.065, footer_right, color=MUTED, fontsize=15, ha="right", va="center")

    fig.savefig(out, facecolor=BG); plt.close(fig); return out


def build_resolution_card(out, close_px, level, winner, preds, missed_by, case_no,
                          season_line, streak, date_label, closes=None):
    """Template A — closing price + a tilted winner stamp; loser miss below."""
    fig = plt.figure(figsize=(10.8, 10.8), dpi=100, facecolor=BG)

    fig.text(0.06, 0.94, f"CASE No. {case_no} — CLOSED   ·   {date_label.upper()} DAILY CLOSE",
             color=MUTED, fontsize=16, va="center")

    # hero: closing price (kept lower-left so the stamp sits clear above-right)
    hero = fig.text(0.06, 0.66, f"${close_px:,.0f}", color=TEXT, fontsize=150,
                    fontweight="bold", va="center", fontfamily=NUM)
    _fit(fig, hero, 0.60)
    fig.text(0.065, 0.55, "DAILY CLOSE", color=MUTED, fontsize=20, va="center")

    # tilted winner stamp (-8° — the only allowed non-right angle); green = CORRECT
    if winner in ("oracle", "guardian"):
        color = DGREEN
        cx, cy, w, h = 0.73, 0.86, 0.30, 0.085
        rect = FancyBboxPatch((cx - w/2, cy - h/2), w, h,
                              boxstyle="round,pad=0,rounding_size=0.012",
                              facecolor=color, edgecolor="none", zorder=4)
        rect.set_transform(Affine2D().rotate_deg_around(cx, cy, -8) + fig.transFigure)
        fig.patches.append(rect)
        fig.text(cx, cy, f"{winner.upper()} ✓", color=BG, fontsize=30, fontweight="bold",
                 ha="center", va="center", rotation=-8, zorder=5, transform=fig.transFigure)

    # loser line: side · level · missed by delta
    if winner in ("oracle", "guardian"):
        loser = "guardian" if winner == "oracle" else "oracle"
        ldir = "below" if loser == "guardian" else "above"
        fig.text(0.06, 0.44, loser.upper() + "  ✗", color=DRED, fontsize=22,
                 fontweight="bold", va="center")
        seg = fig.text(0.06, 0.375, f"{ldir} ${level:,.0f}  ·  missed by ${missed_by:,.0f}",
                       color=MUTED, fontsize=22, va="center", fontfamily=NUM)
        _fit(fig, seg, 0.88)

    # footer: score + streak
    foot = season_line + (f"       Streak:  {streak}" if streak else "")
    fig.text(0.06, 0.09, foot, color=MUTED, fontsize=15, va="center")

    fig.savefig(out, facecolor=BG); plt.close(fig); return out
