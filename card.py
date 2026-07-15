#!/usr/bin/env python3
"""THE ROOM daily cards v3 — the duel is the hero, price is context.

Two templates, one frozen visual system (palette + Inter Tight/tabular unchanged):
  build_today_card       — Template B: two symmetric bet panels + context sparkline.
  build_resolution_card  — Template A: closing price + tilted winner stamp.
"""
import colorsys, textwrap, requests, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
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


def _sparkline(fig, rect, closes, level):
    ax = fig.add_axes(rect); ax.patch.set_visible(False)
    lo, hi = min(min(closes), level), max(max(closes), level)
    pad = (hi - lo) * 0.20 or hi * 0.01
    ax.set_ylim(lo - pad, hi + pad); ax.set_xlim(-0.3, len(closes) - 0.7)
    ax.axhline(level, color=TEXT, ls=(0, (4, 3)), lw=1.0, alpha=0.45, zorder=1)
    ax.plot(range(len(closes)), closes, color=TEXT, lw=2.2,
            solid_capstyle="round", solid_joinstyle="round", zorder=2)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks([]); ax.set_yticks([]); ax.grid(False)


def build_today_card(out, price, predictions, level, case_no, season_line,
                     weekly_line=None, closes=None):
    """Template B — two symmetric bet panels; the shared level is the hero."""
    if closes is None:
        closes = load_closes(14)
    day_chg = (closes[-1] / closes[-2] - 1) * 100
    by = {p["agent"]: p for p in predictions}
    fig = plt.figure(figsize=(10.8, 10.8), dpi=100, facecolor=BG)

    # header
    fig.text(0.06, 0.955, " ".join("THE ROOM") + f"   ·   CASE No. {case_no}   ·   "
             + datetime.now(timezone.utc).strftime("%d %b %Y").upper(),
             color=MUTED, fontsize=15, va="center")

    # two symmetric panels (violet left / amber right); level is the biggest glyph
    PY, PH = 0.455, 0.44
    def panel(px, agent, color, updown, arrow, relation):
        fig.patches.append(FancyBboxPatch(
            (px, PY), 0.43, PH, boxstyle="round,pad=0,rounding_size=0.015",
            transform=fig.transFigure, facecolor=_at_light(color, 0.18),
            edgecolor="none", zorder=1))
        cx = px + 0.215
        T = PY + PH
        fig.text(cx, T - 0.045, f"{agent.upper()}  {arrow} {updown}", color=color,
                 fontsize=23, fontweight="bold", ha="center", va="center", zorder=3)
        fig.text(cx, T - 0.095, relation, color=MUTED, fontsize=15, ha="center",
                 va="center", zorder=3)
        big = fig.text(cx, T - 0.175, f"${level:,.0f}", color=TEXT, fontsize=56,
                       fontweight="bold", ha="center", va="center", zorder=3, fontfamily=NUM)
        _fit(fig, big, 0.40)
        p = by[agent]
        fig.text(cx, T - 0.255, f"{round(p['confidence']*100)}% · {conf_bucket(p['confidence'])}",
                 color=TEXT, fontsize=21, fontweight="bold", ha="center", va="center", zorder=3)
        drv = str(p.get("driver", "")).strip()
        if drv:
            for i, ln in enumerate(textwrap.wrap(drv, width=30)[:2]):
                fig.text(cx, T - 0.315 - i * 0.035, ln, color=MUTED, fontsize=14,
                         ha="center", va="center", zorder=3)
    panel(0.05, "oracle", ORACLE, "UP", "▲", "ABOVE")
    panel(0.52, "guardian", GUARDIAN, "DOWN", "▼", "BELOW")

    # context strip + 7-day sparkline (level dashed through it)
    fig.text(0.5, 0.395, f"BTC now ${price:,.0f}  ·  {day_chg:+.2f}% today",
             color=TEXT, fontsize=17, ha="center", va="center")
    _sparkline(fig, [0.30, 0.305, 0.40, 0.06], closes[-7:], level)

    # footer: score · weekly bet · tagline
    fig.text(0.06, 0.185, season_line, color=MUTED, fontsize=15, va="center")
    if weekly_line:
        fig.text(0.06, 0.145, weekly_line, color=MUTED, fontsize=14, va="center")
    fig.text(0.06, 0.075, "Two minds enter. One verdict.", color=TEXT, fontsize=16,
             fontweight="bold", va="center")

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

    # tilted winner stamp (-8° — the only allowed non-right angle)
    if winner in ("oracle", "guardian"):
        color = ORACLE if winner == "oracle" else GUARDIAN
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
        lcolor = GUARDIAN if loser == "guardian" else ORACLE
        ldir = "below" if loser == "guardian" else "above"
        fig.text(0.06, 0.44, loser.upper(), color=lcolor, fontsize=22,
                 fontweight="bold", va="center")
        seg = fig.text(0.06, 0.375, f"{ldir} ${level:,.0f}  ·  missed by ${missed_by:,.0f}",
                       color=MUTED, fontsize=22, va="center", fontfamily=NUM)
        _fit(fig, seg, 0.88)

    # footer: score + streak
    foot = season_line + (f"       Streak:  {streak}" if streak else "")
    fig.text(0.06, 0.09, foot, color=MUTED, fontsize=15, va="center")

    fig.savefig(out, facecolor=BG); plt.close(fig); return out
